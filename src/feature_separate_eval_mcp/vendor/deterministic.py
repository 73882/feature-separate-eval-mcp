"""Deterministic, source-grounded checks that never use GT or an LLM.

Vendored from claim-decomposition-no-gt-eval. Keep in sync with the source.
"""

import re
import unicodedata
from collections import Counter


NUMBER = re.compile(r"\d+(?:[.,]\d+)?(?:\s*[%％])?")
COMPARATORS = (
    ">=", "<=", ">", "<", "≥", "≤", "不少于", "不大于", "至少", "至多",
    "大于", "小于", "more than", "less than", "at least", "at most",
    "not less than", "not greater than",
)
NEGATIONS = (
    "不", "非", "未", "无", "不得", "not", "without", "none", "never",
)
# 密度超过批次中位数这个倍数即视为粒度离群。
GRANULARITY_TOLERANCE = 1.8
MIN_CLAIMS_FOR_GRANULARITY_CHECK = 5
# 绝对闸门阈值。批次内相对判定（GRANULARITY_TOLERANCE）在整批都过拆时会连同
# 中位数一起抬高而失效，所以拦截用绝对线。取值按历史 8 次 baseline 的实测分布
# 校准：正常 claim 特征数上限 19、密度上限 69；把 Judge 顶到 token 上限的那条
# 是 feature_count=240、density=127。阈值取正常值约 3 倍、事故值约 1/2。
MAX_FEATURES_PER_CLAIM = 60
MAX_FEATURE_DENSITY = 100.0
# 低于该有效长度的 claim 不参与密度判定：分母太小会让比值失去意义（9 个有效
# 字符拆出 7 个特征时密度高达 778，却不说明过拆）。round_05 的真实分布为
# p25=98、median=162，取 100 只滤掉约四分之一最短的从属权要。
MIN_CLAIM_LENGTH_FOR_DENSITY = 100
REFERENCE_PATTERNS = (
    re.compile(r"权利要求\s*([1-9]\d*)"),
    re.compile(r"claim\s+([1-9]\d*)", re.I),
)


def normalized_with_map(text):
    normalized = []
    mapping = []
    for raw_index, raw_char in enumerate(text):
        for char in unicodedata.normalize("NFKC", raw_char):
            if char.isalnum():
                normalized.append(char.lower())
                mapping.append(raw_index)
    return "".join(normalized), mapping


def normalize(text):
    return normalized_with_map(text)[0]


def _find_interval(claim_norm, feature_norm, previous_end, occupied):
    if not feature_norm:
        return None
    positions = []
    start = 0
    while True:
        index = claim_norm.find(feature_norm, start)
        if index < 0:
            break
        positions.append((index, index + len(feature_norm)))
        start = index + 1
    if not positions:
        return None
    after_previous = [interval for interval in positions if interval[0] >= previous_end]
    candidates = after_previous or positions
    candidates.sort(
        key=lambda interval: (
            sum(max(0, min(interval[1], end) - max(interval[0], begin)) for begin, end in occupied),
            interval[0],
        )
    )
    return candidates[0]


def _union_length(intervals):
    if not intervals:
        return 0
    merged = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def _present_tokens(text, tokens):
    lower = unicodedata.normalize("NFKC", text).lower()
    return sorted({token for token in tokens if token.lower() in lower})


def _references(text):
    values = set()
    for pattern in REFERENCE_PATTERNS:
        values.update(match.group(1) for match in pattern.finditer(text))
    return sorted(values)


def evaluate_claim(claim_key, claim_text, features):
    claim_norm, mapping = normalized_with_map(claim_text)
    intervals = []
    located = []
    previous_start = -1
    previous_end = 0
    order_violations = 0
    normalized_features = [normalize(feature) for feature in features]

    for index, (feature, feature_norm) in enumerate(
        zip(features, normalized_features), start=1
    ):
        interval = _find_interval(
            claim_norm, feature_norm, previous_end, intervals
        )
        item = {
            "feature_id": f"{claim_key}_A{index:03d}",
            "text": feature,
            "normalized_length": len(feature_norm),
            "located": interval is not None,
            "span": None,
        }
        if interval is not None:
            start, end = interval
            if previous_start >= 0 and start < previous_start:
                order_violations += 1
            previous_start = start
            previous_end = max(previous_end, end)
            intervals.append(interval)
            raw_start = mapping[start] if mapping else 0
            raw_end = mapping[end - 1] + 1 if mapping and end else raw_start
            item["span"] = {
                "normalized_start": start,
                "normalized_end": end,
                "raw_start": raw_start,
                "raw_end": raw_end,
                "source_quote": claim_text[raw_start:raw_end],
            }
        located.append(item)

    union_length = _union_length(intervals)
    total_feature_span = sum(end - start for start, end in intervals)
    duplicate_values = sorted(
        value for value, count in Counter(normalized_features).items()
        if value and count > 1
    )
    claim_numbers = set(NUMBER.findall(unicodedata.normalize("NFKC", claim_text)))
    feature_numbers = set(
        NUMBER.findall(unicodedata.normalize("NFKC", "\n".join(features)))
    )
    unsupported_numbers = sorted(feature_numbers - claim_numbers)
    claim_comparators = set(_present_tokens(claim_text, COMPARATORS))
    feature_comparators = set(_present_tokens("\n".join(features), COMPARATORS))
    claim_negations = set(_present_tokens(claim_text, NEGATIONS))
    feature_negations = set(_present_tokens("\n".join(features), NEGATIONS))
    claim_refs = set(_references(claim_text))
    feature_refs = set(_references("\n".join(features)))

    feature_count = len(features)
    located_count = sum(item["located"] for item in located)
    return {
        "claim_key": claim_key,
        "effective_claim_length": len(claim_norm),
        "feature_count": feature_count,
        # 每千有效字符的特征数。过拆（例如把父级主体重复进每个特征后再切开）
        # 会把密度显著推高，而这与 GT 无关，可在离线评测前就观察到。
        "feature_density": round(
            feature_count / len(claim_norm) * 1000, 2
        ) if claim_norm else 0.0,
        "located_feature_count": located_count,
        "traceability_rate": round(located_count / feature_count, 4) if feature_count else 0.0,
        "source_coverage": round(union_length / len(claim_norm), 4) if claim_norm else 0.0,
        "overlap_rate": round(
            (total_feature_span - union_length) / total_feature_span, 4
        ) if total_feature_span else 0.0,
        "order_violation_count": order_violations,
        "duplicate_feature_count": sum(
            max(0, count - 1) for count in Counter(normalized_features).values()
        ),
        "duplicate_normalized_values": duplicate_values,
        "unsupported_numbers": unsupported_numbers,
        "unsupported_comparators": sorted(feature_comparators - claim_comparators),
        "unsupported_negations": sorted(feature_negations - claim_negations),
        "reference_mismatches": sorted(feature_refs - claim_refs),
        "critical_conflict_count": (
            len(unsupported_numbers)
            + len(feature_comparators - claim_comparators)
            + len(feature_negations - claim_negations)
            + len(feature_refs - claim_refs)
        ),
        "features": located,
    }


