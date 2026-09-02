"""拆解记录（ledger）的读写。

拆解 skill 每完成一次拆解就追写一条；评测 skill 只读这些记录，不重新调用拆解
服务。存完整 separate_claims 是必要的：黑盒服务的输出不确定，评测时重调会得到
另一份结果，评的就不是用户当时实际拿到的那份。

两种输入形态都记录：
    input_kind="patent"    存专利号，评测时按 CLMS 回取权利要求原文。
    input_kind="freetext"  没有可回取的上游，因此把拆解时的原文一起存下来。

缺失 input_kind 的旧记录按 patent 处理（见 record_input_kind）。

拆解 skill 依赖本模块，因此这里只用标准库，且写入失败绝不向调用方抛错——记账是
评测的附属功能，不能影响拆解本身的输出。
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


LEDGER_NAME = "decomposition_ledger.json"
SCHEMA_VERSION = "1.1"

KIND_PATENT = "patent"
KIND_FREETEXT = "freetext"
_CONFIGURED_DATA_DIR = None


def configure_data_dir(path=None):
    """Apply the already allowlisted runtime setting without mutating env vars."""
    global _CONFIGURED_DATA_DIR
    _CONFIGURED_DATA_DIR = (
        Path(path).expanduser().resolve() if str(path or "").strip() else None
    )


def data_dir():
    """产物目录。默认 <cc-eval-test>/cc_eval_data，可用环境变量覆盖。

    目录名与 CC_EVAL_DATA_DIR 变量名沿用旧名不改：它们是拆解 skill 侧的既有
    契约（CC_EVAL_RECORD 同理），改名会让已有记录失联。
    """
    if _CONFIGURED_DATA_DIR is not None:
        return _CONFIGURED_DATA_DIR
    override = os.getenv("CC_EVAL_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # scripts/ledger.py -> feature-separate-eval -> skills -> 项目根
    return (Path(__file__).resolve().parents[3] / "cc_eval_data").resolve()


def ledger_path():
    return data_dir() / LEDGER_NAME


def evaluations_dir():
    return data_dir() / "evaluations"


def _now():
    return datetime.now(timezone.utc).isoformat()


class LedgerError(RuntimeError):
    """记录文件存在但不可用。"""


def load_ledger():
    path = ledger_path()
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "records": [], "updated_at": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"拆解记录无法解析: {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise LedgerError(f"拆解记录格式不正确: {path}")
    return payload


def write_json(path, payload):
    """原子写：同目录临时文件 + 替换，避免并发拆解写坏记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _new_record_id():
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def _append(record):
    ledger = load_ledger()
    ledger["records"].append(record)
    ledger["schema_version"] = SCHEMA_VERSION
    ledger["updated_at"] = _now()
    write_json(ledger_path(), ledger)
    return record["record_id"]


def record_decomposition(patent_number, patent_id, separate_claims):
    """追写一条按专利号的拆解记录，返回 record_id。

    重复拆同一专利号不去重：每次调用都是该服务的一次独立输出，重复本身也是评测
    素材（同一专利两次拆解结果不一致是可报告的问题）。
    """
    return _append({
        "record_id": _new_record_id(),
        "recorded_at": _now(),
        "input_kind": KIND_PATENT,
        "patent_number": patent_number,
        "patent_id": patent_id,
        "separate_claims": separate_claims,
        "claim_count": len(separate_claims),
        "evaluated": False,
    })


def record_freetext(source_text, separate_claims):
    """追写一条自由文本拆解记录，返回 record_id。

    自由文本没有可回取的上游文本，所以原文必须在记账时一起存下来——否则评测侧
    没有比对基准，Judge 的证据摘抄也无法校验。
    """
    return _append({
        "record_id": _new_record_id(),
        "recorded_at": _now(),
        "input_kind": KIND_FREETEXT,
        "source_text": source_text,
        "separate_claims": separate_claims,
        "claim_count": len(separate_claims),
        "evaluated": False,
    })


def record_input_kind(record):
    """旧记录没有 input_kind 字段，一律按专利处理（当时只记录按专利号的拆解）。"""
    kind = str(record.get("input_kind") or "").strip()
    return kind if kind in (KIND_PATENT, KIND_FREETEXT) else KIND_PATENT


def record_label(record):
    """报告与日志里用来指代一条记录的短标签。"""
    if record_input_kind(record) == KIND_FREETEXT:
        text = str(record.get("source_text") or "").strip().replace("\n", " ")
        return f"自由文本「{text[:24]}…」" if len(text) > 24 else f"自由文本「{text}」"
    return str(record.get("patent_number") or "(无专利号)")


def pending_records(ledger=None):
    """尚未评测的记录，按记录顺序。"""
    ledger = ledger if ledger is not None else load_ledger()
    return [
        item for item in ledger["records"]
        if isinstance(item, dict) and not item.get("evaluated")
    ]


def find_record(record_id=None, patent_number=None, ledger=None):
    """按 record_id 或专利号取一条记录。

    都不给时返回最新一条未评测记录；给专利号时返回该专利号最近一条记录（不限
    是否已评测，便于对同一篇反复调规则）。取不到返回 None。
    """
    ledger = ledger if ledger is not None else load_ledger()
    records = [item for item in ledger["records"] if isinstance(item, dict)]

    if record_id:
        wanted = str(record_id).strip()
        for item in records:
            if str(item.get("record_id")) == wanted:
                return item
        return None

    if patent_number:
        wanted = str(patent_number).strip().upper()
        matched = [
            item for item in records
            if str(item.get("patent_number") or "").strip().upper() == wanted
        ]
        return matched[-1] if matched else None

    pending = [item for item in records if not item.get("evaluated")]
    return pending[-1] if pending else None


def mark_evaluated(record_id):
    """把一条记录标记为已评测。不再有 batch 概念。"""
    ledger = load_ledger()
    wanted = str(record_id)
    for item in ledger["records"]:
        if isinstance(item, dict) and str(item.get("record_id")) == wanted:
            item["evaluated"] = True
            item["evaluated_at"] = _now()
    ledger["updated_at"] = _now()
    write_json(ledger_path(), ledger)
