"""Settings read from environment variables.

The API token is only ever read from the environment. It is never logged,
echoed back in tool output, or written to disk.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

TRUE = {"1", "true", "yes", "on"}


class ConfigError(ValueError):
    """Raised when the environment does not describe a usable Dynatrace target."""


def _flag(env: dict[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in TRUE


def _int(env: dict[str, str], name: str, default: int) -> int:
    value = env.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _float(env: dict[str, str], name: str, default: float) -> float:
    value = env.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def normalize_base_url(raw: str, allow_http: bool = False) -> tuple[str, list[str]]:
    """Return the environment root (without ``/api/v2``) and any warnings.

    Accepts, for example:
      https://dynatrace.corp.example/e/abc-123          (Managed)
      https://dynatrace.corp.example/e/abc-123/api/v2/  (Managed, full API path)
      https://abc12345.live.dynatrace.com               (SaaS)
      https://abc12345.apps.dynatrace.com               (SaaS platform URL, rewritten)
    """
    warnings: list[str] = []
    raw = (raw or "").strip()
    if not raw:
        raise ConfigError(
            "DT_BASE_URL is not set. Use https://<host>/e/<environment-id> for Managed "
            "or https://<environment-id>.live.dynatrace.com for SaaS."
        )
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        raise ConfigError(f"DT_BASE_URL is not a valid URL: {raw!r}")
    if parts.username or parts.password:
        raise ConfigError("DT_BASE_URL must not contain credentials; use DT_API_TOKEN.")
    host = parts.hostname or ""
    if parts.scheme == "http" and not allow_http and host not in ("localhost", "127.0.0.1", "::1"):
        raise ConfigError(
            "DT_BASE_URL uses plain http, which would send the API token unencrypted. "
            "Use https, or set DT_ALLOW_HTTP=true if you really need this."
        )

    netloc = parts.netloc
    if host.endswith(".apps.dynatrace.com"):
        new_host = host.replace(".apps.dynatrace.com", ".live.dynatrace.com")
        netloc = netloc.replace(host, new_host)
        warnings.append(
            f"DT_BASE_URL points at the platform UI ({host}); the classic API v2 lives at "
            f"{new_host}, so that host is used instead."
        )

    path = re.sub(r"/+", "/", parts.path).rstrip("/")
    for suffix in ("/api/v2", "/api/v1", "/api"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    # Managed UI links look like /e/<env>/#... or /e/<env>/ui/...; keep only /e/<env>.
    managed = re.match(r"^(/e/[^/]+)", path)
    if managed and path != managed.group(1):
        warnings.append(f"Ignoring the path after {managed.group(1)} in DT_BASE_URL.")
        path = managed.group(1)
    if not managed and host and not host.endswith(".dynatrace.com") and path in ("", "/"):
        warnings.append(
            "DT_BASE_URL has no /e/<environment-id> path. Dynatrace Managed needs it "
            "(https://<host>/e/<environment-id>); ignore this if your host is a SaaS or "
            "custom-domain environment."
        )
    return urlunsplit((parts.scheme, netloc, path, "", "")), warnings


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_token: str = field(repr=False)
    ca_bundle: str | None = None
    insecure_tls: bool = False
    proxy: str | None = None
    timeout: float = 30.0
    max_retries: int = 2
    enable_write: bool = False
    redact: bool = True
    max_output_chars: int = 60_000
    max_log_line_chars: int = 2_000
    platform_url: str | None = None
    platform_token: str | None = field(default=None, repr=False)
    disabled_tools: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def api_root(self) -> str:
        return self.base_url + "/api/v2"

    @property
    def dql_enabled(self) -> bool:
        return bool(self.platform_url and self.platform_token)

    @property
    def deployment(self) -> str:
        if "/e/" in self.base_url:
            return "managed"
        if ".dynatrace.com" in self.base_url:
            return "saas"
        return "unknown"

    def secrets(self) -> list[str]:
        return [s for s in (self.api_token, self.platform_token) if s]

    def describe(self) -> dict:
        """Settings that are safe to show to the user (no secrets)."""
        return {
            "base_url": self.base_url,
            "deployment": self.deployment,
            "api_token": "set" if self.api_token else "missing",
            "ca_bundle": self.ca_bundle,
            "insecure_tls": self.insecure_tls,
            "proxy": _strip_userinfo(self.proxy) if self.proxy else None,
            "timeout_seconds": self.timeout,
            "write_tools_enabled": self.enable_write,
            "redaction": self.redact,
            "dql_enabled": self.dql_enabled,
            "disabled_tools": list(self.disabled_tools),
            "warnings": list(self.warnings),
        }


def _strip_userinfo(url: str) -> str:
    parts = urlsplit(url)
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, "***@" + netloc, parts.path, "", ""))
    return url


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    allow_http = _flag(env, "DT_ALLOW_HTTP")
    base_url, warnings = normalize_base_url(env.get("DT_BASE_URL", ""), allow_http)

    token = env.get("DT_API_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "DT_API_TOKEN is not set. Create an API token in Dynatrace (Access tokens) "
            "and export it in the environment Cursor starts the server from."
        )
    if any(c.isspace() for c in token):
        raise ConfigError("DT_API_TOKEN contains whitespace; paste the token value only.")
    if token.lower().startswith(("api-token", "bearer")):
        raise ConfigError("DT_API_TOKEN must be the bare token, without an 'Api-Token ' prefix.")

    ca_bundle = env.get("DT_CA_BUNDLE", "").strip() or None
    if ca_bundle and not os.path.isfile(ca_bundle):
        raise ConfigError(f"DT_CA_BUNDLE file not found: {ca_bundle}")
    insecure = _flag(env, "DT_INSECURE_TLS")
    if insecure:
        warnings.append(
            "DT_INSECURE_TLS=true: TLS certificates are NOT verified. Prefer DT_CA_BUNDLE "
            "with your company's root CA."
        )

    platform_url = env.get("DT_PLATFORM_URL", "").strip().rstrip("/") or None
    platform_token = env.get("DT_PLATFORM_TOKEN", "").strip() or None
    if platform_url and not platform_url.startswith("https://"):
        raise ConfigError("DT_PLATFORM_URL must use https.")
    if bool(platform_url) != bool(platform_token):
        warnings.append("DQL needs both DT_PLATFORM_URL and DT_PLATFORM_TOKEN; DQL tools are off.")

    return Settings(
        base_url=base_url,
        api_token=token,
        ca_bundle=ca_bundle,
        insecure_tls=insecure,
        proxy=env.get("DT_PROXY", "").strip() or None,
        timeout=_float(env, "DT_TIMEOUT", 30.0),
        max_retries=max(0, _int(env, "DT_MAX_RETRIES", 2)),
        enable_write=_flag(env, "DT_ENABLE_WRITE"),
        redact=_flag(env, "DT_REDACT", True),
        max_output_chars=max(5_000, _int(env, "DT_MAX_OUTPUT_CHARS", 60_000)),
        max_log_line_chars=max(200, _int(env, "DT_MAX_LOG_LINE_CHARS", 2_000)),
        platform_url=platform_url,
        platform_token=platform_token,
        disabled_tools=tuple(t.strip() for t in env.get("DT_DISABLE_TOOLS", "").split(",") if t.strip()),
        warnings=tuple(warnings),
    )
