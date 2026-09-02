"""Shared outbound-network validation for the skill's two declared services."""

from urllib.parse import urlsplit


def allow_insecure_http(value):
    """Return whether the explicit internal-network HTTP opt-in is enabled."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def validate_endpoint(
    value,
    allowed_hosts,
    label,
    error_type=ValueError,
    allow_http=False,
):
    """Return a normalized endpoint after scheme and exact-host checks."""
    endpoint = str(value or "").strip()
    if not endpoint:
        raise error_type(f"{label} is required")
    parsed = urlsplit(endpoint)
    scheme = parsed.scheme.lower()
    if not parsed.hostname or scheme not in {"http", "https"}:
        raise error_type(f"{label} must be an HTTP(S) URL")
    if scheme == "http" and not allow_http:
        raise error_type(
            f"{label} uses HTTP; set CC_EVAL_ALLOW_INSECURE_HTTP=true "
            "only for a trusted internal endpoint"
        )
    if parsed.username or parsed.password or parsed.fragment:
        raise error_type(f"{label} must not contain credentials or a fragment")

    hosts = {
        item.strip().lower().rstrip(".")
        for item in str(allowed_hosts or "").split(",")
        if item.strip()
    }
    if not hosts:
        raise error_type(f"{label}_ALLOWED_HOSTS is required")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname not in hosts:
        raise error_type(
            f"{label} host {hostname!r} is not in its exact host allowlist"
        )
    return endpoint.rstrip("/")
