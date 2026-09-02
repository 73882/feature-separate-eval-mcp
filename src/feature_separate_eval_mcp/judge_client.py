"""Judge 客户端：复用 PatsnapJudge 的网关调用与 schema 校验，换掉规则目录。

只保留一个阶段——逐篇（逐 claim 分块）审计。跨专利聚合已随本 skill 改造删除：
每次评测只看一次拆解，不做跨记录归纳，因此没有聚合阶段，也没有 issue_key
稳定性与跨专利门槛这套机制。

规则目录指向 prompts/gt_calibrated_rules_v1/。其标注惯例的数据源登记为
ai_cc_new_search_part1.json（42 条 claim 记录、34 篇专利、1263 条人工标注
特征）；校准文件不随 skill 打包，运行时也不读取。
"""

from pathlib import Path

from .vendor import judge as _vendor_judge
from .vendor.judge import PatsnapJudge


PROMPT_ROOT = Path(__file__).resolve().parent / "prompts"
RULES_VERSION = "gt_calibrated_rules_v1"

# gt_calibrated_rules_v1 新增的标签，对应 C1_COMPARABLE_UNIT 的"过细"判定：
# 特征单独拿出来无法判定公开性（裸公式、单独变量名、纯元语言提示）。
# vendor/judge.py 的 REVIEW_LABELS 是白名单，不含该标签会让整篇审计因 schema
# 校验失败而作废。在这里注册而不是改 vendor/：那是原项目副本，注释要求 keep in sync。
NEW_REVIEW_LABELS = {"NOT_INDEPENDENTLY_COMPARABLE"}
_vendor_judge.REVIEW_LABELS |= NEW_REVIEW_LABELS

# recommendations[].target 在 vendor 侧是按 ROOT_CAUSES 校验的（prompt/model/
# parser/other）。本 skill 的建议面向使用方的后处理，不面向改 Prompt，所以补进
# postprocess 与 manual_review 两个取值。
#
# 注意 ROOT_CAUSES 同时用于 root_cause 字段的校验，所以这里是放宽而非替换：
# 规则文件已明确限定 root_cause 只能取 model/parser/other，Judge 不会输出
# root_cause="postprocess"；而 vendor 里所有 prompt 相关的联动校验
# （target=="prompt" 需要 prompt 归因的 issue）在 root_cause 永不为 prompt 时
# 恒为真空条件，不会误触发。
NEW_RECOMMENDATION_TARGETS = {"postprocess", "manual_review"}
_vendor_judge.ROOT_CAUSES |= NEW_RECOMMENDATION_TARGETS


class FeatureSeparateJudge(PatsnapJudge):
    """单次拆解审计。

    继承 review_patent（逐 claim 分块 + 过长二分降级）与 _call_validated 的重试
    校验循环，只把 prompt_root 指向本 skill 的规则目录。

    不提供 Prompt 生成：被评对象是 HTTP 服务，其 Prompt 不可见也不可写。
    """

    def __init__(self, config=None, session=None):
        super().__init__(
            config=config,
            session=session,
            rules_version=RULES_VERSION,
            prompt_root=PROMPT_ROOT / RULES_VERSION,
        )

    def _prompt(self, name):
        """规则文件改名为 judge_single.md；vendor 侧仍按 judge_batch.md 请求。"""
        if name == "judge_batch.md":
            name = "judge_single.md"
        return super()._prompt(name)

    def generate_prompt_version(self, current_prompt, generic_issues):
        raise NotImplementedError(
            "本 skill 不生成 Prompt：被评服务的 Prompt 不可见且不可修改。"
        )

    def aggregate_prompt_issues(self, *args, **kwargs):
        raise NotImplementedError(
            "本 skill 不做跨专利聚合：每次评测只审计一次拆解。"
        )
