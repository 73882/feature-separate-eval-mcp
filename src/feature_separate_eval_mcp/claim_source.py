"""取拆解时的原文，作为 Judge 的比对基准。

Judge 要求 claim_evidence 必须是原文摘抄（judge.py 的 _require_source_evidence
会逐条校验），所以评测侧必须持有原文。两种输入形态来源不同：

    patent   走 /api/patent/fields/query 的 CLMS 字段——和拆解 skill 同一个上游
             服务，因此拿到的就是服务端拆解时看到的那份文本。不读 PDF：PDF 文本层
             与服务端取到的文本可能不同，那样评的就不是线上真实输入。
    freetext 没有可回取的上游，原文在记账时就存进了 ledger 的 source_text。
"""

import re
from . import ledger
from .vendor.extractor import InputError, split_claims


class ClaimSourceError(RuntimeError):
    """无法为某个专利号取得可用的权利要求文本。"""


# CLMS 把整篇权利要求书作为单行返回，不含换行。而 extractor 的 CLAIM_NUMBER 是
# (?m)^ 行锚定的，直接喂进去会让 15 条权利要求静默塌成 1 条——特征数与密度因此
# 全部失真，粒度闸门也会误判。这里在句末标点后、编号前补回换行；编号是否构成真实
# claim 序列仍由 extractor 的最长递增序列校验判定，所以 "2、SiN" 这类行内枚举
# 不会被误认成 claim 2。
_CLAIM_BOUNDARY = re.compile(
    r"(?<=[。；;:：\)）])\s*(?=[1-9]\d{0,2}\s*[.．、]\s*\S)"
)


def restore_claim_line_breaks(text):
    return _CLAIM_BOUNDARY.sub("\n", text)


def _extract_clms(response):
    """从响应中取出 CLMS 文本。

    api_client.query_patent_fields 的 data 形态随 render_type 变化：patent-view
    时是对象，否则是专利数组。CLMS 本身也可能是字符串或按条目返回的列表
    （_clean_html_tags 里就按"列表类型（如权利要求）"处理）。这里把两种维度都
    收敛掉，否则形态一变就会静默判成"字段为空"。
    """
    data = (response or {}).get("data")
    if isinstance(data, dict):
        entries = [data]
    elif isinstance(data, list):
        entries = [item for item in data if isinstance(item, dict)]
    else:
        return None

    for entry in entries:
        fields = entry.get("fields")
        if not isinstance(fields, dict):
            continue
        value = fields.get("CLMS")
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            parts = [str(item).strip() for item in value if str(item).strip()]
            if parts:
                # 按条目返回时每条即一条权利要求，用换行拼接正好符合行锚定切分。
                return "\n".join(parts)
    return None


def fetch_claims(api_client, patent_number, lang="cn"):
    """返回 {"claim_1": "...", ...}。取不到或解析不出就抛 ClaimSourceError。"""
    try:
        response = api_client.query_patent_fields(
            fields=["CLMS"], pns=[patent_number], lang=lang
        )
    except Exception as exc:  # 上游异常形态不固定，统一转成本模块的错误
        raise ClaimSourceError(f"{patent_number}: CLMS 查询失败: {exc}") from exc

    raw = _extract_clms(response)
    if not raw or not raw.strip():
        raise ClaimSourceError(f"{patent_number}: CLMS 字段为空，无法取得权利要求原文")

    try:
        claims = split_claims(restore_claim_line_breaks(raw))
    except InputError as exc:
        raise ClaimSourceError(f"{patent_number}: 权利要求切分失败: {exc}") from exc
    if not claims:
        raise ClaimSourceError(f"{patent_number}: 未切分出任何权利要求")
    return claims


def claims_from_record(record, api_client=None):
    """按记录形态取原文，统一返回 {"claim_1": "...", ...}。

    自由文本不走 split_claims：它没有权利要求编号结构，行锚定切分会把整段当成
    未编号文本而抛错。直接作为单一 claim_1 返回，由 Judge 侧按 input_kind
    跳过权利要求专属规则（G4/G7 编号依赖、引用前序惯例）。
    """
    kind = ledger.record_input_kind(record)

    if kind == ledger.KIND_FREETEXT:
        text = str(record.get("source_text") or "").strip()
        if not text:
            raise ClaimSourceError(
                f"记录 {record.get('record_id')}: 自由文本记录缺少 source_text，"
                "无法取得比对原文（该记录由旧版拆解 skill 写入，请重新拆解一次）"
            )
        return {"claim_1": text}

    patent_number = str(record.get("patent_number") or "").strip()
    if not patent_number:
        raise ClaimSourceError(f"记录 {record.get('record_id')}: 缺少专利号")
    if api_client is None:
        raise ClaimSourceError(f"{patent_number}: 需要 API 客户端才能回取权利要求原文")
    return fetch_claims(api_client, patent_number)
