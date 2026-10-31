"""Composite tools: problem RCA and service debugging.

Both tools fan out to several read-only endpoints, tolerate partial failures
(for example a token without ``logs.read``), and return one structured report
plus a short Markdown summary the agent can quote.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from typing import Any, Awaitable

from . import api
from .client import DynatraceClient, DynatraceError
from .timeutil import iso, now_ms, resolution_for, to_ms

CHANGE_EVENT_TYPES = ("CUSTOM_DEPLOYMENT", "CUSTOM_CONFIGURATION", "PROCESS_RESTART", "CUSTOM_ANNOTATION")

# metric selector, label, unit, scale applied to values
KEY_METRICS: dict[str, list[tuple[str, str, str, float]]] = {
    "SERVICE": [
        ("builtin:service.errors.total.rate", "Failure rate", "%", 1.0),
        ("builtin:service.errors.total.count", "Failed requests", "count", 1.0),
        ("builtin:service.requestCount.total", "Requests", "count", 1.0),
        ("builtin:service.response.time:avg", "Response time (avg)", "ms", 0.001),
        ("builtin:service.response.time:percentile(90)", "Response time (p90)", "ms", 0.001),
    ],
    "HOST": [
        ("builtin:host.cpu.usage", "Host CPU", "%", 1.0),
        ("builtin:host.mem.usage", "Host memory", "%", 1.0),
        ("builtin:host.disk.usedPct:merge(\"dt.entity.disk\"):max", "Fullest disk", "%", 1.0),
    ],
    "PROCESS_GROUP_INSTANCE": [
        ("builtin:tech.generic.cpu.usage", "Process CPU", "%", 1.0),
        ("builtin:tech.generic.mem.workingSetSize", "Process memory (working set)", "MiB", 1 / 1048576),
    ],
}
KEY_METRICS["PROCESS_GROUP"] = KEY_METRICS["PROCESS_GROUP_INSTANCE"]

LOG_ATTRIBUTE = {
    "PROCESS_GROUP": "dt.entity.process_group",
    "PROCESS_GROUP_INSTANCE": "dt.entity.process_group_instance",
    "HOST": "dt.entity.host",
    "KUBERNETES_CLUSTER": "dt.entity.kubernetes_cluster",
}

# Relationship names that point from an entity to where it runs.
RUNS_ON = ("runsOn", "runsOnProcessGroupInstance", "isInstanceOf", "isProcessOf", "runsOnHost")


def entity_type(entity_id: str | None) -> str:
    return entity_id.rsplit("-", 1)[0] if entity_id and "-" in entity_id else ""


class Collector:
    """Runs sub-queries and records failures instead of aborting the whole report."""

    def __init__(self) -> None:
        self.errors: list[dict] = []

    async def run(self, label: str, coro: Awaitable[Any], default: Any = None) -> Any:
        try:
            return await coro
        except DynatraceError as err:
            self.errors.append({"step": label, **err.to_dict()})
            return default


# ------------------------------------------------------------ log helpers

_NORMALIZERS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def log_signature(content: str) -> str:
    first = (content or "").strip().splitlines()[0] if (content or "").strip() else ""
    for pattern, repl in _NORMALIZERS:
        first = pattern.sub(repl, first)
    return first[:200]


def group_logs(records: list[dict], top: int = 10) -> list[dict]:
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for r in records:
        sig = log_signature(r.get("content", ""))
        g = groups.get(sig)
        if g is None:
            groups[sig] = g = {"signature": sig, "count": 0, "firstSeen": r.get("timestamp"),
                               "lastSeen": r.get("timestamp"), "sample": r.get("content", "")[:600]}
        g["count"] += 1
        ts = r.get("timestamp")
        if ts:
            g["firstSeen"] = min(filter(None, [g["firstSeen"], ts]))
            g["lastSeen"] = max(filter(None, [g["lastSeen"], ts]))
    return sorted(groups.values(), key=lambda g: -g["count"])[:top]


def log_scope(entity_ids: list[str]) -> str | None:
    """Build a log query clause selecting logs of the given entities.

    Classic logs are tagged with host/process group (instance) ids, not service
    ids, so prefer process groups, then instances, then hosts.
    """
    by_type: dict[str, list[str]] = {}
    for eid in entity_ids:
        by_type.setdefault(entity_type(eid), []).append(eid)
    for etype in ("PROCESS_GROUP", "PROCESS_GROUP_INSTANCE", "HOST", "KUBERNETES_CLUSTER"):
        ids = by_type.get(etype)
        if ids:
            attr = LOG_ATTRIBUTE[etype]
            return "(" + " OR ".join(f'{attr}="{i}"' for i in ids[:10]) + ")"
    return None


async def error_logs(c: DynatraceClient, col: Collector, entity_ids: list[str], from_time: Any, to_time: Any,
                     limit: int, log_filter: str) -> dict:
    scope = log_scope(entity_ids)
    if not scope:
        return {"skipped": "no host or process group found to scope the log query; use search_logs"}
    query = f"{log_filter} AND {scope}" if log_filter else scope
    res = await col.run("error logs", api.search_logs(c, query, str(from_time), str(to_time), limit))
    if res is None:
        return {"query": query, "skipped": "log search failed (see errors)"}
    records = res.get("results", [])
    return {"query": query, "count": len(records), "moreAvailable": bool(res.get("nextSliceKey")),
            "topSignatures": group_logs(records), "latest": records[:5]}


# --------------------------------------------------------- metric helpers

async def metric_trends(c: DynatraceClient, col: Collector, entity_id: str, from_ms: int, to_ms_: int,
                        split_ms: int | None = None) -> list[dict]:
    """Key metrics of one entity, with before/after comparison around ``split_ms``."""
    etype = entity_type(entity_id)
    metrics = KEY_METRICS.get(etype, [])
    if not metrics:
        return []
    if etype == "PROCESS_GROUP":
        selector = f'type("PROCESS_GROUP_INSTANCE"),fromRelationships.isInstanceOf({api.entity_id_selector([entity_id])})'
    else:
        selector = api.entity_id_selector([entity_id])
    resolution = resolution_for(to_ms_ - from_ms)

    async def one(metric: str, label: str, unit: str, scale: float) -> dict | None:
        body = await col.run(f"metric {metric}", c.get("/metrics/query", {
            "metricSelector": metric, "entitySelector": selector, "from": from_ms, "to": to_ms_,
            "resolution": resolution}))
        if not body:
            return None
        points: list[tuple[int, float]] = []
        for res in body.get("result", []):
            for d in res.get("data", []):
                points += [(t, v * scale) for t, v in zip(d.get("timestamps") or [], d.get("values") or [])
                           if isinstance(v, (int, float))]
        if not points:
            return {"metric": label, "unit": unit, "data": "no data"}
        values = [v for _, v in points]
        peak_t, peak_v = max(points, key=lambda p: p[1])
        out: dict[str, Any] = {"metric": label, "unit": unit, "avg": round(sum(values) / len(values), 3),
                               "max": round(peak_v, 3), "peakAt": iso(peak_t), "last": round(points[-1][1], 3)}
        if split_ms:
            before = [v for t, v in points if t < split_ms]
            during = [v for t, v in points if t >= split_ms]
            if before and during:
                b, d = sum(before) / len(before), sum(during) / len(during)
                out["baselineAvg"], out["duringAvg"] = round(b, 3), round(d, 3)
                if b:
                    out["changePct"] = round((d - b) / abs(b) * 100, 1)
        return out

    results = await asyncio.gather(*(one(*m) for m in metrics))
    return [r for r in results if r]


# ---------------------------------------------------------- event helpers

def changes_before(events: list[dict], start_ms: int, window_ms: int) -> list[dict]:
    out = []
    for e in events:
        etype = e.get("eventType", "")
        title = (e.get("title") or "").lower()
        if etype not in CHANGE_EVENT_TYPES and "deploy" not in title and "release" not in title:
            continue
        t = to_ms(e.get("startTime"))
        if t is None or not (start_ms - window_ms <= t <= start_ms + 5 * 60_000):
            continue
        out.append({**e, "minutesBeforeProblem": round((start_ms - t) / 60_000, 1)})
    return sorted(out, key=lambda e: e["minutesBeforeProblem"])


def related_ids(entity: dict | None) -> list[str]:
    if not entity:
        return []
    ids: list[str] = []
    rels = entity.get("fromRelationships") or {}
    for key in RUNS_ON:
        ids += rels.get(key, [])
    return list(dict.fromkeys(ids))[:25]


# ----------------------------------------------------------------- RCA

async def investigate_problem(c: DynatraceClient, problem_id: str, lookback_minutes: int = 60,
                              log_limit: int = 200, log_filter: str = 'status="ERROR"') -> dict:
    col = Collector()
    raw = await api.fetch_problem(c, problem_id)  # fail fast: no problem, no report
    problem = api.condense_problem(raw)
    start = raw.get("startTime") or now_ms()
    end = raw.get("endTime") if (raw.get("endTime") or -1) > 0 else now_ms()
    lookback = max(5, lookback_minutes) * 60_000
    win_from, win_to = start - lookback, min(now_ms(), end + 5 * 60_000)

    evidence = (raw.get("evidenceDetails") or {}).get("details") or []
    rc_evidence = [api.condense_evidence(e) for e in evidence if e.get("rootCauseRelevant")]
    root = api.entity_ref(raw.get("rootCauseEntity"))
    affected_ids = [r["id"] for r in (api.entity_ref(e) for e in raw.get("affectedEntities") or []) if r and r.get("id")]
    root_id = (root or {}).get("id") or (affected_ids[0] if affected_ids else None)

    root_entity = None
    if root_id:
        root_entity = await col.run("root cause entity", api.get_entity(c, root_id, True, str(win_from), str(win_to)))
    runs_on = related_ids(root_entity)
    scope_ids = list(dict.fromkeys(([root_id] if root_id else []) + affected_ids[:10] + runs_on))

    events_task = col.run("events", api.list_events(
        c, entity_selector=api.entity_id_selector(scope_ids[:50]) if scope_ids else "",
        from_time=str(win_from), to_time=str(win_to), page_size=200), {"events": []})
    logs_task = error_logs(c, col, scope_ids, win_from, win_to, log_limit, log_filter)
    metrics_task = metric_trends(c, col, root_id, win_from, win_to, start) if root_id else asyncio.sleep(0, [])
    events_res, logs_res, metrics_res = await asyncio.gather(events_task, logs_task, metrics_task)

    events = [e for e in events_res.get("events", [])]
    changes = changes_before(events, start, lookback)
    timeline = sorted(events, key=lambda e: e.get("startTime") or "")[:40]

    report = {
        "problem": problem,
        "window": {"from": iso(win_from), "to": iso(win_to), "problemStart": iso(start),
                   "problemEnd": iso(raw.get("endTime")) or "open"},
        "rootCause": {"entity": root, "details": root_entity, "evidence": rc_evidence[:15],
                      "otherEvidenceCount": len(evidence) - len(rc_evidence)},
        "impact": [api._impact(i) for i in ((raw.get("impactAnalysis") or {}).get("impacts") or [])[:15]],
        "changesBeforeProblem": changes,
        "eventTimeline": timeline,
        "errorLogs": logs_res,
        "keyMetrics": metrics_res,
        "comments": [api._comment(cm) for cm in ((raw.get("recentComments") or {}).get("comments") or [])[:5]],
        "errors": col.errors,
    }
    report["findings"] = _rca_findings(report)
    report["summary"] = _rca_summary(report)
    report["nextSteps"] = _rca_next_steps(report)
    return report


def _metric_findings(metrics: list[dict], who: str) -> list[str]:
    out = []
    for m in metrics:
        pct = m.get("changePct")
        if pct is not None and abs(pct) >= 50 and (m.get("duringAvg") or 0) > 0:
            direction = "up" if pct > 0 else "down"
            out.append(f"{m['metric']} on {who} went {direction} {abs(pct)}% "
                       f"({m['baselineAvg']} -> {m['duringAvg']} {m['unit']}).")
        elif m.get("unit") == "%" and (m.get("max") or 0) >= 90 and "rate" not in m["metric"].lower():
            out.append(f"{m['metric']} on {who} peaked at {m['max']}% at {m['peakAt']}.")
    return out


def _rca_findings(r: dict) -> list[str]:
    findings = []
    root = r["rootCause"]["entity"]
    who = (root or {}).get("name") or (root or {}).get("id") or "the affected entity"
    if root:
        findings.append(f"Dynatrace marks {who} ({root.get('type') or entity_type(root.get('id'))}) as the root cause.")
    else:
        findings.append("Dynatrace found no root-cause entity; the analysis uses the first affected entity.")
    for ev in r["rootCause"]["evidence"][:5]:
        findings.append(f"Root-cause evidence: {ev.get('displayName')} on "
                        f"{(ev.get('entity') or {}).get('name') or (ev.get('entity') or {}).get('id')}"
                        f" since {ev.get('startTime')}.")
    for ch in r["changesBeforeProblem"][:3]:
        findings.append(f"Change before the problem: {ch.get('eventType')} '{ch.get('title')}' on "
                        f"{(ch.get('entity') or {}).get('name') or (ch.get('entity') or {}).get('id')} "
                        f"{ch['minutesBeforeProblem']} min before start. Likely trigger; verify.")
    findings += _metric_findings(r["keyMetrics"], who)
    logs = r["errorLogs"]
    if logs.get("topSignatures"):
        top = logs["topSignatures"][0]
        findings.append(f"{logs['count']} error log lines in the window; the most frequent "
                        f"({top['count']}x): {top['signature'][:160]}")
    if r["errors"]:
        findings.append(f"{len(r['errors'])} sub-queries failed (see errors); the report is partial.")
    return findings


def _rca_summary(r: dict) -> str:
    p = r["problem"]
    lines = [f"### {p.get('displayId', '')} {p.get('title', '')}".strip(),
             f"- Status: {p.get('status')} | severity {p.get('severityLevel')} | impact {p.get('impactLevel')}"
             f" | started {p.get('startTime')} | duration {p.get('duration')}",
             f"- Affected entities: {p.get('affectedEntityCount', 0)}, impacted: {p.get('impactedEntityCount', 0)}",
             "", "**Findings**"]
    lines += [f"{i}. {f}" for i, f in enumerate(r["findings"], 1)]
    return "\n".join(lines)


def _rca_next_steps(r: dict) -> list[str]:
    steps = []
    root = (r["rootCause"]["entity"] or {}).get("id")
    if r["changesBeforeProblem"]:
        steps.append("Compare the deployment/config change with the error signatures; consider a rollback.")
    if root and entity_type(root) == "SERVICE":
        steps.append(f"debug_service(service='{root}') for failure rate, failing logs and recent deployments.")
    if r["errorLogs"].get("moreAvailable"):
        steps.append(f"search_logs(query='{r['errorLogs'].get('query')}') to page through more error logs.")
    if any(e.get("status") == 403 for e in r["errors"]):
        steps.append("Add the missing token scopes listed in errors, then rerun investigate_problem.")
    steps.append("get_problem_evidence(problem_id, root_cause_only=false) for the full evidence list.")
    return steps


# ------------------------------------------------------ service debugging

async def resolve_service(c: DynatraceClient, service: str) -> tuple[str | None, list[dict]]:
    service = service.strip()
    if re.fullmatch(r"SERVICE-[0-9A-F]{16}", service, re.I):
        return service.upper(), []
    found = await api.find_entities(c, service, "SERVICE", 10)
    candidates = found.get("entities", [])
    if len(candidates) == 1:
        return candidates[0]["entityId"], []
    return None, candidates


async def debug_service(c: DynatraceClient, service: str, from_time: str = "now-2h", to_time: str = "now",
                        deployments_from: str = "now-7d", log_limit: int = 200,
                        log_filter: str = 'status="ERROR"') -> dict:
    col = Collector()
    service_id, candidates = await resolve_service(c, service)
    if not service_id:
        return {"resolved": False, "message": f"No single service matches {service!r}. "
                "Call debug_service again with one of these entity ids.",
                "candidates": [{"entityId": e["entityId"], "displayName": e.get("displayName")} for e in candidates]}

    now = now_ms()
    f_ms, t_ms = to_ms(from_time, now) or now - 7_200_000, to_ms(to_time, now) or now
    d_ms = to_ms(deployments_from, now) or f_ms - 7 * 86_400_000
    entity = await col.run("service entity", api.get_entity(c, service_id, True, str(min(f_ms, d_ms)), str(t_ms)))
    runs_on = related_ids(entity)
    scope = [service_id] + runs_on

    metrics_task = metric_trends(c, col, service_id, f_ms, t_ms, f_ms + (t_ms - f_ms) // 2)
    logs_task = error_logs(c, col, runs_on, f_ms, t_ms, log_limit, log_filter)
    deploy_task = col.run("deployments", api.list_events(
        c, event_selector="eventType(" + ",".join(f'"{t}"' for t in CHANGE_EVENT_TYPES) + ")",
        entity_selector=api.entity_id_selector(scope[:50]), from_time=str(min(d_ms, f_ms)),
        to_time=str(t_ms), page_size=50), {"events": []})
    problems_task = col.run("problems", api.list_problems(
        c, entity_selector=api.entity_id_selector([service_id]), from_time=str(f_ms), to_time=str(t_ms),
        page_size=20), {"problems": []})
    metrics_res, logs_res, deploys, problems = await asyncio.gather(metrics_task, logs_task, deploy_task, problems_task)

    deployments = sorted((e for e in deploys.get("events", []) if e.get("eventType") in CHANGE_EVENT_TYPES),
                         key=lambda e: e.get("startTime") or "", reverse=True)
    rels = (entity or {}).get("fromRelationships") or {}
    callers = ((entity or {}).get("toRelationships") or {}).get("calls", [])
    report = {
        "service": {"entityId": service_id, "displayName": (entity or {}).get("displayName"),
                    "properties": _service_props(entity), "tags": (entity or {}).get("tags"),
                    "runsOn": runs_on, "calls": rels.get("calls", [])[:15], "calledBy": callers[:15]},
        "window": {"from": iso(f_ms), "to": iso(t_ms),
                   "comparison": "metrics compare the first half of the window (baseline) with the second half"},
        "keyMetrics": metrics_res,
        "failureLogs": logs_res,
        "recentDeployments": deployments[:20],
        "problems": problems.get("problems", []),
        "errors": col.errors,
    }
    report["findings"] = _service_findings(report, f_ms)
    report["summary"] = "\n".join([f"### Service {report['service']['displayName'] or service_id}",
                                   f"- Window {iso(f_ms)} to {iso(t_ms)}", "", "**Findings**"] +
                                  [f"{i}. {f}" for i, f in enumerate(report["findings"], 1)])
    return report


def _service_props(entity: dict | None) -> dict | None:
    props = (entity or {}).get("properties") or {}
    keep = ("serviceType", "serviceTechnologyTypes", "agentTechnologyType", "webServerName",
            "webApplicationId", "contextRoot", "ipAddress", "port", "databaseName", "databaseVendor")
    return {k: props[k] for k in keep if k in props} or None


def _service_findings(r: dict, f_ms: int) -> list[str]:
    name = r["service"]["displayName"] or r["service"]["entityId"]
    out = []
    for m in r["keyMetrics"]:
        if m.get("metric") == "Failure rate" and isinstance(m.get("avg"), (int, float)):
            out.append(f"Failure rate avg {m['avg']}%, peak {m['max']}% at {m['peakAt']}, last {m['last']}%.")
    out += _metric_findings(r["keyMetrics"], name)
    open_problems = [p for p in r["problems"] if p.get("status") == "OPEN"]
    if open_problems:
        out.append(f"{len(open_problems)} open problem(s): " +
                   "; ".join(f"{p.get('displayId')} {p.get('title')}" for p in open_problems[:3]))
    in_window = [d for d in r["recentDeployments"] if (to_ms(d.get("startTime")) or 0) >= f_ms]
    if in_window:
        d = in_window[0]
        out.append(f"Deployment/change inside the window: {d.get('eventType')} '{d.get('title')}' at "
                   f"{d.get('startTime')}. Check whether failures started after it.")
    elif r["recentDeployments"]:
        d = r["recentDeployments"][0]
        out.append(f"Last deployment/change: {d.get('eventType')} '{d.get('title')}' at {d.get('startTime')}.")
    else:
        out.append("No deployment or configuration events found for the service or its process groups.")
    logs = r["failureLogs"]
    if logs.get("topSignatures"):
        top = logs["topSignatures"][0]
        out.append(f"{logs['count']} error log lines; top signature ({top['count']}x): {top['signature'][:160]}")
    elif logs.get("skipped"):
        out.append(f"Logs: {logs['skipped']}")
    if r["errors"]:
        out.append(f"{len(r['errors'])} sub-queries failed (see errors); the report is partial.")
    return out

