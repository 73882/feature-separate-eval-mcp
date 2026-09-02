"""PatSnap Gateway client for source-grounded claim-decomposition review.

Originally vendored from claim-decomposition-no-gt-eval, then reduced to the part
this skill actually runs: the gateway call, the retry-on-schema-violation loop,
and the per-patent review validators behind review_patent.

Two halves of the source were removed because nothing here calls them — prompt
generation (the evaluated service's prompt is neither visible nor writable from
this project) and cross-patent aggregation (each evaluation audits a single
decomposition). Restore them from the source project if either is needed again.

judge_client.py extends REVIEW_LABELS and ROOT_CAUSES at import time to register
the GT-calibrated label NOT_INDEPENDENTLY_COMPARABLE and the postprocess-facing
recommendation targets. Keep those whitelists extensible when syncing upstream.
"""

from dataclasses import dataclass, field
from copy import deepcopy
import json
from pathlib import Path
import re
import threading
import time

import requests

from .network_policy import allow_insecure_http, validate_endpoint


JUDGE_RULES_VERSION = "judge_rules_v5"
MAX_CLAIMS_PER_JUDGE_CALL = 4
# 规则目录的默认位置。本 skill 通过构造参数覆盖为 prompts/gt_calibrated_rules_v1。
PROMPT_ROOT = Path(__file__).resolve().parent / "prompts" / JUDGE_RULES_VERSION
ROOT_CAUSES = {"prompt", "model", "parser", "other"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}


class JudgeConfigurationError(RuntimeError):
    pass


class JudgeError(RuntimeError):
    pass


class JudgeLengthError(JudgeError):
    """The gateway stopped before producing a complete Judge response."""


class JudgeTransportError(JudgeError):
    """The gateway call itself failed, so no response reached the validators."""


class JudgePartialLengthError(JudgeLengthError):
    """Some claims exceeded the token limit; the rest were reviewed normally.

    Carries the successful reviews so the caller can keep them instead of
    discarding a whole patent because one claim was decomposed too finely.
    """

    def __init__(self, claim_keys, reviews):
        self.claim_keys = list(claim_keys)
        self.reviews = reviews
        super().__init__(
            "Judge output reached the token limit for "
            f"{len(self.claim_keys)} claim(s) even individually: "
            + ", ".join(self.claim_keys)
        )


class JudgeValidationError(JudgeError):
    """A model response was received but did not satisfy a schema contract."""

    def __init__(self, result_name, attempts):
        last_error = attempts[-1].get("validation_error", "unknown error")
        super().__init__(
            f"{result_name} failed schema validation after "
            f"{len(attempts)} attempt(s): {last_error}"
        )
        self.result_name = result_name
        self.attempts = deepcopy(attempts)


REVIEW_LABELS = {
    "SUPPORTED_COMPLETE_FACT", "INCOMPLETE_FRAGMENT",
    "MULTIPLE_INDEPENDENT_FACTS", "RELATION_LOSS", "CONDITION_LOSS",
    "UNSUPPORTED_CONTENT", "GENERIC_OR_REFERENCE_ONLY", "DUPLICATE_FACT",
    "AMBIGUOUS",
}


