"""Load only this skill's declared settings from process env or a local .env.

Vendored from claim-decomposition-no-gt-eval. Keep in sync with the source.
"""

import os
from pathlib import Path
import re


ENV_LINE = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)\s*$"
)

# Security boundary: never copy arbitrary .env entries into the process environment.
# Only these documented keys are read, and callers receive a plain mapping.
ALLOWED_SETTINGS = frozenset({
    "CLAIM_DECOMPOSITION_JUDGE_API_URL",
    "CLAIM_DECOMPOSITION_JUDGE_API_TOKEN",
    "CLAIM_DECOMPOSITION_JUDGE_ALLOWED_HOSTS",
    "CLAIM_DECOMPOSITION_JUDGE_MODEL",
    "CLAIM_DECOMPOSITION_JUDGE_TIMEOUT",
    "CLAIM_DECOMPOSITION_JUDGE_RETRIES",
    "CLAIM_DECOMPOSITION_JUDGE_MAX_TOKENS",
    "CLAIM_DECOMPOSITION_JUDGE_TOKEN_HEADER",
    "CLAIM_DECOMPOSITION_JUDGE_TOKEN_PREFIX",
    "PATSNAP_API_BASE",
    "PATSNAP_KEY",
    "PATSNAP_ALLOWED_HOSTS",
    "CC_EVAL_DATA_DIR",
})


class EnvFileError(ValueError):
    pass


def default_env_path():
    """按 skill 目录优先、再向上查找的顺序定位 .env。

    原实现写死 parents[3]（项目根），换个安装位置就读不到配置。改为先看 skill
    自己的目录——将 .env.example 复制成 .env 填自己的 key 即可，不必
    依赖外层项目的文件；找不到再向上逐级找，这样本仓库原有的项目根 .env 仍然生效。
    """
    here = Path(__file__).resolve()
    package_dir = here.parents[1]        # vendor -> feature_separate_eval_mcp
    # Use a list so the lookup order stays explicit across supported Python versions.
    ancestors = list(here.parents)
    candidates = [package_dir / ".env"] + [
        parent / ".env" for parent in ancestors[2:6]
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # 都没有时指向 package 目录，报错信息就会引导到该填配置的位置。
    return package_dir / ".env"


def _parse_value(raw, line_number):
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            raise EnvFileError(f"unterminated quoted value on .env line {line_number}")
        value = value[1:-1]
        if quote == '"':
            value = (
                value.replace(r"\n", "\n")
                .replace(r"\r", "\r")
                .replace(r"\t", "\t")
                .replace(r'\"', '"')
                .replace(r"\\", "\\")
            )
        return value
    # An unquoted # starts a comment only when separated by whitespace.
    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    return value


def load_env_file(path=None, required=False, override=False):
    """Return ``(path, settings)`` without mutating ``os.environ``.

    Existing process values win unless ``override`` is true. Unknown keys in a
    shared project .env are ignored, preventing unrelated secrets from entering
    this skill's data flow.
    """
    env_path = Path(path).expanduser().resolve() if path else default_env_path()
    file_values = {}
    if not env_path.is_file():
        if required:
            raise EnvFileError(f"environment file does not exist: {env_path}")
        env_path = None
    else:
        for line_number, line in enumerate(
            env_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = ENV_LINE.match(line)
            if not match:
                raise EnvFileError(f"invalid .env assignment on line {line_number}")
            key = match.group("key")
            if key in ALLOWED_SETTINGS:
                file_values[key] = _parse_value(match.group("value"), line_number)

    settings = {}
    for key in ALLOWED_SETTINGS:
        process_value = os.getenv(key)
        if process_value is not None and not override:
            settings[key] = process_value
        elif key in file_values:
            settings[key] = file_values[key]
        elif process_value is not None:
            settings[key] = process_value
    return env_path, settings
