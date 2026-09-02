#!/usr/bin/env python3
"""特征拆解评测：判定 patent-tech-feature-separate 单次拆得准不准。

一次评测只看**一条拆解记录**（一篇专利或一段自由文本），按 GT 校准的规则指出
这次拆解的问题。没有批次、没有跨记录聚合、没有"累计 N 篇才算确认"的门槛——那套
跨专利归纳机制已随本 skill 改造删除。

只评测，不迭代 Prompt。被评对象是一个 HTTP 服务，它的 Prompt 与模型都不可见、
不可修改。

流程：
    选一条记录 → 取原文（专利回取 CLMS / 自由文本读 source_text）
    → 确定性检查 → 粒度闸门（零 Judge 消耗）→ 一次 Judge 审计
    → 终端输出问题清单 + 单次落盘
"""

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import ledger
from .claim_source import ClaimSourceError, claims_from_record
from .patent_client import (
    PatentClient,
    PatentConfig,
    PatentConfigurationError,
)
from .vendor.deterministic import (
    MIN_CLAIM_LENGTH_FOR_DENSITY,
    aggregate_deterministic,
    evaluate_claim,
    granularity_outliers,
    granularity_violations,
)
from .vendor.env_loader import load_env_file
from .vendor.judge import (
    JudgeConfig,
    JudgeConfigurationError,
    JudgeError,
    JudgePartialLengthError,
    proxy_score,
)

# 粒度闸门阈值。vendor/deterministic.py 的默认值（60 / 100）来自原项目历史
# baseline；规则数据源登记为
# ai_cc_new_search_part1.json（校准文件不随 skill 打包，运行时不读取）
# （42 条 claim 记录、34 篇专利、1263 条人工标注特征）：
# 当前阈值和基线数值为已固化的 v1 参数；仅替换数据源登记，不在此处重校准数值。
#   GT 单条 claim 特征数 max 26（独立权要 max 26，从属 max 12）→ 取约 1.5 倍余量
#   GT 特征密度 max 76.9 / 千有效字符（p90 52.6）→ 取约 2 倍余量
# 不改 vendor/deterministic.py：那是原项目副本，注释要求 keep in sync。
GT_MAX_FEATURES_PER_CLAIM = 40
GT_MAX_FEATURE_DENSITY = 160.0

# GT 基线，用于终端报告的粒度对照。
#
# 数据集口径见 SKILL.md；基线仅用作终端报告的参考，不作为单独判罚条件。
GT_BASELINE = {
    "independent": {"median": 6, "p75": 10, "p90": 13, "max": 26},
    "dependent": {"median": 2, "p75": 4, "p90": 6, "max": 12},
    "density": {"median": 36.4, "p75": 42.5, "p90": 52.6, "max": 76.9},
}

SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}
DIMENSION_LABELS = [
    ("source_faithfulness", "忠实"),
    ("completeness", "完整"),
    ("atomicity", "粒度"),
    ("relation_integrity", "关系"),
    ("reference_integrity", "引用"),
]


