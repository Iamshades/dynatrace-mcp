import httpx

from dynatrace_mcp import rca

import samples as s
from conftest import params


def metrics_handler(request: httpx.Request):
    metric = params(request)["metricSelector"]
    if "errors.total.rate" in metric:
        return s.metric_series(metric, 0.5, 12.0)
    if "response.time" in metric:
        return s.metric_series(metric, 200_000, 210_000)  # microseconds
    return s.metric_series(metric, 100, 100)


def wire(fake, logs_status: int = 200):
    fake.on("GET", f"/problems/{s.PID}", s.problem())
    fake.on("GET", f"/entities/{s.SERVICE}", s.service_entity())
    fake.on("GET", "/events", s.events())
    if logs_status == 200:
        fake.on("GET", "/logs/search", s.logs())
    else:
        fake.on("GET", "/logs/search", httpx.Response(logs_status, json={"error": {"message": "missing scope"}}))
    fake.on("GET", "/metrics/query", metrics_handler)
    fake.on("GET", "/problems", {"problems": [s.problem()]})


def test_log_signature_groups_variants():
    a = rca.log_signature("SQLTimeoutException: timed out after 30000 ms on pool db-7\n  at x")
    b = rca.log_signature("SQLTimeoutException: timed out after 31000 ms on pool db-2")
    assert a == b == "SQLTimeoutException: timed out after <n> ms on pool db-<n>"


def test_log_scope_prefers_process_groups():
    assert rca.log_scope([s.SERVICE, s.PGI, s.PG]) == f'(dt.entity.process_group="{s.PG}")'
    assert rca.log_scope([s.HOST]) == f'(dt.entity.host="{s.HOST}")'
    assert rca.log_scope([s.SERVICE]) is None


async def test_investigate_problem_correlates_everything(fake, client):
    wire(fake)
    r = await rca.investigate_problem(client, s.PID, lookback_minutes=30)

    assert r["errors"] == []
    assert r["rootCause"]["entity"]["id"] == s.SERVICE
    assert r["rootCause"]["details"]["fromRelationships"]["runsOn"] == [s.PG]

    # events scoped to root cause + the process group/instance it runs on
    ev_sel = params(fake.calls("/events")[0])["entitySelector"]
    assert s.SERVICE in ev_sel and s.PG in ev_sel and s.PGI in ev_sel

    # deployment 10 minutes before start is flagged
    assert r["changesBeforeProblem"][0]["eventId"] == "e1"
    assert r["changesBeforeProblem"][0]["minutesBeforeProblem"] == 10.0

    # error logs scoped by process group and grouped by signature
    q = params(fake.calls("/logs/search")[0])["query"]
    assert q == f'status="ERROR" AND (dt.entity.process_group="{s.PG}")'
    top = r["errorLogs"]["topSignatures"][0]
    assert top["count"] == 3 and top["signature"].startswith("java.sql.SQLTimeoutException")

    # metrics compare before vs during, response time converted to ms
    by_name = {m["metric"]: m for m in r["keyMetrics"]}
    assert by_name["Failure rate"]["changePct"] > 1000
    assert by_name["Response time (avg)"]["unit"] == "ms"
    assert by_name["Response time (avg)"]["duringAvg"] == 210.0

    text = " ".join(r["findings"])
    assert "root cause" in text and "Deploy checkout 2.4.1" in text and "Failure rate" in text
    assert r["summary"].startswith("### P-24101 Failure rate increase")
    assert any("debug_service" in step for step in r["nextSteps"])


async def test_investigate_problem_partial_failure(fake, client):
    wire(fake, logs_status=403)
    r = await rca.investigate_problem(client, s.PID)
    assert r["errors"][0]["step"] == "error logs"
    assert "logs.read" in r["errors"][0]["hint"]
    assert r["keyMetrics"]  # the rest still works
    assert any("partial" in f for f in r["findings"])
    assert any("scopes" in step for step in r["nextSteps"])


async def test_investigate_problem_without_root_cause(fake, client):
    p = s.problem()
    p.pop("rootCauseEntity")
    fake.on("GET", f"/problems/{s.PID}", p)
    fake.on("GET", f"/entities/{s.SERVICE}", s.service_entity())
    fake.on("GET", "/events", {"events": []})
    fake.on("GET", "/logs/search", {"results": []})
    fake.on("GET", "/metrics/query", metrics_handler)
    r = await rca.investigate_problem(client, s.PID)
    assert "no root-cause entity" in r["findings"][0]
    assert r["rootCause"]["details"]["entityId"] == s.SERVICE  # falls back to the first affected entity


async def test_debug_service_by_name(fake, client):
    wire(fake)
    answers = [{"entities": [{"entityId": s.SERVICE, "displayName": "checkout-service"}]}]
    fake.on("GET", "/entities", lambda req: answers.pop(0))
    r = await rca.debug_service(client, "checkout-service", "now-1h", "now")
    assert r["service"]["entityId"] == s.SERVICE
    assert r["service"]["calledBy"] == ["SERVICE-8888888888888888"]
    ev = params(fake.calls("/events")[0])
    assert ev["eventSelector"].startswith('eventType("CUSTOM_DEPLOYMENT"')
    assert r["recentDeployments"][0]["title"] == "Deploy checkout 2.4.1"
    assert r["failureLogs"]["count"] == 4
    assert r["problems"][0]["displayId"] == "P-24101"
    text = " ".join(r["findings"])
    assert "Failure rate avg" in text and "open problem" in text and "Deploy checkout" in text


async def test_debug_service_ambiguous_name(fake, client):
    two = {"entities": [{"entityId": "SERVICE-A", "displayName": "checkout-a"},
                        {"entityId": "SERVICE-B", "displayName": "checkout-b"}]}
    answers = [{"entities": []}, two]
    fake.on("GET", "/entities", lambda req: answers.pop(0))
    r = await rca.debug_service(client, "checkout")
    assert r["resolved"] is False
    assert [c["entityId"] for c in r["candidates"]] == ["SERVICE-A", "SERVICE-B"]
    assert len(fake.requests) == 2  # exact + contains lookups, nothing else


async def test_debug_service_same_name_twice_is_ambiguous(fake, client):
    fake.on("GET", "/entities", {"entities": [{"entityId": "SERVICE-A", "displayName": "checkout"},
                                              {"entityId": "SERVICE-B", "displayName": "checkout"}]})
    r = await rca.debug_service(client, "checkout")
    assert r["resolved"] is False and len(r["candidates"]) == 2
