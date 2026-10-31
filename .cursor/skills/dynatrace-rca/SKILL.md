---
name: dynatrace-rca
description: Root-cause analysis of Dynatrace problems and misbehaving services with the dynatrace MCP server (Managed or SaaS, classic API v2). Use when the user mentions a Dynatrace problem (P-12345), an incident, alert, outage, error spike, latency regression, failing service, or asks "why is X failing/slow". Read-only by default; never closes or comments on problems without explicit confirmation.
metadata:
  version: "1.0.0"
  requires: "dynatrace MCP server (this repo)"
---

# Dynatrace RCA

Goal: an RCA the user can paste into an incident ticket, where every claim cites the tool call that shows it. Facts come from tool output, not from what such incidents usually look like.

## 0. Preconditions

- If any tool fails with "not configured", 401, 403 or a TLS error, call `check_connection` and report `missingScopes` / hints. Do not guess around missing data.
- Never ask the user to paste the API token into chat. Tokens live in the environment Cursor starts the server from.

## 1. Frame

Pin down, in one line each: what is broken (service, app, host), since when, and the problem id if there is one.

- Problem id given (`P-12345` or internal id): go to step 2.
- No id: `list_problems(problem_selector='status("OPEN")')`, optionally with `entity_selector='type("SERVICE"),entityName.equals("<name>")'`. Pick the problem that matches the user's symptom and say which one you picked.
- Only a service name: `debug_service(service="<name>")`. If it returns `candidates`, ask which one.

## 2. Investigate

Call `investigate_problem(problem_id)` once. It returns:

- `rootCause` (entity Dynatrace blames, its relationships, root-cause evidence)
- `changesBeforeProblem` (deployments, config changes, restarts before the start, with minutes before)
- `errorLogs.topSignatures` (error logs in the window, grouped by normalized message)
- `keyMetrics` (baseline vs during, `changePct`)
- `errors` (sub-queries that failed; the report is partial if this is non-empty)

Read `findings` and `nextSteps`, then verify. Do not stop at the first plausible story.

## 3. Verify (pick what applies)

| Question | Tool call |
| --- | --- |
| What exactly did Dynatrace detect? | `get_problem_evidence(problem_id, root_cause_only=false)` |
| Did a change trigger it? | `get_event(event_id)` for each change; compare its time with the first error log and the metric `peakAt` |
| What do the errors say? | `search_logs(query='status="ERROR" AND content="<signature words>"', from_time=..., to_time=...)` |
| Is it this instance or all of them? | `query_metrics(metric_selector='builtin:service.errors.total.rate:splitBy("dt.entity.service")', ...)` or split by `dt.entity.process_group_instance` |
| Upstream or downstream? | `get_entity(root_cause_id)` then `debug_service` on ids in `calls` (callees) and `calledBy` |
| Infrastructure? | `get_entity` on the host / process group from `runsOn`, then `query_metrics` with `builtin:host.cpu.usage`, `builtin:host.mem.usage`, `builtin:tech.generic.mem.workingSetSize` |
| Users impacted? | `get_problem_impact(problem_id)` |
| SLO burn? | `list_slos(slo_selector='text("<service>")')` |
| Known vulnerability involved? | `list_security_problems(selector='status("OPEN")')` |

Time windows: use the report's `window.from` / `window.to`. Keep log queries narrow (minutes, not days) and page with `next_slice_key` instead of raising limits.

## 4. Report

Use this shape. Mark each statement **Confirmed** (a tool output shows it), **Likely** (correlated in time, not proven) or **Unknown**.

```markdown
## RCA: <displayId> <title>
**Status:** OPEN|CLOSED · **Severity:** ... · **Window:** <start> to <end> (<duration>)
**Impact:** <users/apps/services, from get_problem_impact>

### Root cause (Confirmed|Likely)
<one paragraph: entity, mechanism, trigger>

### Timeline (UTC)
- <time> <change/event> (<tool>)
- <time> first error: <signature> (<tool>)
- <time> problem opened (<tool>)

### Evidence
- <metric: baseline -> during, changePct> (`investigate_problem.keyMetrics`)
- <log signature, count> (`search_logs ...`)
- <Dynatrace evidence item> (`get_problem_evidence`)

### Contributing factors
### Mitigation / next steps
### Open questions (what the data could not show)
```

## 5. Write-back (only if the user asks)

`add_problem_comment` and `close_problem` exist only when the server runs with `DT_ENABLE_WRITE=true`. Before calling either, show the exact comment text and get an explicit "yes". Never close a problem that is still `OPEN` in Dynatrace's own analysis unless the user insists.

## Syntax cheat sheet

- Entity selector: `type("SERVICE"),entityName.equals("checkout")`, `type("HOST"),tag("env:prod")`, `entityId("SERVICE-ABC","SERVICE-DEF")`, `type("PROCESS_GROUP_INSTANCE"),fromRelationships.isInstanceOf(entityId("PROCESS_GROUP-..."))`
- Problem selector: `status("OPEN")`, `severityLevel("ERROR")`, `impactLevel("APPLICATION")`, `rootCauseEntity("SERVICE-...")`, `displayId("P-12345")`
- Event selector: `eventType("CUSTOM_DEPLOYMENT","CUSTOM_CONFIGURATION")`, `status("OPEN")`
- Log query: `status="ERROR" AND content="timeout"`, `dt.entity.process_group="PROCESS_GROUP-..."`, `log.source="/var/log/app.log"`
- Metric selector: `builtin:service.response.time:percentile(90)` (microseconds), `builtin:service.errors.total.rate`, `builtin:host.cpu.usage:splitBy("dt.entity.host"):max`, `...:filter(eq("dt.entity.service","SERVICE-..."))`
- Times: `now-2h`, `2026-10-31T10:00:00Z`, epoch ms
