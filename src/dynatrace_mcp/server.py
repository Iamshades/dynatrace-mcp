"""MCP server entry point (stdio) exposing Dynatrace tools to Cursor."""


import functools
import inspect
import logging
import os
import sys
from typing import Annotated, Any, Awaitable, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, api, rca
from .client import DynatraceClient, DynatraceError
from .config import ConfigError, Settings, load_settings
from .redact import fit_output, truncate_strings

log = logging.getLogger("dynatrace_mcp")

INSTRUCTIONS = """Dynatrace observability tools (classic API v2; works on Dynatrace Managed and SaaS).
Start with check_connection if anything fails. For an incident, call investigate_problem first,
then drill down with get_problem_evidence, search_logs, query_metrics and get_entity.
For a misbehaving service, call debug_service. Times accept now-2h, ISO-8601 or epoch ms.
Selectors use Dynatrace syntax, e.g. entitySelector type("SERVICE"),entityName.equals("checkout").
Never ask the user to paste their API token into chat."""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

# Shared parameter types (descriptions end up in the JSON schema Cursor shows the model).
FromTime = Annotated[str, Field(description="Start: now-2h, ISO-8601 (2026-10-31T10:00:00Z) or epoch ms")]
ToTime = Annotated[str, Field(description="End: now, ISO-8601 or epoch ms")]
PageKey = Annotated[str | None, Field(description="nextPageKey from the previous page; other filters are ignored when set")]
ProblemId = Annotated[str, Field(description="Problem id (internal id or display id such as P-12345)")]


class State:
    """Lazily built so the server starts (and lists tools) even when env is incomplete."""

    def __init__(self, settings: Settings | None = None, client: DynatraceClient | None = None,
                 error: str | None = None):
        self.settings = settings
        self.client = client
        self.error = error

    def require(self) -> DynatraceClient:
        if self.client is None:
            raise ToolError(f"Dynatrace MCP is not configured: {self.error}")
        return self.client


def _shape(state: State, value: Any) -> Any:
    client = state.client
    if client is None:
        return value
    value = client.redactor.data(value)
    value = truncate_strings(value, client.settings.max_log_line_chars * 2)
    return fit_output(value, client.settings.max_output_chars)


