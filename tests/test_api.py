import httpx

from dynatrace_mcp import api

import samples as s
from conftest import params


async def test_search_logs_params_and_condense(fake, client):
    fake.on("GET", "/logs/search", {**s.logs(), "nextSliceKey": "slice-2"})
    out = await api.search_logs(client, 'status="ERROR"', "now-1h", "now", 5000)
    p = params(fake.requests[0])
    assert p == {"query": 'status="ERROR"', "from": "now-1h", "to": "now", "limit": "1000", "sort": "-timestamp"}
    assert out["count"] == 4 and out["nextSliceKey"] == "slice-2"
    first = out["results"][0]
    assert first["timestamp"].endswith("Z")
    assert first["attributes"] == {"dt.entity.process_group": s.PG, "log.source": "/var/log/app.log"}


async def test_search_logs_next_slice_sends_only_the_key(fake, client):
    fake.on("GET", "/logs/search", {"results": []})
    await api.search_logs(client, "x", next_slice_key="abc")
    assert params(fake.requests[0]) == {"nextSliceKey": "abc"}


async def test_trace_logs_validates_id(fake, client):
    fake.on("GET", "/logs/search", {"results": []})
    out = await api.trace_logs(client, "4bf92f3577b34da6a3ce929d0e0e4736")
    assert params(fake.requests[0])["query"] == 'trace_id="4bf92f3577b34da6a3ce929d0e0e4736"'
    assert "PurePath" in out["note"]
    try:
        await api.trace_logs(client, 'x" OR 1=1')
    except api.DynatraceError:
        pass
    else:
        raise AssertionError("expected validation error")


async def test_list_problems(fake, client):
    fake.on("GET", "/problems", {"totalCount": 1, "problems": [s.problem()], "nextPageKey": "k2"})
    out = await api.list_problems(client, 'status("OPEN")')
    p = params(fake.requests[0])
    assert p["problemSelector"] == 'status("OPEN")' and p["from"] == "now-24h"
    prob = out["problems"][0]
    assert prob["displayId"] == "P-24101"
    assert prob["rootCauseEntity"] == {"id": s.SERVICE, "type": "SERVICE", "name": "checkout-service"}
    assert out["nextPageKey"] == "k2"
    await api.list_problems(client, next_page_key="k2")
    assert params(fake.requests[1]) == {"nextPageKey": "k2"}


async def test_get_problem_by_display_id(fake, client):
    fake.on("GET", "/problems", {"problems": [{"problemId": s.PID}]})
    fake.on("GET", f"/problems/{s.PID}", s.problem())
    out = await api.get_problem(client, "p-24101")
    assert params(fake.requests[0])["problemSelector"] == 'displayId("P-24101")'
    assert params(fake.requests[1])["fields"] == api.PROBLEM_FIELDS
    assert out["evidenceCount"] == 2 and len(out["rootCauseEvidence"]) == 1
    assert out["impacts"][0]["estimatedAffectedUsers"] == 120
    assert out["recentComments"][0]["author"] == "oncall"
    assert out["status"] == "OPEN" and "endTime" not in out


async def test_display_id_not_found(fake, client):
    fake.on("GET", "/problems", {"problems": []})
    try:
        await api.get_problem(client, "P-1")
    except api.DynatraceError as err:
        assert err.status == 404
    else:
        raise AssertionError


async def test_evidence_and_impact(fake, client):
    fake.on("GET", f"/problems/{s.PID}", s.problem())
    ev = await api.get_problem_evidence(client, s.PID)
    assert [e["rootCauseRelevant"] for e in ev["evidence"]] == [True, False]
    assert ev["evidence"][1]["properties"]["reason"].startswith("OOM")
    rc = await api.get_problem_evidence(client, s.PID, root_cause_only=True)
    assert rc["totalCount"] == 1
    imp = await api.get_problem_impact(client, s.PID)
    assert imp["impactedEntities"][0]["name"] == "webshop"