@dataclass(frozen=True)
class JudgeConfig:
    api_url: str
    api_token: str = field(repr=False)
    model: str = "gpt-5.5"
    timeout: int = 180
    retries: int = 2
    max_tokens: int = 12000
    token_header: str = "Authorization"
    token_prefix: str = ""

    @classmethod
    def from_mapping(cls, settings, model=None):
        api_url = validate_endpoint(
            settings.get("CLAIM_DECOMPOSITION_JUDGE_API_URL"),
            settings.get("CLAIM_DECOMPOSITION_JUDGE_ALLOWED_HOSTS"),
            "CLAIM_DECOMPOSITION_JUDGE_API_URL",
            JudgeConfigurationError,
            allow_http=allow_insecure_http(
                settings.get("CC_EVAL_ALLOW_INSECURE_HTTP")
            ),
        )
        api_token = str(
            settings.get("CLAIM_DECOMPOSITION_JUDGE_API_TOKEN") or ""
        ).strip()
        if not api_token:
            raise JudgeConfigurationError(
                "CLAIM_DECOMPOSITION_JUDGE_API_TOKEN is required"
            )
        token_header = str(settings.get(
            "CLAIM_DECOMPOSITION_JUDGE_TOKEN_HEADER"
        ) or "Authorization").strip()
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", token_header)
            or token_header.lower() in {"content-length", "content-type", "host"}
        ):
            raise JudgeConfigurationError(
                "CLAIM_DECOMPOSITION_JUDGE_TOKEN_HEADER is not a safe header name"
            )
        return cls(
            api_url=api_url,
            api_token=api_token,
            model=model or str(settings.get(
                "CLAIM_DECOMPOSITION_JUDGE_MODEL") or "gpt-5.5"
            ).strip(),
            timeout=_bounded_int(
                settings, "CLAIM_DECOMPOSITION_JUDGE_TIMEOUT", 180, 300
            ),
            retries=_bounded_int(
                settings, "CLAIM_DECOMPOSITION_JUDGE_RETRIES", 2, 3
            ),
            max_tokens=_bounded_int(
                settings, "CLAIM_DECOMPOSITION_JUDGE_MAX_TOKENS", 12000, 20000
            ),
            token_header=token_header,
            token_prefix=str(settings.get(
                "CLAIM_DECOMPOSITION_JUDGE_TOKEN_PREFIX") or ""
            ).strip(),
        )