def build_server(settings: Settings | None = None, client: DynatraceClient | None = None,
                 env: dict[str, str] | None = None) -> FastMCP:
    if settings is None and client is None:
        try:
            settings = load_settings(env)
        except ConfigError as err:
            state = State(error=str(err))
        else:
            state = State(settings, DynatraceClient(settings))
    else:
        settings = settings or (client.settings if client else None)
        state = State(settings, client or DynatraceClient(settings))  # type: ignore[arg-type]

    mcp = FastMCP("dynatrace", instructions=INSTRUCTIONS)
    mcp._mcp_server.version = __version__

    def tool(annotations: ToolAnnotations) -> Callable:
        def wrap(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            @functools.wraps(fn)
            async def run(*args: Any, **kwargs: Any) -> Any:
                c = state.require()
                try:
                    return _shape(state, await fn(c, *args, **kwargs))
                except DynatraceError as err:
                    raise ToolError(c.redactor.text(str(err))) from err

            # Hide the injected client parameter from the tool schema.
            sig = inspect.signature(fn)
            run.__signature__ = sig.replace(parameters=list(sig.parameters.values())[1:])  # type: ignore[attr-defined]
            run.__annotations__ = {k: v for k, v in fn.__annotations__.items() if k != "c"}
            return mcp.tool(annotations=annotations)(run)
        return wrap

    # ----------------------------------------------------------- diagnostics

    @tool(READ)
    async def check_connection(c: DynatraceClient) -> dict:
        """Show the active configuration (no secrets) and probe which token scopes work."""
        return await api.check_connection(c)

    # ------------------------------------------------------------------ logs

    @tool(READ)
    async def search_logs(
        c: DynatraceClient,
        query: Annotated[str, Field(description='Log query, e.g. status="ERROR" AND content="timeout". Empty = all')] = "",
        from_time: FromTime = "now-2h",
        to_time: ToTime = "now",
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
        sort: Annotated[str, Field(description="timestamp or -timestamp")] = "-timestamp",
        next_slice_key: Annotated[str | None, Field(description="nextSliceKey from the previous result")] = None,
    ) -> dict:
        """Search log records (GET /api/v2/logs/search). Needs scope logs.read."""
        return await api.search_logs(c, query, from_time, to_time, limit, sort, next_slice_key)

    @tool(READ)
    async def aggregate_logs(
        c: DynatraceClient,
        query: Annotated[str, Field(description="Log query to filter on")] = "",
        from_time: FromTime = "now-2h",
        to_time: ToTime = "now",
        group_by: Annotated[list[str] | None, Field(description="Attributes to group by, e.g. ['status','log.source']")] = None,
        time_buckets: Annotated[int | None, Field(ge=1, le=100, description="Number of time buckets")] = None,
        max_group_values: Annotated[int | None, Field(ge=1, le=100)] = None,
    ) -> dict:
        """Count log records over time and by attribute (GET /api/v2/logs/aggregate). Needs logs.read."""
        return await api.aggregate_logs(c, query, from_time, to_time, group_by, time_buckets, max_group_values)

    @tool(READ)
    async def get_trace_logs(
        c: DynatraceClient,
        trace_id: Annotated[str, Field(description="Distributed trace id (hex), e.g. from a log line or the UI")],
        from_time: FromTime = "now-24h",
        to_time: ToTime = "now",
        attribute: Annotated[str, Field(description="Log attribute holding the trace id")] = "trace_id",
    ) -> dict:
        """Log lines for one distributed trace / PurePath. Classic API v2 has no trace endpoint."""
        return await api.trace_logs(c, trace_id, from_time, to_time, attribute)

    # -------------------------------------------------------------- problems

    @tool(READ)
    async def list_problems(
        c: DynatraceClient,
        problem_selector: Annotated[str, Field(description='e.g. status("OPEN"),severityLevel("ERROR")')] = "",
        entity_selector: Annotated[str, Field(description='Only problems touching these entities, e.g. type("SERVICE"),tag("team:pay")')] = "",
        from_time: FromTime = "now-24h",
        to_time: ToTime = "now",
        page_size: Annotated[int, Field(ge=1, le=500)] = 50,
        sort: Annotated[str, Field(description="e.g. -startTime, +status, -relevance")] = "-startTime",
        next_page_key: PageKey = None,
    ) -> dict:
        """List problems (GET /api/v2/problems). Needs problems.read."""
        return await api.list_problems(c, problem_selector, entity_selector, from_time, to_time, page_size,
                                       sort, next_page_key)

    @tool(READ)
    async def get_problem(c: DynatraceClient, problem_id: ProblemId) -> dict:
        """Problem details: status, root-cause entity, root-cause evidence, impacts, recent comments."""
        return await api.get_problem(c, problem_id)

    @tool(READ)
    async def get_problem_evidence(
        c: DynatraceClient,
        problem_id: ProblemId,
        root_cause_only: bool = False,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> dict:
        """Evidence Dynatrace collected for a problem (events, metric change points, transactions)."""
        return await api.get_problem_evidence(c, problem_id, root_cause_only, limit)

    @tool(READ)
    async def get_problem_impact(c: DynatraceClient, problem_id: ProblemId) -> dict:
        """Root cause, affected and impacted entities, and impact analysis (users, services, apps)."""
        return await api.get_problem_impact(c, problem_id)

    @tool(READ)
    async def list_problem_comments(
        c: DynatraceClient, problem_id: ProblemId,
        page_size: Annotated[int, Field(ge=1, le=500)] = 50, next_page_key: PageKey = None,
    ) -> dict:
        """Comments on a problem (GET /api/v2/problems/{id}/comments)."""
        return await api.list_problem_comments(c, problem_id, page_size, next_page_key)

    @tool(READ)
    async def investigate_problem(
        c: DynatraceClient,
        problem_id: ProblemId,
        lookback_minutes: Annotated[int, Field(ge=5, le=1440, description="Minutes before the problem start to include (changes, baseline metrics)")] = 60,
        log_limit: Annotated[int, Field(ge=1, le=1000)] = 200,
        log_filter: Annotated[str, Field(description="Log query clause for error logs")] = 'status="ERROR"',
    ) -> dict:
        """One-shot RCA: correlates the problem's root-cause entity and evidence, change events
        (deployments, config, restarts) before the start, error logs in the problem window grouped by
        signature, and key metrics before vs during. Returns findings, a Markdown summary and next steps."""
        return await rca.investigate_problem(c, problem_id, lookback_minutes, log_limit, log_filter)

    # ---------------------------------------------------------------- events

    @tool(READ)
    async def list_events(
        c: DynatraceClient,
        event_selector: Annotated[str, Field(description='e.g. eventType("CUSTOM_DEPLOYMENT"),status("OPEN")')] = "",
        entity_selector: Annotated[str, Field(description='e.g. entityId("SERVICE-1234567890ABCDEF")')] = "",
        from_time: FromTime = "now-2h",
        to_time: ToTime = "now",
        page_size: Annotated[int, Field(ge=1, le=1000)] = 100,
        next_page_key: PageKey = None,
    ) -> dict:
        """List events (deployments, config changes, restarts, anomalies). Needs events.read."""
        return await api.list_events(c, event_selector, entity_selector, from_time, to_time, page_size, next_page_key)

    @tool(READ)
    async def get_event(c: DynatraceClient, event_id: str) -> dict:
        """One event with all properties (GET /api/v2/events/{id})."""
        return await api.get_event(c, event_id)

    # -------------------------------------------------------------- entities

    @tool(READ)
    async def search_entities(
        c: DynatraceClient,
        entity_selector: Annotated[str, Field(description='Must contain type(...) or entityId(...), e.g. type("SERVICE"),tag("env:prod")')],
        from_time: FromTime = "now-72h",
        to_time: ToTime = "now",
        fields: Annotated[str, Field(description="+properties,+tags,+managementZones,+fromRelationships,+toRelationships")] = "+tags,+managementZones",
        page_size: Annotated[int, Field(ge=1, le=500)] = 50,
        next_page_key: PageKey = None,
    ) -> dict:
        """Search monitored entities by selector (GET /api/v2/entities). Needs entities.read."""
        return await api.search_entities(c, entity_selector, from_time, to_time, fields, page_size, next_page_key)

    @tool(READ)
    async def find_entity_by_name(
        c: DynatraceClient,
        name: str,
        entity_type: Annotated[str, Field(description="SERVICE, HOST, PROCESS_GROUP, APPLICATION, ...")] = "SERVICE",
    ) -> dict:
        """Find entities by display name (exact first, then contains)."""
        return await api.find_entities(c, name, entity_type)

    @tool(READ)
    async def get_entity(
        c: DynatraceClient, entity_id: str, include_relationships: bool = True,
        from_time: FromTime = "now-72h", to_time: ToTime = "now",
    ) -> dict:
        """Entity details: properties, tags, management zones and relationships (calls, runsOn, ...)."""
        return await api.get_entity(c, entity_id, include_relationships, from_time, to_time)

    # --------------------------------------------------------------- metrics

    @tool(READ)
    async def query_metrics(
        c: DynatraceClient,
        metric_selector: Annotated[str, Field(description='e.g. builtin:service.response.time:avg or builtin:host.cpu.usage:splitBy("dt.entity.host"):max')],
        entity_selector: Annotated[str, Field(description="Limit to these entities")] = "",
        from_time: FromTime = "now-2h",
        to_time: ToTime = "now",
        resolution: Annotated[str, Field(description="e.g. 1m, 5m, 1h, Inf; empty = automatic")] = "",
        mz_selector: Annotated[str, Field(description='Management zone, e.g. mzName("Prod")')] = "",
        include_datapoints: Annotated[bool, Field(description="Return every timestamp/value, not only the summary")] = False,
    ) -> dict:
        """Query metric data (GET /api/v2/metrics/query) with per-series min/max/avg/last. Needs metrics.read."""
        return await api.query_metrics(c, metric_selector, entity_selector, from_time, to_time, resolution,
                                       mz_selector, include_datapoints)

    @tool(READ)
    async def list_metrics(
        c: DynatraceClient,
        text: Annotated[str, Field(description="Free-text search in metric id/name, e.g. 'response time'")] = "",
        metric_selector: Annotated[str, Field(description="e.g. builtin:service.* ")] = "",
        page_size: Annotated[int, Field(ge=1, le=500)] = 50,
        next_page_key: PageKey = None,
    ) -> dict:
        """Find metric keys and their units and dimensions (GET /api/v2/metrics)."""
        return await api.list_metrics(c, text, metric_selector, page_size, next_page_key)

    @tool(READ)
    async def get_metric_descriptor(c: DynatraceClient, metric_key: str) -> dict:
        """Full descriptor of one metric: unit, dimensions, aggregations, transformations."""
        return await api.get_metric_descriptor(c, metric_key)

    # -------------------------------------------------- security and SLOs

    @tool(READ)
    async def list_security_problems(
        c: DynatraceClient,
        selector: Annotated[str, Field(description='securityProblemSelector, e.g. status("OPEN"),riskLevel("CRITICAL")')] = "",
        from_time: FromTime = "now-30d",
        to_time: ToTime = "now",
        sort: str = "-riskAssessment.riskScore",
        page_size: Annotated[int, Field(ge=1, le=500)] = 50,
        next_page_key: PageKey = None,
    ) -> dict:
        """List vulnerabilities (GET /api/v2/securityProblems). Needs securityProblems.read."""
        return await api.list_security_problems(c, selector, from_time, to_time, sort, page_size, next_page_key)

    @tool(READ)
    async def get_security_problem(c: DynatraceClient, security_problem_id: str) -> dict:
        """One vulnerability with risk assessment, affected entities and remediation."""
        return await api.get_security_problem(c, security_problem_id)

    @tool(READ)
    async def list_slos(
        c: DynatraceClient,
        slo_selector: Annotated[str, Field(description='e.g. name("Checkout availability") or text("checkout")')] = "",
        from_time: Annotated[str, Field(description="Evaluate over a custom window; empty = the SLO's own timeframe")] = "",
        to_time: Annotated[str, Field(description="End of a custom window")] = "",
        evaluate: bool = True,
        page_size: Annotated[int, Field(ge=1, le=25)] = 25,
        next_page_key: PageKey = None,
    ) -> dict:
        """List SLOs with status and error budget (GET /api/v2/slo). Needs slo.read."""
        return await api.list_slos(c, slo_selector, from_time, to_time, evaluate, page_size, next_page_key)

    @tool(READ)
    async def get_slo(c: DynatraceClient, slo_id: str, from_time: str = "", to_time: str = "") -> dict:
        """One SLO with evaluated percentage, error budget and burn rate."""
        return await api.get_slo(c, slo_id, from_time, to_time)

    # ------------------------------------------------------------- services

    @tool(READ)
    async def debug_service(
        c: DynatraceClient,
        service: Annotated[str, Field(description="Service entity id (SERVICE-...) or display name")],
        from_time: FromTime = "now-2h",
        to_time: ToTime = "now",
        deployments_from: Annotated[str, Field(description="How far back to look for deployments")] = "now-7d",
        log_limit: Annotated[int, Field(ge=1, le=1000)] = 200,
        log_filter: Annotated[str, Field(description="Log query clause for failure logs")] = 'status="ERROR"',
    ) -> dict:
        """Debug a service: failure rate, request volume and response time (baseline vs recent),
        failure logs of its process groups grouped by signature, recent deployments/config changes,
        problems in the window, and callers/callees."""
        return await rca.debug_service(c, service, from_time, to_time, deployments_from, log_limit, log_filter)

    # ------------------------------------------------- optional: Grail DQL

    if state.settings and state.settings.dql_enabled:
        @tool(READ)
        async def execute_dql(
            c: DynatraceClient,
            query: Annotated[str, Field(description="DQL, e.g. fetch logs | filter loglevel == \"ERROR\" | limit 50")],
            max_records: Annotated[int, Field(ge=1, le=5000)] = 200,
        ) -> dict:
            """Run a DQL query on Grail (SaaS platform only; needs DT_PLATFORM_URL and DT_PLATFORM_TOKEN)."""
            return await api.execute_dql(c, query, max_records)

        @tool(READ)
        async def get_trace_spans(
            c: DynatraceClient,
            trace_id: Annotated[str, Field(description="Distributed trace id (hex)")],
            max_records: Annotated[int, Field(ge=1, le=5000)] = 500,
        ) -> dict:
            """All spans of a distributed trace from Grail (fetch spans)."""
            if not all(ch in "0123456789abcdefABCDEF" for ch in trace_id):
                raise DynatraceError(None, "trace_id must be hex", "dql")
            q = (f'fetch spans | filter trace.id == toUid("{trace_id}") | sort start_time asc '
                 "| fields start_time, duration, span.name, span.kind, service.name, "
                 "dt.entity.service, span.status_code, http.response.status_code, span.id, span.parent_id")
            return await api.execute_dql(c, q, max_records)

    # ------------------------------------------------ opt-in write tools

    if state.settings and state.settings.enable_write:
        @tool(WRITE)
        async def add_problem_comment(
            c: DynatraceClient, problem_id: ProblemId,
            message: Annotated[str, Field(min_length=1, max_length=5000)],
            context: Annotated[str, Field(description="Optional context label, e.g. Cursor RCA")] = "",
        ) -> dict:
            """WRITE: add a comment to a problem. Confirm the exact text with the user first. Needs problems.write."""
            return await api.add_problem_comment(c, problem_id, message, context)

        @tool(DESTRUCTIVE)
        async def close_problem(
            c: DynatraceClient, problem_id: ProblemId,
            message: Annotated[str, Field(min_length=1, max_length=5000, description="Closing comment")],
        ) -> dict:
            """WRITE: close a problem with a comment. Ask the user for explicit confirmation first. Needs problems.write."""
            return await api.close_problem(c, problem_id, message)

    for name in (state.settings.disabled_tools if state.settings else ()):
        if name in mcp._tool_manager._tools:
            mcp.remove_tool(name)
        else:
            log.warning("DT_DISABLE_TOOLS: unknown tool %s", name)

    mcp._dt_state = state  # type: ignore[attr-defined]  # for tests and main()
    return mcp


def main() -> None:
    level = os.environ.get("DT_LOG_LEVEL", "WARNING").upper()
    # stdout carries the MCP protocol; logs go to stderr only.
    logging.basicConfig(stream=sys.stderr, level=level, format="dynatrace-mcp %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"dynatrace-mcp {__version__}")
        return
    server = build_server()
    state: State = server._dt_state  # type: ignore[attr-defined]
    if state.error:
        log.error("not configured: %s (tools will report this until it is fixed)", state.error)
    elif state.settings:
        for warning in state.settings.warnings:
            log.warning("%s", warning)
        log.info("target %s (%s), write tools %s", state.settings.base_url, state.settings.deployment,
                 "ON" if state.settings.enable_write else "off")
    server.run("stdio")


if __name__ == "__main__":
    main()
