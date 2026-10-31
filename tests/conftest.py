"""Shared fixtures: a fake Dynatrace API served through httpx.MockTransport."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from dynatrace_mcp.client import DynatraceClient
from dynatrace_mcp.config import load_settings

TOKEN = "dt0c01.ABCDEFGHIJKLMNOPQRSTUVWX.ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"
BASE = "https://dynatrace.corp.example/e/env-1"

Handler = Callable[[httpx.Request], Any]


class FakeDynatrace:
    """Route (METHOD, path-after-/api/v2) to a handler; record every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler | Any] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, response: Handler | Any) -> None:
        self.routes[(method.upper(), path)] = response

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if self._path(r) == path]

    @staticmethod
    def _path(request: httpx.Request) -> str:
        path = request.url.path
        for prefix in ("/e/env-1/api/v2",):
            if path.startswith(prefix):
                return path[len(prefix):]
        return path

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, self._path(request))
        if key not in self.routes:
            return httpx.Response(404, json={"error": {"code": 404, "message": f"no route {key}"}})
        handler = self.routes[key]
        result = handler(request) if callable(handler) else handler
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, content=json.dumps(result), headers={"Content-Type": "application/json"})


def make_settings(**env: str):
    base = {"DT_BASE_URL": BASE, "DT_API_TOKEN": TOKEN, "DT_MAX_RETRIES": "1"}
    base.update(env)
    return load_settings(base)


@pytest.fixture
def fake() -> FakeDynatrace:
    return FakeDynatrace()


@pytest.fixture
async def client(fake: FakeDynatrace):
    c = DynatraceClient(make_settings(), transport=httpx.MockTransport(fake))
    yield c
    await c.aclose()


def params(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params)
