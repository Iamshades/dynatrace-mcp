"""Read (and opt-in write) operations on the Dynatrace classic API v2.

Each function returns a compact dict: the fields an engineer needs, epoch
timestamps converted to ISO-8601 UTC, and pagination keys passed through.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .client import DynatraceClient, DynatraceError
from .timeutil import duration_text, iso, now_ms

PROBLEM_FIELDS = "evidenceDetails,impactAnalysis,recentComments"
DISPLAY_ID = re.compile(r"^P-\d+$", re.I)


def _q(value: str) -> str:
    """Quote a value for use inside a Dynatrace selector."""
    return '"' + value.replace("~", "~~").replace('"', '~"') + '"'


def entity_id_selector(ids: list[str]) -> str:
    return "entityId(" + ",".join(_q(i) for i in ids) + ")"


def entity_ref(e: dict | None) -> dict | None:
    """Normalize the many entity shapes the API returns to {id, type, name}."""
    if not e:
        return None
    eid = e.get("entityId")
    if isinstance(eid, dict):
        return {"id": eid.get("id"), "type": eid.get("type"), "name": e.get("name")}
    out = {"id": eid or e.get("id"), "type": e.get("type"), "name": e.get("name") or e.get("displayName")}
    return {k: v for k, v in out.items() if v is not None}


def _page(body: dict, items_key: str, items: list) -> dict:
    out: dict[str, Any] = {items_key: items}
    for key in ("totalCount", "nextPageKey", "nextSliceKey", "warnings"):
        if body.get(key) not in (None, "", []):
            out[key] = body[key]
    return out


# --------------------------------------------------------------------- logs

def condense_log(record: dict, max_chars: int = 2000) -> dict:
    out: dict[str, Any] = {
        "timestamp": iso(record.get("timestamp")),
        "status": record.get("status"),
        "content": (record.get("content") or "")[:max_chars],
    }
    cols = record.get("additionalColumns") or {}
    attrs = {}
    for key, value in cols.items():
        if value in (None, "", []):
            continue
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        attrs[key] = value
    if record.get("event.type") and record.get("event.type") != "LOG":
        attrs["event.type"] = record["event.type"]
    if attrs:
        out["attributes"] = attrs
    return out


async def search_logs(c: DynatraceClient, query: str = "", from_time: str = "now-2h", to_time: str = "now",
                      limit: int = 100, sort: str = "-timestamp", next_slice_key: str | None = None) -> dict:
    if next_slice_key:
        params: dict[str, Any] = {"nextSliceKey": next_slice_key}
    else:
        params = {"query": query, "from": from_time, "to": to_time,
                  "limit": max(1, min(limit, 1000)), "sort": sort}
    body = await c.get("/logs/search", params)
    records = [condense_log(r, c.settings.max_log_line_chars) for r in body.get("results", [])]
    out = _page(body, "results", records)
    out["count"] = len(records)
    if body.get("sliceSize") is not None:
        out["sliceSize"] = body["sliceSize"]
    return out


async def aggregate_logs(c: DynatraceClient, query: str = "", from_time: str = "now-2h", to_time: str = "now",
                         group_by: list[str] | None = None, time_buckets: int | None = None,
                         max_group_values: int | None = None) -> dict:
    params = {"query": query, "from": from_time, "to": to_time, "groupBy": group_by or [],
              "timeBuckets": time_buckets, "maxGroupValues": max_group_values}
    return await c.get("/logs/aggregate", params)


async def trace_logs(c: DynatraceClient, trace_id: str, from_time: str = "now-24h", to_time: str = "now",
                     attribute: str = "trace_id", limit: int = 200) -> dict:
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", trace_id):
        raise DynatraceError(None, "trace_id must be a hex trace id", "/logs/search")
    out = await search_logs(c, f'{attribute}="{trace_id}"', from_time, to_time, limit, "timestamp")
    out["note"] = ("Classic API v2 has no PurePath/trace endpoint; these are the log lines enriched "
                   "with this trace id. Open the trace in the Dynatrace UI for the full PurePath, or "
                   "use the DQL tools (fetch spans) on SaaS with Grail.")
    return out


# ----------------------------------------------------------------- problems

def condense_problem(p: dict) -> dict:
    start, end = p.get("startTime"), p.get("endTime")
    affected = p.get("affectedEntities") or []
    impacted = p.get("impactedEntities") or []
    out = {
        "problemId": p.get("problemId"),
        "displayId": p.get("displayId"),
        "title": p.get("title"),
        "status": p.get("status"),
        "severityLevel": p.get("severityLevel"),
        "impactLevel": p.get("impactLevel"),
        "startTime": iso(start),
        "endTime": iso(end),
        "duration": duration_text(((end if end and end > 0 else now_ms()) - start) if start else None),
        "rootCauseEntity": entity_ref(p.get("rootCauseEntity")),
        "affectedEntities": [entity_ref(e) for e in affected[:20]],
        "affectedEntityCount": len(affected),
        "impactedEntityCount": len(impacted),
        "managementZones": [z.get("name") for z in p.get("managementZones") or []],
    }
    tags = p.get("entityTags") or []
    if tags:
        out["entityTags"] = [t.get("stringRepresentation") or t.get("key") for t in tags[:20]]
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def condense_evidence(ev: dict) -> dict:
    out = {
        "evidenceType": ev.get("evidenceType"),
        "displayName": ev.get("displayName"),
        "entity": entity_ref(ev.get("entity")),
        "groupingEntity": entity_ref(ev.get("groupingEntity")),
        "rootCauseRelevant": ev.get("rootCauseRelevant"),
        "startTime": iso(ev.get("startTime")),
        "endTime": iso(ev.get("endTime")),
    }
    for key in ("eventType", "metricId", "unit", "valueBeforeChangePoint", "valueAfterChangePoint",
                "transactionalEvidenceType", "affectedUsers"):
        if ev.get(key) is not None:
            out[key] = ev[key]
    data = ev.get("data")
    if isinstance(data, dict):
        props = {p.get("key"): p.get("value") for p in data.get("properties") or [] if p.get("key")}
        if props:
            out["properties"] = props
    return {k: v for k, v in out.items() if v is not None}


async def resolve_problem_id(c: DynatraceClient, problem_id: str) -> str:
    """Accept the internal problem id or the display id shown in the UI (P-12345)."""
    problem_id = problem_id.strip()
    if not DISPLAY_ID.match(problem_id):
        return problem_id
    body = await c.get("/problems", {"problemSelector": f"displayId({_q(problem_id.upper())})",
                                     "from": "now-90d", "pageSize": 1})
    problems = body.get("problems") or []
    if not problems:
        raise DynatraceError(404, f"no problem with display id {problem_id} in the last 90 days", "/problems")
    return problems[0]["problemId"]


async def fetch_problem(c: DynatraceClient, problem_id: str) -> dict:
    pid = await resolve_problem_id(c, problem_id)
    return await c.get(f"/problems/{pid}", {"fields": PROBLEM_FIELDS})


async def list_problems(c: DynatraceClient, problem_selector: str = "", entity_selector: str = "",
                        from_time: str = "now-24h", to_time: str = "now", page_size: int = 50,
                        sort: str = "-startTime", next_page_key: str | None = None) -> dict:
    if next_page_key:
        params: dict[str, Any] = {"nextPageKey": next_page_key}
    else:
        params = {"problemSelector": problem_selector, "entitySelector": entity_selector,
                  "from": from_time, "to": to_time, "pageSize": max(1, min(page_size, 500)), "sort": sort}
    body = await c.get("/problems", params)
    return _page(body, "problems", [condense_problem(p) for p in body.get("problems", [])])


async def get_problem(c: DynatraceClient, problem_id: str) -> dict:
    p = await fetch_problem(c, problem_id)
    out = condense_problem(p)
    evidence = (p.get("evidenceDetails") or {}).get("details") or []
    rc = [condense_evidence(e) for e in evidence if e.get("rootCauseRelevant")]
    out["rootCauseEvidence"] = rc[:15]
    out["evidenceCount"] = len(evidence)
    impacts = (p.get("impactAnalysis") or {}).get("impacts") or []
    out["impacts"] = [_impact(i) for i in impacts[:20]]
    comments = (p.get("recentComments") or {}).get("comments") or []
    out["recentComments"] = [_comment(cm) for cm in comments[:5]]
    linked = p.get("linkedProblemInfo")
    if linked:
        out["linkedProblem"] = linked
    return out


def _impact(i: dict) -> dict:
    out = {"impactType": i.get("impactType"), "entity": entity_ref(i.get("impactedEntity"))}
    if i.get("estimatedAffectedUsers") is not None:
        out["estimatedAffectedUsers"] = i["estimatedAffectedUsers"]
    return out


def _comment(cm: dict) -> dict:
    return {"id": cm.get("id"), "createdAt": iso(cm.get("createdAtTimestamp")),
            "author": cm.get("authorName"), "content": cm.get("content"), "context": cm.get("context")}


async def get_problem_evidence(c: DynatraceClient, problem_id: str, root_cause_only: bool = False,
                               limit: int = 50) -> dict:
    p = await fetch_problem(c, problem_id)
    details = (p.get("evidenceDetails") or {}).get("details") or []
    if root_cause_only:
        details = [e for e in details if e.get("rootCauseRelevant")]
    details = sorted(details, key=lambda e: (not e.get("rootCauseRelevant"), e.get("startTime") or 0))
    return {"problemId": p.get("problemId"), "displayId": p.get("displayId"),
            "rootCauseEntity": entity_ref(p.get("rootCauseEntity")),
            "totalCount": len(details), "evidence": [condense_evidence(e) for e in details[:limit]]}


async def get_problem_impact(c: DynatraceClient, problem_id: str) -> dict:
    p = await fetch_problem(c, problem_id)
    return {
        "problemId": p.get("problemId"), "displayId": p.get("displayId"),
        "rootCauseEntity": entity_ref(p.get("rootCauseEntity")),
        "affectedEntities": [entity_ref(e) for e in p.get("affectedEntities") or []],
        "impactedEntities": [entity_ref(e) for e in p.get("impactedEntities") or []],
        "impacts": [_impact(i) for i in (p.get("impactAnalysis") or {}).get("impacts") or []],
    }


async def list_problem_comments(c: DynatraceClient, problem_id: str, page_size: int = 50,
                                next_page_key: str | None = None) -> dict:
    pid = await resolve_problem_id(c, problem_id)
    params = {"nextPageKey": next_page_key} if next_page_key else {"pageSize": page_size}
    body = await c.get(f"/problems/{pid}/comments", params)
    return _page(body, "comments", [_comment(cm) for cm in body.get("comments", [])])


async def add_problem_comment(c: DynatraceClient, problem_id: str, message: str, context: str = "") -> dict:
    pid = await resolve_problem_id(c, problem_id)
    body = {"message": message}
    if context:
        body["context"] = context
    result = await c.post(f"/problems/{pid}/comments", body)
    return {"ok": True, "problemId": pid, "comment": result or None}


async def close_problem(c: DynatraceClient, problem_id: str, message: str) -> dict:
    pid = await resolve_problem_id(c, problem_id)
    result = await c.post(f"/problems/{pid}/close", {"message": message})
    return {"ok": True, "problemId": pid, "result": result or None}


# ------------------------------------------------------------------- events

def condense_event(e: dict) -> dict:
    props = {p.get("key"): p.get("value") for p in e.get("properties") or [] if p.get("key")}
    out = {
        "eventId": e.get("eventId"),
        "eventType": e.get("eventType"),
        "title": e.get("title"),
        "status": e.get("status"),
        "startTime": iso(e.get("startTime")),
        "endTime": iso(e.get("endTime")),
        "entity": entity_ref(e.get("entityId")),
        "correlationId": e.get("correlationId"),
        "underMaintenance": e.get("underMaintenance") or None,
        "properties": props or None,
    }
    return {k: v for k, v in out.items() if v is not None}


async def list_events(c: DynatraceClient, event_selector: str = "", entity_selector: str = "",
                      from_time: str = "now-2h", to_time: str = "now", page_size: int = 100,
                      next_page_key: str | None = None) -> dict:
    if next_page_key:
        params: dict[str, Any] = {"nextPageKey": next_page_key}
    else:
        params = {"eventSelector": event_selector, "entitySelector": entity_selector,
                  "from": from_time, "to": to_time, "pageSize": max(1, min(page_size, 1000))}
    body = await c.get("/events", params)
    return _page(body, "events", [condense_event(e) for e in body.get("events", [])])


async def get_event(c: DynatraceClient, event_id: str) -> dict:
    return condense_event(await c.get(f"/events/{event_id}"))


# ----------------------------------------------------------------- entities

def condense_entity(e: dict, relationships: bool = True) -> dict:
    out: dict[str, Any] = {"entityId": e.get("entityId"), "type": e.get("type"),
                           "displayName": e.get("displayName")}
    if e.get("firstSeenTms"):
        out["firstSeen"] = iso(e["firstSeenTms"])
    if e.get("lastSeenTms"):
        out["lastSeen"] = iso(e["lastSeenTms"])
    if e.get("tags"):
        out["tags"] = [t.get("stringRepresentation") or t.get("key") for t in e["tags"]]
    if e.get("managementZones"):
        out["managementZones"] = [z.get("name") for z in e["managementZones"]]
    if e.get("properties"):
        out["properties"] = e["properties"]
    if relationships:
        for key in ("fromRelationships", "toRelationships"):
            rels = e.get(key)
            if rels:
                out[key] = {rel: [x.get("id") for x in ids][:25] for rel, ids in rels.items()}
    return out


async def search_entities(c: DynatraceClient, entity_selector: str, from_time: str = "now-72h",
                          to_time: str = "now", fields: str = "+tags,+managementZones",
                          page_size: int = 50, next_page_key: str | None = None) -> dict:
    if next_page_key:
        params: dict[str, Any] = {"nextPageKey": next_page_key, "fields": fields}
    else:
        params = {"entitySelector": entity_selector, "from": from_time, "to": to_time,
                  "fields": fields, "pageSize": max(1, min(page_size, 500))}
    body = await c.get("/entities", params)
    return _page(body, "entities", [condense_entity(e) for e in body.get("entities", [])])


async def get_entity(c: DynatraceClient, entity_id: str, include_relationships: bool = True,
                     from_time: str = "now-72h", to_time: str = "now") -> dict:
    fields = "+properties,+tags,+managementZones"
    if include_relationships:
        fields += ",+fromRelationships,+toRelationships"
    body = await c.get(f"/entities/{entity_id}", {"fields": fields, "from": from_time, "to": to_time})
    return condense_entity(body, include_relationships)


async def find_entities(c: DynatraceClient, name: str, entity_type: str = "SERVICE", limit: int = 20) -> dict:
    etype = entity_type.upper()
    exact = await search_entities(c, f'type({_q(etype)}),entityName.equals({_q(name)})', page_size=limit)
    if exact["entities"]:
        exact["match"] = "exact"
        return exact
    fuzzy = await search_entities(c, f'type({_q(etype)}),entityName({_q(name)})', page_size=limit)
    fuzzy["match"] = "contains"
    return fuzzy


# ------------------------------------------------------------------ metrics

def summarize_series(values: list) -> dict:
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return {"points": len(values), "nonNull": 0}
    return {"points": len(values), "nonNull": len(nums), "min": round(min(nums), 4),
            "max": round(max(nums), 4), "avg": round(sum(nums) / len(nums), 4), "last": round(nums[-1], 4)}


async def query_metrics(c: DynatraceClient, metric_selector: str, entity_selector: str = "",
                        from_time: str = "now-2h", to_time: str = "now", resolution: str = "",
                        mz_selector: str = "", include_datapoints: bool = False,
                        max_series: int = 20) -> dict:
    params = {"metricSelector": metric_selector, "entitySelector": entity_selector, "from": from_time,
              "to": to_time, "resolution": resolution, "mzSelector": mz_selector}
    body = await c.get("/metrics/query", params)
    results = []
    for res in body.get("result", []):
        series = []
        for d in res.get("data", [])[:max_series]:
            item: dict[str, Any] = {"dimensions": d.get("dimensionMap") or d.get("dimensions"),
                                    "summary": summarize_series(d.get("values") or [])}
            if include_datapoints:
                item["datapoints"] = [[iso(t), v] for t, v in zip(d.get("timestamps") or [], d.get("values") or [])]
            series.append(item)
        entry = {"metricId": res.get("metricId"), "seriesCount": len(res.get("data", [])), "series": series}
        for warn_key in ("warnings", "dataPointCountRatio", "dimensionCountRatio"):
            if res.get(warn_key):
                entry[warn_key] = res[warn_key]
        results.append(entry)
    out = {"resolution": body.get("resolution"), "result": results}
    if body.get("nextPageKey"):
        out["nextPageKey"] = body["nextPageKey"]
    return out


async def list_metrics(c: DynatraceClient, text: str = "", metric_selector: str = "", page_size: int = 50,
                       next_page_key: str | None = None) -> dict:
    if next_page_key:
        params: dict[str, Any] = {"nextPageKey": next_page_key}
    else:
        params = {"text": text, "metricSelector": metric_selector, "pageSize": max(1, min(page_size, 500)),
                  "fields": "displayName,description,unit,dimensionDefinitions,entityType"}
    body = await c.get("/metrics", params)
    metrics = [{"metricId": m.get("metricId"), "displayName": m.get("displayName"), "unit": m.get("unit"),
                "description": (m.get("description") or "")[:300] or None,
                "entityType": m.get("entityType"),
                "dimensions": [d.get("key") for d in m.get("dimensionDefinitions") or []]}
               for m in body.get("metrics", [])]
    return _page(body, "metrics", [{k: v for k, v in m.items() if v not in (None, [])} for m in metrics])


async def get_metric_descriptor(c: DynatraceClient, metric_key: str) -> dict:
    return await c.get(f"/metrics/{metric_key}")


# ------------------------------------------------------- security and SLOs

def condense_security_problem(s: dict) -> dict:
    risk = s.get("riskAssessment") or {}
    out = {
        "securityProblemId": s.get("securityProblemId"),
        "displayId": s.get("displayId"),
        "title": s.get("title"),
        "status": s.get("status"),
        "muted": s.get("muted"),
        "vulnerabilityType": s.get("vulnerabilityType"),
        "technology": s.get("technology"),
        "cveIds": s.get("cveIds"),
        "riskLevel": risk.get("riskLevel"),
        "riskScore": risk.get("riskScore"),
        "exposure": risk.get("exposure"),
        "publicExploit": risk.get("publicExploit"),
        "firstSeen": iso(s.get("firstSeenTimestamp")),
        "lastUpdated": iso(s.get("lastUpdatedTimestamp")),
        "url": s.get("url"),
    }
    for key in ("description", "remediationDescription", "affectedEntities", "exposedEntities",
                "relatedEntities", "globalCounts", "managementZones"):
        if s.get(key):
            out[key] = s[key]
    return {k: v for k, v in out.items() if v not in (None, [], "")}


async def list_security_problems(c: DynatraceClient, selector: str = "", from_time: str = "now-30d",
                                 to_time: str = "now", sort: str = "-riskAssessment.riskScore",
                                 page_size: int = 50, next_page_key: str | None = None) -> dict:
    if next_page_key:
        params: dict[str, Any] = {"nextPageKey": next_page_key}
    else:
        params = {"securityProblemSelector": selector, "from": from_time, "to": to_time, "sort": sort,
                  "pageSize": max(1, min(page_size, 500)), "fields": "+riskAssessment,+managementZones"}
    body = await c.get("/securityProblems", params)
    return _page(body, "securityProblems",
                 [condense_security_problem(s) for s in body.get("securityProblems", [])])


async def get_security_problem(c: DynatraceClient, security_problem_id: str) -> dict:
    fields = ("+riskAssessment,+managementZones,+codeLevelVulnerabilityDetails,+globalCounts,"
              "+description,+remediationDescription,+affectedEntities,+exposedEntities,+relatedEntities")
    return condense_security_problem(await c.get(f"/securityProblems/{security_problem_id}", {"fields": fields}))


def condense_slo(s: dict) -> dict:
    out = {k: s.get(k) for k in ("id", "name", "status", "evaluatedPercentage", "target", "warning",
                                 "errorBudget", "errorBudgetBurnRate", "timeframe", "enabled",
                                 "metricExpression", "evaluationType", "filter", "description", "error")}
    burn = out.get("errorBudgetBurnRate")
    if isinstance(burn, dict):
        out["errorBudgetBurnRate"] = {k: burn.get(k) for k in ("burnRateValue", "burnRateType",
                                                                "estimatedTimeToConsumeErrorBudget")}
    return {k: v for k, v in out.items() if v not in (None, "", "NONE")}


async def list_slos(c: DynatraceClient, slo_selector: str = "", from_time: str = "", to_time: str = "",
                    evaluate: bool = True, page_size: int = 25, next_page_key: str | None = None) -> dict:
    if next_page_key:
        params: dict[str, Any] = {"nextPageKey": next_page_key}
    else:
        params = {"sloSelector": slo_selector, "from": from_time, "to": to_time,
                  "evaluate": "true" if evaluate else "false", "pageSize": max(1, min(page_size, 25)),
                  "timeFrame": "GTF" if from_time or to_time else "CURRENT"}
    body = await c.get("/slo", params)
    return _page(body, "slos", [condense_slo(s) for s in body.get("slo", [])])


async def get_slo(c: DynatraceClient, slo_id: str, from_time: str = "", to_time: str = "") -> dict:
    params = {"from": from_time, "to": to_time, "timeFrame": "GTF" if from_time or to_time else "CURRENT"}
    return condense_slo(await c.get(f"/slo/{slo_id}", params))


# ------------------------------------------------------------ diagnostics

PROBES = [
    ("problems.read", "/problems", {"pageSize": 1, "from": "now-1h"}),
    ("events.read", "/events", {"pageSize": 1, "from": "now-1h"}),
    ("entities.read", "/entities", {"entitySelector": 'type("HOST")', "pageSize": 1}),
    ("metrics.read", "/metrics", {"pageSize": 1}),
    ("logs.read", "/logs/search", {"limit": 1, "from": "now-5m"}),
    ("securityProblems.read", "/securityProblems", {"pageSize": 1}),
    ("slo.read", "/slo", {"pageSize": 1, "evaluate": "false"}),
]


async def check_connection(c: DynatraceClient) -> dict:
    async def probe(scope: str, path: str, params: dict) -> dict:
        try:
            await c.get(path, params)
            return {"scope": scope, "ok": True}
        except DynatraceError as err:
            return {"scope": scope, "ok": False, "status": err.status, "error": err.message}

    results = await asyncio.gather(*(probe(*p) for p in PROBES))
    reachable = any(r["ok"] or r.get("status") is not None for r in results)
    auth_failed = all(r.get("status") == 401 for r in results)
    return {
        "settings": c.settings.describe(),
        "reachable": reachable,
        "authenticated": reachable and not auth_failed,
        "scopes": results,
        "missingScopes": [r["scope"] for r in results if r.get("status") == 403],
    }


# ---------------------------------------------------------- optional DQL

async def execute_dql(c: DynatraceClient, query: str, max_records: int = 200, timeout_seconds: int = 60) -> dict:
    """Run a DQL query on Grail (SaaS platform only; needs DT_PLATFORM_URL and DT_PLATFORM_TOKEN)."""
    body = {"query": query, "maxResultRecords": max(1, min(max_records, 5000)),
            "requestTimeoutMilliseconds": 10_000}
    started = await c.platform_post("/platform/storage/query/v1/query:execute", body)
    deadline = now_ms() + timeout_seconds * 1000
    while started.get("state") in ("RUNNING", "NOT_STARTED") and now_ms() < deadline:
        started = await c.platform_get("/platform/storage/query/v1/query:poll",
                                       {"request-token": started.get("requestToken"),
                                        "request-timeout-milliseconds": 5000})
    result = started.get("result") or {}
    records = result.get("records") or []
    return {"state": started.get("state"), "recordCount": len(records), "records": records,
            "metadata": (result.get("metadata") or {}).get("grail")}