async def test_comments_read_and_write(fake, client):
    fake.on("GET", f"/problems/{s.PID}/comments", {"comments": [{"id": "c1", "content": "hi"}], "totalCount": 1})
    fake.on("POST", f"/problems/{s.PID}/comments", httpx.Response(201))
    fake.on("POST", f"/problems/{s.PID}/close", {"closing": True})
    out = await api.list_problem_comments(client, s.PID)
    assert out["comments"][0]["content"] == "hi"
    await api.add_problem_comment(client, s.PID, "RCA: db pool", "cursor")
    assert fake.requests[-1].read() == b'{"message":"RCA: db pool","context":"cursor"}'
    closed = await api.close_problem(client, s.PID, "fixed")
    assert closed["ok"] and closed["result"] == {"closing": True}


async def test_events(fake, client):
    fake.on("GET", "/events", s.events())
    fake.on("GET", "/events/e1", s.events()["events"][0])
    out = await api.list_events(client, 'eventType("CUSTOM_DEPLOYMENT")', 'entityId("X")')
    p = params(fake.requests[0])
    assert p["eventSelector"] == 'eventType("CUSTOM_DEPLOYMENT")' and p["entitySelector"] == 'entityId("X")'
    assert out["events"][0]["properties"]["dt.event.deployment.version"] == "2.4.1"
    assert out["events"][0]["entity"]["id"] == s.PG
    one = await api.get_event(client, "e1")
    assert one["eventType"] == "CUSTOM_DEPLOYMENT"


async def test_entities(fake, client):
    fake.on("GET", f"/entities/{s.SERVICE}", s.service_entity())
    out = await api.get_entity(client, s.SERVICE)
    assert "+fromRelationships" in params(fake.requests[0])["fields"]
    assert out["fromRelationships"]["runsOn"] == [s.PG]
    assert out["tags"] == ["team:payments"]


async def test_find_entities_exact_then_contains(fake, client):
    answers = [{"entities": []}, {"entities": [{"entityId": s.SERVICE, "displayName": "checkout-service"}]}]
    fake.on("GET", "/entities", lambda req: answers.pop(0))
    out = await api.find_entities(client, 'check"out')
    sels = [params(r)["entitySelector"] for r in fake.requests]
    assert sels == ['type("SERVICE"),entityName.equals("check~"out")', 'type("SERVICE"),entityName("check~"out")']
    assert out["match"] == "contains" and out["entities"][0]["entityId"] == s.SERVICE


async def test_query_metrics_summary(fake, client):
    fake.on("GET", "/metrics/query", s.metric_series("builtin:service.errors.total.rate", 1.0, 10.0))
    out = await api.query_metrics(client, "builtin:service.errors.total.rate", 'entityId("x")')
    series = out["result"][0]["series"][0]
    assert series["summary"]["max"] == 10.0 and series["summary"]["min"] == 1.0
    assert series["summary"]["nonNull"] == 59
    assert "datapoints" not in series
    out = await api.query_metrics(client, "m", include_datapoints=True)
    assert len(out["result"][0]["series"][0]["datapoints"]) == 60


async def test_list_metrics_and_descriptor(fake, client):
    fake.on("GET", "/metrics", {"totalCount": 1, "metrics": [
        {"metricId": "builtin:host.cpu.usage", "displayName": "CPU usage %", "unit": "Percent",
         "dimensionDefinitions": [{"key": "dt.entity.host"}]}]})
    fake.on("GET", "/metrics/builtin:host.cpu.usage", {"metricId": "builtin:host.cpu.usage"})
    out = await api.list_metrics(client, text="cpu")
    assert out["metrics"][0]["dimensions"] == ["dt.entity.host"]
    assert (await api.get_metric_descriptor(client, "builtin:host.cpu.usage"))["metricId"]


