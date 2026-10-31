import httpx
import pytest

from dynatrace_mcp.client import DynatraceClient, DynatraceError

from conftest import TOKEN, make_settings


async def test_sends_api_token_header(fake, client):
    fake.on("GET", "/metrics", {"metrics": []})
    await client.get("/metrics", {"pageSize": 1, "text": None, "empty": ""})
    req = fake.requests[0]
    assert req.headers["Authorization"] == f"Api-Token {TOKEN}"
    assert str(req.url).startswith("https://dynatrace.corp.example/e/env-1/api/v2/metrics")
    assert dict(req.url.params) == {"pageSize": "1"}


async def test_list_params_repeat(fake, client):
    fake.on("GET", "/logs/aggregate", {})
    await client.get("/logs/aggregate", {"groupBy": ["status", "log.source"]})
    assert fake.requests[0].url.params.get_list("groupBy") == ["status", "log.source"]


async def test_403_gives_scope_hint(fake, client):
    fake.on("GET", "/logs/search", httpx.Response(403, json={"error": {"code": 403, "message": "Token is missing required scope"}}))
    with pytest.raises(DynatraceError) as exc:
        await client.get("/logs/search")
    assert exc.value.status == 403
    assert "logs.read" in exc.value.hint


async def test_error_message_never_contains_token(fake, client):
    fake.on("GET", "/events", httpx.Response(400, json={"error": {"code": 400, "message": f"bad header {TOKEN}",
                                                                   "constraintViolations": [{"path": "from", "message": "invalid"}]}}))
    with pytest.raises(DynatraceError) as exc:
        await client.get("/events")
    assert TOKEN not in str(exc.value)
    assert "from: invalid" in str(exc.value)


async def test_retries_get_on_429(fake, client):
    responses = [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json={"ok": True})]
    fake.on("GET", "/problems", lambda req: responses.pop(0))
    assert await client.get("/problems") == {"ok": True}
    assert len(fake.calls("/problems")) == 2


async def test_does_not_retry_post(fake, client):
    fake.on("POST", "/problems/p1/comments", httpx.Response(503, text="busy"))
    with pytest.raises(DynatraceError):
        await client.post("/problems/p1/comments", {"message": "x"})
    assert len(fake.requests) == 1


async def test_does_not_follow_redirects(fake, client):
    fake.on("GET", "/problems", httpx.Response(302, headers={"Location": "https://sso.example/login"}))
    with pytest.raises(DynatraceError, match="redirect"):
        await client.get("/problems")
    assert len(fake.requests) == 1


async def test_non_json_body(fake, client):
    fake.on("GET", "/problems", httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(DynatraceError, match="not JSON"):
        await client.get("/problems")


async def test_connect_error_tls_hint():
    def boom(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", request=request)
    c = DynatraceClient(make_settings(), transport=httpx.MockTransport(boom))
    with pytest.raises(DynatraceError) as exc:
        await c.get("/problems")
    assert "DT_CA_BUNDLE" in exc.value.hint
    await c.aclose()


def test_tls_settings():
    import ssl

    import certifi

    from dynatrace_mcp.client import _ssl_verify
    assert _ssl_verify(make_settings()) is True
    assert _ssl_verify(make_settings(DT_INSECURE_TLS="true")) is False
    assert isinstance(_ssl_verify(make_settings(DT_CA_BUNDLE=certifi.where())), ssl.SSLContext)
