import json
import os
import subprocess
import sys

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dynatrace_mcp.client import DynatraceClient
from dynatrace_mcp.server import build_server

import samples as s
from conftest import BASE, TOKEN, make_settings

READ_TOOLS = {
    "check_connection", "search_logs", "aggregate_logs", "get_trace_logs",
    "list_problems", "get_problem", "get_problem_evidence", "get_problem_impact", "list_problem_comments",
    "investigate_problem", "list_events", "get_event", "search_entities", "find_entity_by_name", "get_entity",
    "query_metrics", "list_metrics", "get_metric_descriptor", "list_security_problems", "get_security_problem",
    "list_slos", "get_slo", "debug_service",
}
WRITE_TOOLS = {"add_problem_comment", "close_problem"}


def server_for(fake, **env):
    settings = make_settings(**env)
    return build_server(client=DynatraceClient(settings, transport=httpx.MockTransport(fake)))


async def names(server):
    return {t.name for t in await server.list_tools()}


async def test_read_only_by_default(fake):
    tools = await server_for(fake).list_tools()
    assert {t.name for t in tools} == READ_TOOLS
    assert all(t.annotations.readOnlyHint for t in tools)
    assert all(t.description for t in tools)
    for t in tools:
        assert "c" not in t.inputSchema.get("properties", {}), t.name


async def test_write_tools_need_opt_in(fake):
    server = server_for(fake, DT_ENABLE_WRITE="true")
    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    assert tools["close_problem"].annotations.destructiveHint is True


async def test_dql_tools_only_with_platform_token(fake):
    server = server_for(fake, DT_PLATFORM_URL="https://abc.apps.dynatrace.com", DT_PLATFORM_TOKEN="dt0s16.AAAAAAAA.BBBBBBBB")
    assert {"execute_dql", "get_trace_spans"} <= await names(server)


async def test_disable_tools(fake):
    server = server_for(fake, DT_DISABLE_TOOLS="list_slos,get_slo")
    assert not {"list_slos", "get_slo"} & await names(server)


async def test_call_tool_redacts_output(fake):
    fake.on("GET", "/logs/search", {"results": [
        {"timestamp": s.NOW, "status": "ERROR", "content": f"auth failed password=hunter2 header {TOKEN}"}]})
    server = server_for(fake)
    result = await server.call_tool("search_logs", {"query": 'status="ERROR"'})
    blocks = result[0] if isinstance(result, tuple) else result
    text = "".join(b.text for b in blocks)
    assert json.loads(text)["count"] == 1
    assert "hunter2" not in text and TOKEN not in text
    assert "password=***" in text


async def test_call_tool_error_is_tool_error(fake):
    fake.on("GET", "/problems/x", httpx.Response(403, json={"error": {"message": "Token is missing required scope"}}))
    server = server_for(fake)
    with pytest.raises(Exception) as exc:
        await server.call_tool("get_problem", {"problem_id": "x"})
    assert "problems.read" in str(exc.value)


async def test_unconfigured_server_still_lists_tools():
    server = build_server(env={})
    assert await names(server) == READ_TOOLS
    with pytest.raises(Exception, match="DT_BASE_URL"):
        await server.call_tool("check_connection", {})


def test_version_flag():
    out = subprocess.run([sys.executable, "-m", "dynatrace_mcp.server", "--version"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.startswith("dynatrace-mcp ")


async def test_stdio_end_to_end():
    """Start the real server over stdio, as Cursor does, and list its tools."""
    env = {**os.environ, "DT_BASE_URL": BASE, "DT_API_TOKEN": TOKEN, "DT_LOG_LEVEL": "INFO"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "dynatrace_mcp.server"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.serverInfo.name == "dynatrace"
            assert init.serverInfo.version == "0.1.0"
            assert "investigate_problem" in (init.instructions or "")
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == READ_TOOLS
