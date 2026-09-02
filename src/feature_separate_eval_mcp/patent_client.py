"""Minimal PatSnap client used only to retrieve the CLMS field by patent number."""

from dataclasses import dataclass, field
import re

import requests

from .vendor.network_policy import validate_https_endpoint


class PatentConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PatentConfig:
    base_url: str
    api_key: str = field(repr=False)
    timeout: int = 30

    @classmethod
    def from_mapping(cls, settings):
        base_url = validate_https_endpoint(
            settings.get("PATSNAP_API_BASE"),
            settings.get("PATSNAP_ALLOWED_HOSTS"),
            "PATSNAP_API_BASE",
            PatentConfigurationError,
        )
        api_key = str(settings.get("PATSNAP_KEY") or "").strip()
        if not api_key:
            raise PatentConfigurationError("PATSNAP_KEY is required")
        return cls(base_url=base_url, api_key=api_key)


class PatentClient:
    """One-endpoint client; it cannot call search, files, or other APIs."""

    def __init__(self, config, session=None):
        self.config = config
        self.session = session or requests.Session()

    def query_patent_fields(self, fields, pns, lang="cn"):
        if fields != ["CLMS"]:
            raise ValueError("this client permits only the CLMS field")
        response = self.session.post(
            f"{self.config.base_url}/api/patent/fields/query",
            json={"fields": ["CLMS"], "pns": list(pns), "lang": lang},
            headers={
                "Content-Type": "application/json",
                "PATSNAP_KEY": self.config.api_key,
            },
            timeout=self.config.timeout,
            allow_redirects=False,
            verify=True,
        )
        if response.status_code != 200:
            raise requests.HTTPError(
                f"PatSnap returned HTTP {response.status_code}"
            )
        result = response.json()
        self._clean_clms(result)
        return result

    @classmethod
    def _clean_clms(cls, result):
        data = result.get("data") if isinstance(result, dict) else None
        entries = [data] if isinstance(data, dict) else data
        if not isinstance(entries, list):
            return
        for entry in entries:
            fields = entry.get("fields") if isinstance(entry, dict) else None
            if not isinstance(fields, dict) or "CLMS" not in fields:
                continue
            value = fields["CLMS"]
            if isinstance(value, list):
                fields["CLMS"] = [
                    cls._strip_html(item) if isinstance(item, str) else item
                    for item in value
                ]
            elif isinstance(value, str):
                fields["CLMS"] = cls._strip_html(value)

    @staticmethod
    def _strip_html(text):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text)).strip()