def _bounded_int(settings, name, default, maximum):
    raw = str(settings.get(name) or default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise JudgeConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise JudgeConfigurationError(f"{name} must be greater than zero")
    if value > maximum:
        raise JudgeConfigurationError(f"{name} must not exceed {maximum}")
    return value


def _prompt(name):
    path = PROMPT_ROOT / name
    if not path.is_file():
        raise JudgeConfigurationError(f"Judge rules file is missing: {name}")
    return path.read_text(encoding="utf-8")


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise JudgeError("Judge output does not contain a JSON object")
        try:
            payload = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise JudgeError("Judge output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise JudgeError("Judge output must be a JSON object")
    return payload


def _extract_message_content(payload):
    data = payload.get("data")
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = payload.get("output")
    if isinstance(output, list):
        parts = []
        for item in output:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    parts.append(content["text"])
        if parts:
            return "".join(parts)

    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = [
            item["text"] for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if parts:
            return "".join(parts)
    return None


class PatsnapJudge:
    def __init__(
        self,
        config=None,
        session=None,
        rules_version=JUDGE_RULES_VERSION,
        prompt_root=None,
    ):
        if config is None:
            raise JudgeConfigurationError("an explicit JudgeConfig is required")
        self.config = config
        self.session = session
        self.rules_version = rules_version
        self.prompt_root = (
            Path(prompt_root).resolve()
            if prompt_root is not None
            else Path(__file__).resolve().parent / "prompts" / rules_version
        )
        self._thread_sessions = threading.local()
        self._review_cache = {}

    def _prompt(self, name):
        path = self.prompt_root / name
        if not path.is_file():
            raise JudgeConfigurationError(
                f"Judge rules file is missing for {self.rules_version}: {name}"
            )
        return path.read_text(encoding="utf-8")

    def _session(self):
        if self.session is not None:
            return self.session
        session = getattr(self._thread_sessions, "session", None)
        if session is None:
            session = requests.Session()
            self._thread_sessions.session = session
        return session

    def _headers(self):
        value = self.config.api_token
        if self.config.token_prefix:
            value = f"{self.config.token_prefix} {value}"
        return {
            "Content-Type": "application/json",
            self.config.token_header: value,
        }

    def _call(self, prompt):
        body = {
            "message": prompt,
            "model": self.config.model,
            "stream": False,
            "max_tokens": self.config.max_tokens,
        }
        # gpt-5.5 only accepts its default temperature. Older gateway models
        # can still use temperature=0 for deterministic review.
        if self.config.model.lower() != "gpt-5.5":
            body["temperature"] = 0
        last_error = None
        for attempt in range(self.config.retries):
            try:
                response = self._session().post(
                    self.config.api_url,
                    json=body,
                    headers=self._headers(),
                    timeout=self.config.timeout,
                    allow_redirects=False,
                    verify=True,
                )
                if response.status_code in {401, 403}:
                    raise JudgeConfigurationError(
                        f"Judge authentication failed with HTTP {response.status_code}"
                    )
                if response.status_code != 200:
                    last_error = JudgeError(
                        f"Judge returned HTTP {response.status_code}"
                    )
                else:
                    payload = response.json()
                    if not isinstance(payload, dict):
                        last_error = JudgeError("Judge gateway returned a non-object response")
                        continue
                    if payload.get("error_code") not in (None, 0, "0"):
                        reason = payload.get("msg") or payload.get("message")
                        reason = " ".join(str(reason).split())[:300] if reason else ""
                        last_error = JudgeError(
                            f"Judge gateway returned error_code {payload.get('error_code')}"
                            + (f": {reason}" if reason else "")
                        )
                    else:
                        data = payload.get("data")
                        if (
                            isinstance(data, dict)
                            and data.get("finish_reason") == "length"
                        ):
                            usage = data.get("usage") or {}
                            raise JudgeLengthError(
                                "Judge output reached the token limit"
                                f" (completion_tokens="
                                f"{usage.get('completion_tokens')})"
                            )
                        message = _extract_message_content(payload)
                        if not isinstance(message, str) or not message.strip():
                            finish_reason = (
                                data.get("finish_reason")
                                if isinstance(data, dict) else None
                            )
                            usage = (
                                data.get("usage")
                                if isinstance(data, dict) else payload.get("usage")
                            ) or {}
                            last_error = JudgeError(
                                "Judge gateway response has no supported text content"
                                f" (finish_reason={finish_reason}, "
                                f"completion_tokens={usage.get('completion_tokens')})"
                            )
                        else:
                            try:
                                result = _extract_json(message)
                            except JudgeError as exc:
                                last_error = exc
                            else:
                                usage = (
                                    data.get("usage")
                                    if isinstance(data, dict)
                                    else payload.get("usage")
                                )
                                return result, usage or {}
            except JudgeConfigurationError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = JudgeTransportError(
                    f"Judge request failed: {type(exc).__name__}"
                    + (
                        f" after {self.config.timeout}s; raise "
                        "CLAIM_DECOMPOSITION_JUDGE_TIMEOUT"
                        if isinstance(exc, requests.Timeout) else ""
                    )
                )
            if attempt + 1 < self.config.retries:
                time.sleep(2 ** attempt)
        raise last_error or JudgeError("Judge request failed")

    def _call_validated(self, prompt, validator, result_name, repair_context=None):
        """Retry when the gateway succeeds but the model violates its JSON contract."""
        last_error = None
        last_result = None
        attempts = []
        for attempt in range(self.config.retries):
            retry_prompt = prompt
            if last_error is not None:
                retry_prompt += (
                    "\n\n上一次响应未通过结构校验："
                    f"{last_error}。请严格按照原要求重新输出完整 JSON；"
                    "不要输出 Markdown、解释或省略任何必填字段。"
                )
                if repair_context:
                    retry_prompt += (
                        "\n本次必须覆盖的结构键："
                        + json.dumps(repair_context, ensure_ascii=False)
                    )
                if last_result is not None:
                    # 长度超限时不能让模型原样保留 prompt 文本，否则重试必然再次
                    # 超限；那种情况要求它改写压缩。
                    instruction = (
                        "\n上一次无效响应如下。其 prompt 过长，必须改写压缩到预算"
                        "以内（合并重复规则、删除冗余限定），同时保持规则效力：\n"
                        if "character budget" in str(last_error)
                        else "\n上一次无效响应如下。保留其中的完整 prompt 文本，"
                        "只修复 JSON 结构、issue_key 映射和遗漏字段：\n"
                    )
                    retry_prompt += instruction + json.dumps(
                        last_result, ensure_ascii=False
                    )
            try:
                result, usage = self._call(retry_prompt)
            except (JudgeLengthError, JudgeTransportError):
                # The gateway never returned a body, so this is not a schema
                # problem. Surface the transport cause instead of relabelling it.
                raise
            except JudgeError as exc:
                last_error = exc
                attempts.append({
                    "attempt": attempt + 1,
                    "validation_error": str(exc),
                    "response": None,
                    "usage": {},
                })
                continue
            try:
                validated = validator(result)
                return (result if validated is None else validated), usage
            except JudgeError as exc:
                last_error = exc
                last_result = result
                attempts.append({
                    "attempt": attempt + 1,
                    "validation_error": str(exc),
                    "response": result,
                    "usage": usage,
                })
        raise JudgeValidationError(result_name, attempts)

    def review_claim(self, claim_key, claim_text, features, deterministic):
        return self.review_patent([{
            "claim_key": claim_key,
            "claim_text": claim_text,
            "features": features,
            "deterministic": deterministic,
        }])[claim_key]

    def review_patent(self, claims):
        """Review one patent in bounded claim chunks and merge the results.

        A claim whose audit output cannot fit the token limit even on its own is a
        real property of the data (one round_05 claim was decomposed into 240
        features). Such a claim is reported as its own failure while the rest of
        the patent still gets reviewed — otherwise a single oversized claim
        fails all 51 and costs the whole batch.
        """
        reviews = {}
        oversized = []
        for start in range(0, len(claims), MAX_CLAIMS_PER_JUDGE_CALL):
            batch = claims[start:start + MAX_CLAIMS_PER_JUDGE_CALL]
            merged, too_long = self._review_patent_batch_adaptive(batch)
            reviews.update(merged)
            oversized.extend(too_long)
        if oversized:
            raise JudgePartialLengthError(oversized, reviews)
        return reviews

    def _review_patent_batch_adaptive(self, claims):
        """Return (reviews, oversized_claim_keys) instead of failing the patent."""
        try:
            return self._review_patent_batch(claims), []
        except JudgeLengthError:
            if len(claims) == 1:
                return {}, [claims[0]["claim_key"]]
            midpoint = len(claims) // 2
            reviews, oversized = self._review_patent_batch_adaptive(
                claims[:midpoint]
            )
            more, more_oversized = self._review_patent_batch_adaptive(
                claims[midpoint:]
            )
            reviews.update(more)
            return reviews, oversized + more_oversized

    def _review_patent_batch(self, claims):
        payload_claims = []
        expected = {}
        for item in claims:
            claim_key = item["claim_key"]
            features = [
                {"feature_id": f"{claim_key}_A{index:03d}", "text": text}
                for index, text in enumerate(item["features"], start=1)
            ]
            payload_claims.append({
                "claim_key": claim_key,
                "claim_text": item["claim_text"],
                "features": features,
                "deterministic_checks": item["deterministic"],
            })
            expected[claim_key] = {
                "claim_text": item["claim_text"],
                "feature_ids": [feature["feature_id"] for feature in features],
            }
        payload = {"claims": payload_claims}
        cache_key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if cache_key in self._review_cache:
            cached = deepcopy(self._review_cache[cache_key])
            for review in cached.values():
                review["cache_hit"] = True
            return cached
        prompt = self._prompt("judge_batch.md").replace(
            "{evaluation_payload}",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        result, usage = self._call_validated(
            prompt,
            lambda value: _validate_patent_review(value, expected),
            "patent Judge review",
        )
        reviews = {}
        for row in result["claim_reviews"]:
            claim_key = row["claim_key"]
            review = {key: value for key, value in row.items() if key != "claim_key"}
            review["usage"] = usage
            review["judge_model"] = self.config.model
            review["judge_rules_version"] = self.rules_version
            review["cache_hit"] = False
            review["judge_call_scope"] = "patent"
            reviews[claim_key] = review
        self._review_cache[cache_key] = deepcopy(reviews)
        return reviews




def _validate_patent_review(result, expected):
    rows = result.get("claim_reviews")
    if not isinstance(rows, list):
        raise JudgeError("patent Judge result.claim_reviews must be an array")
    actual_keys = [
        row.get("claim_key") for row in rows if isinstance(row, dict)
    ]
    if len(actual_keys) != len(rows) or sorted(actual_keys) != sorted(expected):
        raise JudgeError("patent Judge must review every input claim exactly once")
    for row in rows:
        claim_key = row["claim_key"]
        specification = expected[claim_key]
        _validate_review_v2(
            row,
            specification["feature_ids"],
            specification["claim_text"],
        )
    return result


def _normalized_evidence(text):
    return "".join(str(text).split())


def _require_source_evidence(evidence, claim_text, message):
    if not isinstance(evidence, str) or not evidence.strip():
        raise JudgeError(message)
    if _normalized_evidence(evidence) not in _normalized_evidence(claim_text):
        raise JudgeError("Judge claim_evidence must be copied from the source claim")


def _validate_review_common(result, expected_feature_ids, claim_text):
    dimensions = result.get("dimensions")
    if not isinstance(dimensions, dict):
        raise JudgeError("Judge result.dimensions must be an object")
    required = {
        "source_faithfulness", "completeness", "atomicity",
        "relation_integrity", "reference_integrity",
    }
    if not required.issubset(dimensions):
        raise JudgeError("Judge result is missing required dimensions")
    for key in required:
        value = dimensions[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 5:
            raise JudgeError(f"Judge dimension {key} must be between 0 and 5")
    if not isinstance(result.get("feature_reviews"), list):
        raise JudgeError("Judge result.feature_reviews must be an array")
    for review in result["feature_reviews"]:
        if not isinstance(review, dict):
            raise JudgeError("each Judge feature review must be an object")
        labels = review.get("labels")
        if not isinstance(labels, list) or not set(labels).issubset(REVIEW_LABELS):
            raise JudgeError("Judge feature review contains an invalid label")
        if review.get("severity") not in {"critical", "major", "minor", "info"}:
            raise JudgeError("Judge feature review contains an invalid severity")
        _require_source_evidence(
            review.get("claim_evidence"),
            claim_text,
            "each feature review must contain claim_evidence",
        )
    actual_ids = [review.get("feature_id") for review in result["feature_reviews"]]
    if not all(isinstance(feature_id, str) for feature_id in actual_ids):
        raise JudgeError("each Judge feature review must contain feature_id")
    if sorted(actual_ids) != sorted(expected_feature_ids):
        raise JudgeError("Judge must review every input feature exactly once")


def _required_text(item, keys, message):
    if not all(
        isinstance(item.get(key), str) and item[key].strip()
        for key in keys
    ):
        raise JudgeError(message)


def _validate_review_v2(result, expected_feature_ids, claim_text):
    _validate_review_common(result, expected_feature_ids, claim_text)
    gaps = result.get("coverage_gaps")
    if not isinstance(gaps, list):
        raise JudgeError("Judge result.coverage_gaps must be an array")
    gap_ids = set()
    for gap in gaps:
        if not isinstance(gap, dict):
            raise JudgeError("each coverage gap must be an object")
        _required_text(
            gap,
            ("gap_id", "claim_evidence", "missing_fact", "reason"),
            "coverage gap is missing required text",
        )
        if gap["gap_id"] in gap_ids:
            raise JudgeError("coverage gap IDs must be unique")
        gap_ids.add(gap["gap_id"])
        if gap.get("severity") not in {"critical", "major", "minor"}:
            raise JudgeError("coverage gap contains an invalid severity")
        if gap.get("root_cause") not in ROOT_CAUSES:
            raise JudgeError("coverage gap contains an invalid root cause")
        if gap.get("root_cause_confidence") not in CONFIDENCE_LEVELS:
            raise JudgeError("coverage gap contains an invalid confidence")
        _require_source_evidence(
            gap.get("claim_evidence"),
            claim_text,
            "coverage gap must contain claim_evidence",
        )

    issues = result.get("issues")
    if not isinstance(issues, list):
        raise JudgeError("Judge result.issues must be an array")
    issue_ids = set()
    for issue in issues:
        if not isinstance(issue, dict):
            raise JudgeError("each Judge issue must be an object")
        _required_text(
            issue,
            ("issue_id", "issue_type", "claim_evidence", "reason", "regression_risk"),
            "Judge issue is missing required text",
        )
        if issue["issue_id"] in issue_ids:
            raise JudgeError("Judge issue IDs must be unique")
        issue_ids.add(issue["issue_id"])
        if issue.get("severity") not in {"critical", "major", "minor"}:
            raise JudgeError("Judge issue contains an invalid severity")
        if issue.get("root_cause") not in ROOT_CAUSES:
            raise JudgeError("Judge issue contains an invalid root cause")
        if issue.get("root_cause_confidence") not in CONFIDENCE_LEVELS:
            raise JudgeError("Judge issue contains an invalid confidence")
        _require_source_evidence(
            issue.get("claim_evidence"),
            claim_text,
            "Judge issue must contain claim_evidence",
        )
        feature_ids = issue.get("feature_ids")
        if not isinstance(feature_ids, list) or not set(feature_ids).issubset(
            set(expected_feature_ids)
        ):
            raise JudgeError("Judge issue references an unknown feature ID")
        linked_gaps = issue.get("gap_ids")
        if not isinstance(linked_gaps, list) or not set(linked_gaps).issubset(gap_ids):
            raise JudgeError("Judge issue references an unknown coverage gap")

    recommendations = result.get("recommendations")
    if not isinstance(recommendations, list):
        raise JudgeError("Judge result.recommendations must be an array")
    recommendation_ids = set()
    for recommendation in recommendations:
        if not isinstance(recommendation, dict):
            raise JudgeError("each Judge recommendation must be an object")
        _required_text(
            recommendation,
            ("recommendation_id", "suggestion", "risk", "guardrail"),
            "Judge recommendation is missing required text",
        )
        if recommendation["recommendation_id"] in recommendation_ids:
            raise JudgeError("Judge recommendation IDs must be unique")
        recommendation_ids.add(recommendation["recommendation_id"])
        linked = recommendation.get("issue_ids")
        if not isinstance(linked, list) or not linked or not set(linked).issubset(issue_ids):
            raise JudgeError("Judge recommendation must link known issue IDs")
        target = recommendation.get("target")
        if target not in ROOT_CAUSES:
            raise JudgeError("Judge recommendation contains an invalid target")
        if recommendation.get("confidence") not in CONFIDENCE_LEVELS:
            raise JudgeError("Judge recommendation contains an invalid confidence")
        linked_issues = [item for item in issues if item["issue_id"] in linked]
        if target == "prompt" and any(
            item["root_cause"] != "prompt"
            or item["root_cause_confidence"] == "low"
            for item in linked_issues
        ):
            raise JudgeError(
                "prompt recommendation requires prompt-attributed, non-low-confidence issues"
            )
    linked_gap_ids = {
        value for issue in issues for value in issue.get("gap_ids", [])
    }
    if linked_gap_ids != gap_ids:
        raise JudgeError("every coverage gap must be linked by a Judge issue")
    prompt_issue_ids = {
        item["issue_id"]
        for item in issues
        if item["root_cause"] == "prompt"
        and item["root_cause_confidence"] in {"high", "medium"}
    }
    recommended_prompt_issue_ids = {
        issue_id
        for recommendation in recommendations
        if recommendation.get("target") == "prompt"
        for issue_id in recommendation.get("issue_ids", [])
    }
    if not prompt_issue_ids.issubset(recommended_prompt_issue_ids):
        raise JudgeError(
            "each non-low-confidence prompt issue requires a linked prompt recommendation"
        )


def proxy_score(dimensions):
    weights = {
        "source_faithfulness": 0.30,
        "completeness": 0.25,
        "atomicity": 0.20,
        "relation_integrity": 0.15,
        "reference_integrity": 0.10,
    }
    return round(
        sum(float(dimensions[key]) * weight for key, weight in weights.items())
        / 5
        * 100,
        2,
    )