class EvalError(RuntimeError):
    """评测无法继续。"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _log(message):
    print(message, file=sys.stderr, flush=True)


def _normalized(text):
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def _claim_sort_key(claim_key):
    match = re.fullmatch(r"claim_(\d+)", str(claim_key))
    return int(match.group(1)) if match else 10**9


def _is_independent(claim_key):
    """claim_1 视为独立权利要求，用于选对照基线。

    真正的独立/从属判定需要看原文有没有引用语，但报告里只用来选一条参考基线，
    claim_1 这个近似足够；判罚本身由 Judge 按原文认定，不依赖这里。
    """
    return _claim_sort_key(claim_key) == 1


# ---------------------------------------------------------------- 记录读取

def _features_from_record(record):
    """把记录里的 separate_claims 还原成 {claim_key: [特征, ...]}。"""
    features = {}
    malformed = []
    for item in record.get("separate_claims") or []:
        if not isinstance(item, dict):
            malformed.append("非对象条目")
            continue
        name = str(item.get("name") or "").strip()
        separates = item.get("separates")
        if not re.fullmatch(r"claim_\d+", name):
            malformed.append(f"claim 键名不符合契约: {name!r}")
            continue
        if not isinstance(separates, list) or not separates:
            malformed.append(f"{name} 无特征")
            continue
        texts = [str(value).strip() for value in separates if str(value).strip()]
        if not texts:
            malformed.append(f"{name} 特征全为空")
            continue
        features[name] = texts
    return features, malformed


def _resolve_record(args):
    """定位要评测的那条记录。"""
    record = ledger.find_record(
        record_id=args.record_id, patent_number=args.patent
    )
    if record is not None:
        return record

    if args.record_id:
        raise EvalError(f"找不到记录 {args.record_id}。用 --status 查看现有记录。")
    if args.patent:
        raise EvalError(
            f"找不到 {args.patent} 的拆解记录。"
            "先用 patent-tech-feature-separate 拆一次该专利。"
        )
    raise EvalError(
        "没有未评测的拆解记录。先用 patent-tech-feature-separate 拆一次，"
        "或用 --record-id 指定一条已评测的记录重跑。"
    )


# ---------------------------------------------------------------- 单次评测

def _prepare(record, api_client):
    """取原文 + 确定性检查。不调用 Judge，闸门放行后才花额度。"""
    kind = ledger.record_input_kind(record)
    label = ledger.record_label(record)
    _log(f"取原文: {label}（{kind}）")

    claims = claims_from_record(record, api_client)
    features, malformed = _features_from_record(record)

    failures = []
    for note in malformed:
        failures.append({
            "stage": "ledger_contract",
            "message": f"拆解记录不符合契约: {note}",
            "claim_recoverable": True,
        })

    claim_results = []
    judge_inputs = []
    for claim_key, claim_text in sorted(claims.items(), key=lambda kv: _claim_sort_key(kv[0])):
        claim_features = features.get(claim_key, [])
        checks = evaluate_claim(claim_key, claim_text, claim_features)
        claim_results.append({
            "claim_key": claim_key,
            "claim_text": claim_text,
            "features": claim_features,
            "deterministic": checks,
            "judge": None,
            "proxy_score": None,
            "status": "ok" if claim_features else "missing_decomposition_output",
        })
        if claim_features:
            judge_inputs.append({
                "claim_key": claim_key,
                "claim_text": claim_text,
                "features": claim_features,
                "deterministic": checks,
            })
        else:
            # 服务漏掉整条 claim 是接口层失败，必须记录而不是静默跳过。
            failures.append({
                "claim_key": claim_key,
                "stage": "claim_alignment",
                "message": "拆解结果遗漏该 claim",
                "claim_recoverable": True,
            })

    extra = sorted(set(features) - set(claims), key=_claim_sort_key)
    if extra:
        failures.append({
            "stage": "claim_alignment",
            "message": f"拆解结果多出原文不存在的 claim: {', '.join(extra)}",
            "claim_recoverable": True,
        })

    return {
        "record": record,
        "input_kind": kind,
        "label": label,
        "claims": claims,
        "claim_results": claim_results,
        "judge_inputs": judge_inputs,
        "extra_claims": extra,
        "failures": failures,
    }


def _enforce_gate(item, max_features, max_density):
    """Judge 调用之前的硬闸门。命中时 Judge 额度零消耗。"""
    checks = [c["deterministic"] for c in item["claim_results"] if c.get("deterministic")]
    violations = granularity_violations(
        checks, max_features=max_features, max_density=max_density
    )
    if not violations:
        return
    lines = [
        f"粒度闸门中止本次评测，未调用 Judge，额度零消耗。{item['label']} 拆解粒度异常："
    ]
    for violation in violations:
        lines.append(f"  {violation['claim_key']}: " + "；".join(violation["reasons"]))
    lines.append(
        f"（GT 实测上限：单条 claim 特征数 {GT_BASELINE['dependent']['max']}，"
        f"密度 {GT_BASELINE['density']['max']}/千字符）"
    )
    lines.append(
        "该记录仍标记为未评测。核对该服务在这篇上的输出，"
        "或用 --max-features-per-claim / --max-feature-density 调整阈值后重跑。"
    )
    raise EvalError("\n".join(lines))


def _run_judge(item, judge):
    """审计一次拆解。claim 级失败只作废那几条，其余继续。"""
    judge_inputs = item["judge_inputs"]
    if not judge_inputs:
        return []

    _log(f"Judge 审计 {len(judge_inputs)} 条 claim（input_kind={item['input_kind']}）")
    reviews = {}
    errors = {}
    try:
        reviews = judge.review_patent(judge_inputs)
    except JudgePartialLengthError as exc:
        # 单条 claim 过拆到顶破 token 上限时只作废那几条，其余 claim 继续。
        reviews = exc.reviews
        errors = {
            key: "Judge 输出超出 token 上限，该 claim 很可能被严重过拆"
            for key in exc.claim_keys
        }
    except (JudgeError, RuntimeError) as exc:
        errors = {entry["claim_key"]: str(exc) for entry in judge_inputs}

    for entry in judge_inputs:
        key = entry["claim_key"]
        if key not in reviews and key not in errors:
            errors[key] = "Judge 未返回该 claim 的审计结果"

    failures = []
    for claim in item["claim_results"]:
        key = claim["claim_key"]
        if key in reviews:
            claim["judge"] = reviews[key]
            claim["proxy_score"] = proxy_score(reviews[key]["dimensions"])
        elif key in errors:
            claim["status"] = "judge_failed"
            failures.append({
                "claim_key": key,
                "stage": "judge",
                "message": errors[key],
                "claim_recoverable": True,
            })
    return failures


# ---------------------------------------------------------------- 终端输出

def _collect_findings(item):
    """把 Judge 的逐特征判定与遗漏摊平成一张按严重度排序的问题清单。"""
    findings = []
    for claim in item["claim_results"]:
        review = claim.get("judge") or {}
        features = claim.get("features") or []

        for entry in review.get("feature_reviews") or []:
            if not isinstance(entry, dict):
                continue
            labels = [l for l in (entry.get("labels") or []) if l != "SUPPORTED_COMPLETE_FACT"]
            if not labels:
                continue
            # feature_id 形如 <claim_key>_A003，尾号即输入特征的 1-based 序号。
            text = ""
            match = re.search(r"_A(\d+)$", str(entry.get("feature_id") or ""))
            if match:
                index = int(match.group(1)) - 1
                if 0 <= index < len(features):
                    text = features[index]
            findings.append({
                "kind": "feature",
                "claim_key": claim["claim_key"],
                "severity": entry.get("severity") or "info",
                "labels": labels,
                "feature_text": text,
                "evidence": entry.get("claim_evidence") or "",
                "reason": entry.get("reason") or "",
            })

        for gap in review.get("coverage_gaps") or []:
            if not isinstance(gap, dict):
                continue
            findings.append({
                "kind": "gap",
                "claim_key": claim["claim_key"],
                "severity": gap.get("severity") or "major",
                "labels": ["COVERAGE_GAP"],
                "missing_fact": gap.get("missing_fact") or "",
                "evidence": gap.get("claim_evidence") or "",
                "reason": gap.get("reason") or "",
            })

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.get("severity"), 9), f["claim_key"]))
    return findings


def _recommendation_note(item):
    """汇总 Judge 给使用方的后处理建议，按 target 分组。"""
    postprocess, manual = [], []
    for claim in item["claim_results"]:
        for rec in (claim.get("judge") or {}).get("recommendations") or []:
            if not isinstance(rec, dict):
                continue
            text = str(rec.get("suggestion") or "").strip()
            if not text:
                continue
            (manual if rec.get("target") == "manual_review" else postprocess).append(
                (claim["claim_key"], text)
            )
    return postprocess, manual


def _print_report(item, findings, outliers, failures, out=None):
    # Resolve stdout at call time so the MCP wrapper can capture the report without
    # leaking bytes into the stdio JSON-RPC transport.
    if out is None:
        out = sys.stdout
    record = item["record"]
    scored = [c for c in item["claim_results"] if c.get("judge")]
    feature_total = sum(len(c["features"]) for c in item["claim_results"])

    print("", file=out)
    print("=" * 72, file=out)
    print(f"拆解评测  record={record.get('record_id')}  {item['label']}", file=out)
    print(
        f"输入类型: {item['input_kind']} | claim {len(item['claim_results'])} 条"
        f" | 特征 {feature_total} 条",
        file=out,
    )
    print(f"规则版本: {item.get('rules_version')} | Judge: {item.get('judge_model')}", file=out)
    print("=" * 72, file=out)

    if scored:
        parts = []
        for key, label in DIMENSION_LABELS:
            values = [
                c["judge"]["dimensions"].get(key)
                for c in scored
                if isinstance(c["judge"].get("dimensions"), dict)
                and c["judge"]["dimensions"].get(key) is not None
            ]
            if values:
                parts.append(f"{label} {sum(values) / len(values):.1f}")
        if parts:
            print(f"\n评分（{len(scored)} 条 claim 均值）  " + " | ".join(parts), file=out)

    print(f"\n问题清单: {len(findings)} 条", file=out)
    if not findings:
        print("  未发现违反规则的特征。", file=out)
    for finding in findings:
        severity = str(finding.get("severity", "?")).upper()
        print("", file=out)
        print(f"  [{severity}] {finding['claim_key']}  {'/'.join(finding['labels'])}", file=out)
        if finding["kind"] == "feature":
            if finding.get("feature_text"):
                print(f"    特征: {finding['feature_text']}", file=out)
        else:
            print(f"    遗漏: {finding.get('missing_fact')}", file=out)
        if finding.get("evidence"):
            print(f"    原文: {finding['evidence']}", file=out)
        if finding.get("reason"):
            print(f"    说明: {finding['reason']}", file=out)

    # GT 基线是按权利要求统计的。自由文本没有权利要求结构，段落长度也不可比，
    # 因此只列实测值不做基线判定，避免给出"远低于中位"这类无意义的结论。
    is_freetext = item["input_kind"] == ledger.KIND_FREETEXT
    header = "粒度对照（GT 基线）" if not is_freetext else \
        "粒度实测（自由文本无权利要求结构，GT 基线不适用，仅列实测值）"
    print(f"\n{header}", file=out)

    outlier_keys = {o["claim_key"] for o in outliers}
    for claim in item["claim_results"]:
        checks = claim.get("deterministic") or {}
        count = checks.get("feature_count") or 0
        density = checks.get("feature_density") or 0.0
        # 密度只在有效长度足够时才有意义：分母太小会让比值失去意义（33 个有效
        # 字符拆出 3 个特征时密度高达 90，却不说明过拆）。与闸门口径保持一致。
        length = checks.get("effective_claim_length") or 0
        density_meaningful = length >= MIN_CLAIM_LENGTH_FOR_DENSITY
        density_text = f"{density:>6.1f}" if density_meaningful else "   n/a"

        notes = []
        if not count:
            notes.append("无拆解输出")
        if claim["claim_key"] in outlier_keys:
            notes.append("本篇内密度离群")

        if is_freetext:
            suffix = ("  ← " + "；".join(notes)) if notes else ""
            print(
                f"  {claim['claim_key']:<10} {count:>3} 条 / 密度 {density_text}"
                f"   有效长度 {length}{suffix}",
                file=out,
            )
            continue

        base = GT_BASELINE["independent"] if _is_independent(claim["claim_key"]) \
            else GT_BASELINE["dependent"]
        role = "独立" if _is_independent(claim["claim_key"]) else "从属"
        if count > base["max"]:
            notes.append(f"超 GT {role}上限 {base['max']}")
        elif count > base["p90"]:
            notes.append(f"高于 GT {role} p90 {base['p90']}")
        elif count and count < base["median"] / 2:
            notes.append(f"远低于 GT {role}中位 {base['median']}")
        if density_meaningful and density > GT_BASELINE["density"]["max"]:
            notes.append(f"密度超 GT 上限 {GT_BASELINE['density']['max']}")
        suffix = ("  ← " + "；".join(notes)) if notes else ""
        print(
            f"  {claim['claim_key']:<10} {count:>3} 条 / 密度 {density_text}"
            f"   GT {role}中位 {base['median']} p90 {base['p90']} max {base['max']}{suffix}",
            file=out,
        )

    postprocess, manual = _recommendation_note(item)
    if postprocess or manual:
        print("\n应对建议", file=out)
        for claim_key, text in postprocess:
            print(f"  [可后处理] {claim_key}: {text}", file=out)
        for claim_key, text in manual:
            print(f"  [需人工复核] {claim_key}: {text}", file=out)

    if failures:
        print(f"\n接口层与执行失败: {len(failures)} 条", file=out)
        for entry in failures:
            location = entry.get("claim_key") or "-"
            print(f"  [{entry.get('stage')}] {location}: {entry.get('message')}", file=out)

    print("", file=out)


# ---------------------------------------------------------------- 只读命令

def _record_summary(record):
    return {
        "record_id": record.get("record_id"),
        "recorded_at": record.get("recorded_at"),
        "input_kind": ledger.record_input_kind(record),
        "label": ledger.record_label(record),
        "claim_count": record.get("claim_count"),
        "feature_count": sum(
            len(c.get("separates") or [])
            for c in record.get("separate_claims") or []
            if isinstance(c, dict)
        ),
        "evaluated": bool(record.get("evaluated")),
    }


def _cmd_status(args):
    state = ledger.load_ledger()
    records = [r for r in state.get("records") or [] if isinstance(r, dict)]
    pending = ledger.pending_records(state)
    print(json.dumps({
        "ledger_file": str(ledger.ledger_path()),
        "evaluations_dir": str(ledger.evaluations_dir()),
        "total_records": len(records),
        "pending_records": len(pending),
        "next_record": _record_summary(pending[-1]) if pending else None,
        "records": [_record_summary(r) for r in records],
    }, ensure_ascii=False, indent=2))
    return 0


def _require_network_permission(args, summary):
    """Require an explicit per-command acknowledgement before any network call."""
    if not args.allow_network:
        raise EvalError(
            "本命令需要网络访问，但尚未获得本次发送确认。\n"
            f"将发送：{summary}。\n"
            "确认目标记录和发送范围后，添加 --allow-network 重跑。"
        )
    _log(f"网络发送已确认：{summary}")


def _patent_client(settings):
    try:
        config = PatentConfig.from_mapping(settings)
    except PatentConfigurationError as exc:
        raise EvalError(f"PatSnap 配置不安全或不完整: {exc}") from exc
    _log(f"PatSnap 端点已通过协议与主机白名单校验: {config.base_url}")
    return PatentClient(config)


def _cmd_check(args):
    """核对一条记录能否评测：原文可取性与 claim 对齐。不调用 Judge。"""
    record = _resolve_record(args)
    features, malformed = _features_from_record(record)
    row = _record_summary(record)
    row["ledger_claim_count"] = len(features)
    row["ledger_problems"] = malformed

    if not record.get("separate_claims"):
        row["ok"] = False
        row["error"] = (
            "拆解记录为空（separate_claims 无内容）。这是一次失败的拆解，"
            "属于接口层失败，无法评测其拆解质量。"
        )
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return 1

    client = None
    if ledger.record_input_kind(record) == ledger.KIND_PATENT:
        _require_network_permission(
            args,
            "PatSnap：发送专利号并仅请求 CLMS 字段",
        )
        client = _patent_client(args.settings)
    try:
        claims = claims_from_record(record, client)
        row["source_claim_count"] = len(claims)
        row["missing_claims"] = sorted(set(claims) - set(features), key=_claim_sort_key)
        row["extra_claims"] = sorted(set(features) - set(claims), key=_claim_sort_key)
        row["ok"] = not malformed and not row["missing_claims"] and not row["extra_claims"]
    except ClaimSourceError as exc:
        row["source_claim_count"] = None
        row["error"] = str(exc)
        row["ok"] = False

    print(json.dumps(row, ensure_ascii=False, indent=2))
    return 0 if row["ok"] else 1


# ---------------------------------------------------------------- 评测主流程

def _cmd_run(args):
    # Judge 只在真实评测时导入；--status / --check 不加载 Judge 客户端。
    from .judge_client import FeatureSeparateJudge

    record = _resolve_record(args)
    record_id = record.get("record_id")
    _log(f"评测记录 {record_id}（{ledger.record_label(record)}）")

    # 空拆解是接口层失败：没有特征可审计，直接结束而不消耗 Judge 额度。
    if not record.get("separate_claims"):
        raise EvalError(
            f"记录 {record_id} 的 separate_claims 为空——这次拆解没有产出任何特征。\n"
            "这是接口层失败（服务返回空结果或解析未命中），不是拆解质量问题，"
            "无法进行特征级评测。\n"
            "请重新拆解该输入；若稳定复现，说明该服务在这篇上不可用。"
        )

    _require_network_permission(
        args,
        "Judge：发送当前记录的 claim 原文、拆解特征和确定性检查结果"
        + (
            "；PatSnap：发送专利号并仅请求 CLMS 字段"
            if ledger.record_input_kind(record) == ledger.KIND_PATENT else ""
        ),
    )

    try:
        judge = FeatureSeparateJudge(
            config=JudgeConfig.from_mapping(
                args.settings, model=args.judge_model
            )
        )
        _log(
            "Judge 端点已通过协议与主机白名单校验: "
            f"{judge.config.api_url}"
        )
    except JudgeConfigurationError as exc:
        from .vendor.env_loader import default_env_path
        raise EvalError(
            f"Judge 配置缺失: {exc}\n"
            f"请在 {default_env_path()} 中配置 "
            "CLAIM_DECOMPOSITION_JUDGE_API_URL、"
            "CLAIM_DECOMPOSITION_JUDGE_API_TOKEN 与 "
            "CLAIM_DECOMPOSITION_JUDGE_ALLOWED_HOSTS。\n"
            "首次使用：cp .env.example .env（在 skill 目录下），再填入自己的 key 与 url。"
        ) from exc
    client = None
    if ledger.record_input_kind(record) == ledger.KIND_PATENT:
        client = _patent_client(args.settings)

    # 阶段一：取原文 + 确定性检查。零 Judge 消耗。
    try:
        item = _prepare(record, client)
    except ClaimSourceError as exc:
        raise EvalError(
            f"取不到原文，本次中止（未消耗 Judge 额度）：{exc}\n"
            "没有原文就无法校验 Judge 的证据摘抄，评测结论不可信。"
        ) from exc

    # 阶段二：粒度闸门。命中即中止，Judge 额度零消耗。
    _enforce_gate(item, args.max_features_per_claim, args.max_feature_density)
    _log("粒度闸门通过")

    # 本篇内 claim 之间的相对密度离群，接入报告作为提示信号（不中止）。
    outliers = granularity_outliers(
        [c["deterministic"] for c in item["claim_results"] if c.get("deterministic")]
    )

    # 阶段三：一次 Judge 审计。
    item["rules_version"] = judge.rules_version
    item["judge_model"] = judge.config.model
    failures = list(item["failures"])
    failures.extend(_run_judge(item, judge))

    findings = _collect_findings(item)

    # 落盘单次审计。单条记录本来就在本地 ledger 里，不做脱敏，便于逐条核对。
    payload = {
        "record_id": record_id,
        "evaluated_at": _now(),
        "input_kind": item["input_kind"],
        "label": item["label"],
        "patent_number": record.get("patent_number"),
        "judge_model": judge.config.model,
        "judge_rules_version": judge.rules_version,
        "gt_baseline": GT_BASELINE,
        "gate": {
            "max_features_per_claim": args.max_features_per_claim,
            "max_feature_density": args.max_feature_density,
        },
        "claim_total": len(item["claim_results"]),
        "claims": [
            {
                "claim_key": c["claim_key"],
                "claim_text": c["claim_text"],
                "features": c["features"],
                "status": c["status"],
                "proxy_score": c.get("proxy_score"),
                "deterministic": c.get("deterministic"),
                "judge": c.get("judge"),
            }
            for c in item["claim_results"]
        ],
        "findings": findings,
        "granularity_outliers": outliers,
        "failures": failures,
        "deterministic_aggregate": aggregate_deterministic(
            [c["deterministic"] for c in item["claim_results"] if c.get("deterministic")]
        ),
    }
    out_path = ledger.evaluations_dir() / f"{record_id}.json"
    ledger.write_json(out_path, payload)
    _log(f"审计已落盘: {out_path}")

    _print_report(item, findings, outliers, failures)

    if args.keep_pending:
        _log("--keep-pending: 该记录仍标记为未评测，可重复评测。")
    else:
        ledger.mark_evaluated(record_id)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="特征拆解单次评测（patent-tech-feature-separate）",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true",
                        help="查看拆解记录与待评测情况（零调用）")
    action.add_argument("--check", action="store_true",
                        help="核对一条记录能否评测（零 Judge 调用）")

    target = parser.add_mutually_exclusive_group()
    target.add_argument("--record-id", default=None, help="指定要评测的记录 ID")
    target.add_argument("--patent", default=None, help="按专利号取该专利最近一条记录")

    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--max-features-per-claim", type=int,
                        default=GT_MAX_FEATURES_PER_CLAIM,
                        help=f"单条 claim 特征数上限，默认 {GT_MAX_FEATURES_PER_CLAIM}（GT 校准）")
    parser.add_argument("--max-feature-density", type=float,
                        default=GT_MAX_FEATURE_DENSITY,
                        help=f"每千有效字符特征数上限，默认 {GT_MAX_FEATURE_DENSITY}（GT 校准）")
    parser.add_argument("--keep-pending", action="store_true",
                        help="评完不标记 evaluated，便于对同一条记录反复调规则")
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="确认允许本次命令调用已配置且通过白名单校验的外部服务",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    _, args.settings = load_env_file(required=False)
    ledger.configure_data_dir(args.settings.get("CC_EVAL_DATA_DIR"))
    try:
        if args.status:
            return _cmd_status(args)
        if args.check:
            return _cmd_check(args)
        return _cmd_run(args)
    except (EvalError, ledger.LedgerError) as exc:
        _log(f"\n错误: {exc}")
        return 1
    except JudgeError as exc:
        _log(f"\nJudge 错误: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