async def test_security_problems(fake, client):
    sp = {"securityProblemId": "sp1", "displayId": "S-1", "title": "Log4Shell", "status": "OPEN",
          "riskAssessment": {"riskLevel": "CRITICAL", "riskScore": 10.0, "exposure": "PUBLIC_NETWORK"},
          "cveIds": ["CVE-2021-44228"], "firstSeenTimestamp": s.NOW}
    fake.on("GET", "/securityProblems", {"securityProblems": [sp], "totalCount": 1})
    fake.on("GET", "/securityProblems/sp1", sp)
    out = await api.list_security_problems(client, 'riskLevel("CRITICAL")')
    assert out["securityProblems"][0]["riskLevel"] == "CRITICAL"
    assert params(fake.requests[0])["securityProblemSelector"] == 'riskLevel("CRITICAL")'
    one = await api.get_security_problem(client, "sp1")
    assert one["cveIds"] == ["CVE-2021-44228"]


async def test_slos(fake, client):
    slo = {"id": "slo1", "name": "Checkout availability", "status": "WARNING", "evaluatedPercentage": 99.2,
           "target": 99.5, "warning": 99.7, "errorBudget": -0.3, "timeframe": "-1w",
           "errorBudgetBurnRate": {"burnRateValue": 2.1, "burnRateType": "FAST", "x": 1}}
    fake.on("GET", "/slo", {"slo": [slo], "totalCount": 1})
    fake.on("GET", "/slo/slo1", slo)
    out = await api.list_slos(client, 'text("checkout")')
    assert params(fake.requests[0])["timeFrame"] == "CURRENT"
    assert out["slos"][0]["errorBudgetBurnRate"]["burnRateType"] == "FAST"
    one = await api.get_slo(client, "slo1", "now-7d", "now")
    assert params(fake.requests[1])["timeFrame"] == "GTF"
    assert one["evaluatedPercentage"] == 99.2


async def test_check_connection_reports_missing_scopes(fake, client):
    for _, path, _ in api.PROBES:
        fake.on("GET", path, {})
    fake.on("GET", "/logs/search", httpx.Response(403, json={"error": {"message": "missing scope"}}))
    out = await api.check_connection(client)
    assert out["reachable"] and out["authenticated"]
    assert out["missingScopes"] == ["logs.read"]
    assert out["settings"]["api_token"] == "set"


async def test_check_connection_bad_token(fake, client):
    for _, path, _ in api.PROBES:
        fake.on("GET", path, httpx.Response(401, json={"error": {"message": "invalid token"}}))
    out = await api.check_connection(client)
    assert out["authenticated"] is False


async def test_execute_dql_polls_with_platform_token(fake):
    from dynatrace_mcp.client import DynatraceClient
    from conftest import make_settings

    settings = make_settings(DT_PLATFORM_URL="https://abc.apps.dynatrace.com", DT_PLATFORM_TOKEN="dt0s16.AAAAAAAA.BBBBBBBB")
    c = DynatraceClient(settings, transport=httpx.MockTransport(fake))
    fake.on("POST", "/platform/storage/query/v1/query:execute", {"state": "RUNNING", "requestToken": "rt1"})
    fake.on("GET", "/platform/storage/query/v1/query:poll",
            {"state": "SUCCEEDED", "result": {"records": [{"a": 1}], "metadata": {"grail": {"scannedRecords": 9}}}})
    out = await api.execute_dql(c, "fetch logs | limit 1")
    assert out == {"state": "SUCCEEDED", "recordCount": 1, "records": [{"a": 1}], "metadata": {"scannedRecords": 9}}
    execute, poll = fake.requests
    assert execute.headers["Authorization"] == "Bearer dt0s16.AAAAAAAA.BBBBBBBB"
    assert execute.url.host == "abc.apps.dynatrace.com"
    assert params(poll)["request-token"] == "rt1"
    await c.aclose()


async def test_dql_not_configured(client):
    try:
        await api.execute_dql(client, "fetch logs")
    except api.DynatraceError as err:
        assert "DT_PLATFORM_TOKEN" in err.hint
    else:
        raise AssertionError
