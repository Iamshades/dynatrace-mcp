"""Thin async HTTP client for the Dynatrace classic API v2 (and optional Grail DQL)."""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any

import httpx

from . import __version__
from .config import Settings
from .redact import Redactor

log = logging.getLogger("dynatrace_mcp")

RETRY_STATUS = {429, 502, 503, 504}

# Scope that each API path needs; used to turn a bare 403 into an actionable hint.
SCOPE_HINTS = [
    ("/logs", "logs.read"),
    ("/problems", "problems.read (problems.write for comments/close)"),
    ("/events", "events.read"),
    ("/entities", "entities.read"),
    ("/entityTypes", "entities.read"),
    ("/metrics", "metrics.read"),
    ("/securityProblems", "securityProblems.read"),
    ("/slo", "slo.read"),
]


class DynatraceError(Exception):
    def __init__(self, status: int | None, message: str, path: str = "", hint: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.path = path
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.message, "path": self.path}
        if self.status is not None:
            out["status"] = self.status
        if self.hint:
            out["hint"] = self.hint
        return out

    def __str__(self) -> str:
        bits = [f"Dynatrace API error ({self.status})" if self.status else "Dynatrace request failed"]
        bits.append(f"{self.path}: {self.message}")
        if self.hint:
            bits.append(f"Hint: {self.hint}")
        return " ".join(bits)


def _hint(status: int | None, path: str) -> str | None:
    if status == 401:
        return "The token was rejected. Check DT_API_TOKEN and that it belongs to this environment."
    if status == 403:
        for prefix, scope in SCOPE_HINTS:
            if path.startswith(prefix):
                return f"The token lacks a scope. This endpoint needs: {scope}."
        return "The token lacks a required scope."
    if status == 404:
        return "Not found. Check the id, and that DT_BASE_URL includes /e/<environment-id> on Managed."
    if status == 429:
        return "Rate limited by Dynatrace; retry later or narrow the time range."
    return None


def _ssl_verify(settings: Settings) -> bool | ssl.SSLContext:
    if settings.insecure_tls:
        return False
    if settings.ca_bundle:
        return ssl.create_default_context(cafile=settings.ca_bundle)
    return True


class DynatraceClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.redactor = Redactor(settings.secrets(), enabled=settings.redact)
        common: dict[str, Any] = {
            "timeout": settings.timeout,
            "verify": _ssl_verify(settings),
            "follow_redirects": False,  # never replay the token to another host
            "transport": transport,
        }
        if settings.proxy:
            common["proxy"] = settings.proxy
        headers = {"Accept": "application/json; charset=utf-8",
                   "User-Agent": f"dynatrace-mcp/{__version__}"}
        self._http = httpx.AsyncClient(
            base_url=settings.api_root,
            headers={**headers, "Authorization": f"Api-Token {settings.api_token}"},
            **common,
        )
        self._platform: httpx.AsyncClient | None = None
        if settings.dql_enabled:
            self._platform = httpx.AsyncClient(
                base_url=settings.platform_url or "",
                headers={**headers, "Authorization": f"Bearer {settings.platform_token}"},
                **common,
            )

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._platform:
            await self._platform.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request(self._http, "GET", path, params=params)

    async def post(self, path: str, body: dict[str, Any], params: dict[str, Any] | None = None) -> Any:
        return await self._request(self._http, "POST", path, params=params, json=body)

    async def platform_post(self, path: str, body: dict[str, Any], params: dict[str, Any] | None = None) -> Any:
        if not self._platform:
            raise DynatraceError(None, "DQL is not configured", path,
                                 "Set DT_PLATFORM_URL and DT_PLATFORM_TOKEN to enable DQL.")
        return await self._request(self._platform, "POST", path, params=params, json=body)

    async def platform_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._platform:
            raise DynatraceError(None, "DQL is not configured", path)
        return await self._request(self._platform, "GET", path, params=params)

    async def _request(self, http: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> Any:
        params = kwargs.pop("params", None)
        if params:
            params = {k: v for k, v in params.items() if v is not None and v != "" and v != []}
        attempt = 0
        while True:
            try:
                resp = await http.request(method, path, params=params, **kwargs)
            except httpx.TimeoutException as exc:
                raise DynatraceError(None, f"timed out after {self.settings.timeout}s", path,
                                     "Narrow the time range or raise DT_TIMEOUT.") from exc
            except httpx.ConnectError as exc:
                msg = self.redactor.text(str(exc))
                hint = None
                if "CERTIFICATE_VERIFY_FAILED" in msg or "certificate" in msg.lower():
                    hint = ("TLS verification failed. Set DT_CA_BUNDLE to your company's root CA "
                            "bundle (PEM).")
                raise DynatraceError(None, f"connection failed: {msg}", path, hint) from exc
            except httpx.HTTPError as exc:
                raise DynatraceError(None, self.redactor.text(str(exc)), path) from exc

            if resp.status_code in RETRY_STATUS and method == "GET" and attempt < self.settings.max_retries:
                attempt += 1
                await asyncio.sleep(self._retry_delay(resp, attempt))
                continue
            break

        log.debug("%s %s -> %s", method, path, resp.status_code)
        if resp.is_redirect:
            raise DynatraceError(resp.status_code, "unexpected redirect (not followed)", path,
                                 "Check DT_BASE_URL; a login page or proxy may be redirecting.")
        if resp.status_code >= 400:
            raise DynatraceError(resp.status_code, self._error_message(resp), path,
                                 _hint(resp.status_code, path))
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise DynatraceError(resp.status_code, "response was not JSON (is DT_BASE_URL the API "
                                 "endpoint and not a login page?)", path) from exc

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        try:
            return min(10.0, max(0.0, float(resp.headers.get("Retry-After", ""))))
        except ValueError:
            return min(10.0, 0.5 * 2 ** attempt)

    def _error_message(self, resp: httpx.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            return self.redactor.text(resp.text[:300] or resp.reason_phrase)
        err = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(err, dict):
            msg = str(err.get("message") or err.get("details") or err)
            violations = err.get("constraintViolations") or []
            extra = "; ".join(
                f"{v.get('path', '')}: {v.get('message', '')}".strip(": ")
                for v in violations if isinstance(v, dict)
            )
            if extra:
                msg = f"{msg} ({extra})"
        else:
            msg = str(err)
        return self.redactor.text(msg[:1000])