def granularity_outliers(claim_results, tolerance=GRANULARITY_TOLERANCE):
    """按特征密度找出粒度离群的 claim。

    guardrail 目前只是写给模型看的自然语言（"独立事实仍拆分"），没有任何自动
    检查，所以 v5 那种粒度漂移只能等离线 GT 评测才暴露。密度离群是无 GT 可测
    的代理信号：同一批专利内部，某条 claim 的每千字符特征数远高于中位数，通常
    意味着把父级主体或条件重复注入后又切开了。

    单轮使用规则让每轮专利都不重复，无法跨轮比较同一篇，因此判定只在批次内做，
    不依赖任何历史基线。
    """
    # 极短 claim 的密度由分母支配而非拆分粒度：9 个有效字符拆出 7 个特征时密度
    # 高达 778，却不说明过拆。只在长度足够、密度有意义的 claim 上判定。
    eligible = [
        item for item in claim_results
        if item.get("feature_density")
        and item.get("effective_claim_length", 0) >= MIN_CLAIM_LENGTH_FOR_DENSITY
    ]
    densities = [item["feature_density"] for item in eligible]
    if len(densities) < MIN_CLAIMS_FOR_GRANULARITY_CHECK:
        return []
    ordered = sorted(densities)
    middle = len(ordered) // 2
    median = (
        ordered[middle] if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )
    if not median:
        return []
    return [
        {
            "claim_key": item["claim_key"],
            "feature_density": item["feature_density"],
            "batch_median_density": round(median, 2),
            "ratio": round(item["feature_density"] / median, 2),
            "feature_count": item["feature_count"],
            "effective_claim_length": item["effective_claim_length"],
        }
        for item in eligible
        if item["feature_density"] > median * tolerance
    ]


def granularity_violations(
    claim_results,
    max_features=MAX_FEATURES_PER_CLAIM,
    max_density=MAX_FEATURE_DENSITY,
):
    """按绝对阈值找出会把 Judge 顶到 token 上限的过拆 claim。

    与 granularity_outliers 的分工：那个是批次内相对信号，喂给问题聚合；这个是
    硬闸门，命中即中止本轮，用来在花掉 Judge 额度之前拦住必然失败的批次。整批
    都过拆时中位数会一起抬高，相对判定因此不能用作闸门。

    密度判定沿用 MIN_CLAIM_LENGTH_FOR_DENSITY 下限，否则 9 个有效字符拆出 7 个
    特征（密度 778）会被误判成过拆。特征数是绝对计数，不受该下限约束。
    """
    violations = []
    for item in claim_results:
        reasons = []
        feature_count = item.get("feature_count") or 0
        density = item.get("feature_density") or 0.0
        length = item.get("effective_claim_length", 0)
        if feature_count > max_features:
            reasons.append(
                f"特征数 {feature_count} 超过上限 {max_features}"
            )
        if density > max_density and length >= MIN_CLAIM_LENGTH_FOR_DENSITY:
            reasons.append(
                f"每千字符特征数 {density} 超过上限 {max_density}"
            )
        if reasons:
            violations.append({
                "claim_key": item["claim_key"],
                "feature_count": feature_count,
                "feature_density": density,
                "effective_claim_length": length,
                "max_features": max_features,
                "max_density": max_density,
                "reasons": reasons,
            })
    return violations


def aggregate_deterministic(claim_results):
    if not claim_results:
        return {}
    rates = (
        "traceability_rate", "source_coverage", "overlap_rate",
    )
    aggregate = {
        key: round(sum(item[key] for item in claim_results) / len(claim_results), 4)
        for key in rates
    }
    for key in (
        "feature_count", "located_feature_count", "order_violation_count",
        "duplicate_feature_count", "critical_conflict_count",
    ):
        aggregate[key] = sum(item[key] for item in claim_results)
    return aggregate
