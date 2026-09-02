"""Shared outbound-network validation for the skill's two declared services."""

from urllib.parse import urlsplit


def validate_https_endpoint(value, allowed_hosts, label, error_type=ValueError):
    """Return a normalized HTTPS endpoint after an exact host allowlist check."""
    endpoint = str(value or "").strip()
    if not endpoint:
        raise error_type(f"{label} is required")
    parsed = urlsplit(endpoint)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise error_type(f"{label} must be an HTTPS URL")
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
