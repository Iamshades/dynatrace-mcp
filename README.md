# dynatrace-mcp

An MCP server that gives Cursor's agent **read-only access to Dynatrace**: logs, problems, events, entities, metrics, security problems and SLOs, plus two composite tools for **root-cause analysis** (`investigate_problem`) and **service debugging** (`debug_service`).

It targets the **classic Environment API v2** with an **API token** (`Authorization: Api-Token ...`), so it works on **self-hosted Dynatrace Managed** (`https://<host>/e/<env-id>`) as well as **SaaS** (`https://<env>.live.dynatrace.com`). Grail/DQL is an optional extra for SaaS environments that have it.

```text
Cursor agent ──stdio──► dynatrace-mcp ──HTTPS + Api-Token──► Dynatrace Managed / SaaS  /api/v2/...
                         │ read-only by default, secrets redacted, output size-bounded
                         └─ optional: Grail DQL (platform token), write tools (opt-in flag)
```

## Why this and not the official server

[dynatrace-oss/dynatrace-mcp](https://github.com/dynatrace-oss/dynatrace-mcp) is built on the Dynatrace **platform** (Grail, DQL, OAuth clients and platform tokens). Those APIs do not exist on Dynatrace **Managed**, and many companies only hand out classic API tokens. This server uses only API v2 endpoints that Managed supports, and borrows the official server's ideas where they fit (problem-centric workflow, entity lookup by name, DQL when available).

**Why Python:** the `mcp` Python SDK (FastMCP) builds tool schemas from type hints, and it already ships `httpx` and `pydantic`, which cover custom CA bundles, proxies and TLS options. So the server has **one runtime dependency** (`mcp`) and the tests mock HTTP with `httpx.MockTransport` without extra libraries. `uv` handles the Python version, so the machine's system Python does not matter.

## Tools

Read-only (always on):

| Area | Tool | API |
| --- | --- | --- |
| Setup | `check_connection` | Shows the active config (no secrets) and probes which token scopes work |
| Logs | `search_logs` | `GET /logs/search` (query, time range, limit, sort, `nextSliceKey` paging) |
| | `aggregate_logs` | `GET /logs/aggregate` (group by attributes, time buckets) |
| | `get_trace_logs` | Log lines enriched with a trace id (classic API has no PurePath endpoint) |
| Problems | `list_problems` | `GET /problems` (problem and entity selectors, paging) |
| | `get_problem` | `GET /problems/{id}`: status, root-cause entity, root-cause evidence, impacts, recent comments. Accepts `P-12345` too |
| | `get_problem_evidence` | Full evidence list, root-cause items first |
| | `get_problem_impact` | Root cause, affected and impacted entities, impact analysis |
| | `list_problem_comments` | `GET /problems/{id}/comments` |
| Events | `list_events`, `get_event` | `GET /events`, `GET /events/{id}` |
| Entities | `search_entities`, `get_entity`, `find_entity_by_name` | `GET /entities`, `GET /entities/{id}` with relationships |
| Metrics | `query_metrics` | `GET /metrics/query` with per-series min/max/avg/last (data points on request) |
| | `list_metrics`, `get_metric_descriptor` | `GET /metrics`, `GET /metrics/{key}` |
| Security | `list_security_problems`, `get_security_problem` | `GET /securityProblems` |
| SLOs | `list_slos`, `get_slo` | `GET /slo` (status, error budget, burn rate) |
| **RCA** | `investigate_problem` | Problem + root-cause entity and its process groups/hosts, change events (deployments, config, restarts) before the start, error logs in the window grouped by signature, key metrics before vs during. Returns `findings`, a Markdown `summary` and `nextSteps`; failed sub-queries go to `errors` instead of failing the whole report |
| **Service** | `debug_service` | Service by id or name: failure rate, requests and response time (baseline vs recent), failure logs of its process groups, recent deployments/config changes, problems in the window, callers and callees |

Opt-in:

| Tool | Enabled by | API |
| --- | --- | --- |
| `add_problem_comment` | `DT_ENABLE_WRITE=true` | `POST /problems/{id}/comments` |
| `close_problem` | `DT_ENABLE_WRITE=true` | `POST /problems/{id}/close` (marked destructive) |
| `execute_dql`, `get_trace_spans` | `DT_PLATFORM_URL` + `DT_PLATFORM_TOKEN` | Grail `query:execute` / `query:poll` (SaaS only) |

Tools that are off are not registered at all, so the agent cannot see or call them.

## Setup

### 1. Create an API token

Dynatrace -> **Access tokens** -> **Generate new token** (on Managed: inside the environment, not the Cluster Management Console). Pick these **API v2** scopes:

| Scope | UI name | Needed for |
| --- | --- | --- |
| `logs.read` | Read logs | `search_logs`, `aggregate_logs`, `get_trace_logs`, RCA error logs |
| `problems.read` | Read problems | problem tools, RCA |
| `events.read` | Read events | events, deployments in RCA / `debug_service` |
| `entities.read` | Read entities | entity tools, relationships in RCA, name lookup |
| `metrics.read` | Read metrics | metric tools, key metrics in RCA |
| `securityProblems.read` | Read security problems | security tools (needs Application Security) |
| `slo.read` | Read SLO | SLO tools |
| `problems.write` | Write problems | only with `DT_ENABLE_WRITE=true` |

`DataExport` (API v1 "Access problem and event feed, metrics, and topology") is **not** needed; this server never calls API v1. Add it only if your admins issue one token for several tools that use v1. Missing scopes only disable the tools that need them: `check_connection` lists them.

### 2. Get the code

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.10+ if needed).

```bash
git clone <this-repo> ~/github/dynatrace-mcp
```

```bash
cd ~/github/dynatrace-mcp && uv sync && uv run dynatrace-mcp --version
```

Behind a corporate proxy or TLS-inspecting firewall, `uv sync` itself may need `HTTPS_PROXY` and `SSL_CERT_FILE` (or `UV_NATIVE_TLS=true`) to reach PyPI or your internal mirror (`UV_INDEX_URL`).

### 3. Put the token in the environment

The server reads the token only from `DT_API_TOKEN`. It never logs it, never returns it and never writes it to disk. Pick one:

- **Shell profile** (Cursor picks up your login shell's environment; restart Cursor fully after changing it):

  ```bash
  export DT_BASE_URL="https://dynatrace.corp.example.com/e/<environment-id>"
  export DT_API_TOKEN="dt0c01.XXXX..."   # better: read it from a secret store, see below
  ```

- **macOS Keychain** (nothing in plain-text files):

  ```bash
  security add-generic-password -a "$USER" -s dynatrace-api-token -w
  ```

  Then use [`examples/mcp.keychain.json`](examples/mcp.keychain.json), which reads the token at launch.

### 4. Register the server in Cursor

Add to `~/.cursor/mcp.json` (all projects) or `<project>/.cursor/mcp.json`, with the absolute path of this repo:

**Dynatrace Managed** ([`examples/mcp.managed.json`](examples/mcp.managed.json)):

```json
{
  "mcpServers": {
    "dynatrace": {
      "command": "uv",
      "args": ["run", "--quiet", "--directory", "/absolute/path/to/dynatrace-mcp", "dynatrace-mcp"],
      "env": {
        "DT_BASE_URL": "https://dynatrace.corp.example.com/e/<environment-id>",
        "DT_API_TOKEN": "${env:DT_API_TOKEN}",
        "DT_CA_BUNDLE": "/absolute/path/to/corp-root-ca.pem"
      }
    }
  }
}
```

**SaaS** ([`examples/mcp.saas.json`](examples/mcp.saas.json)): same, with `"DT_BASE_URL": "https://<env-id>.live.dynatrace.com"` and no CA bundle. A `*.apps.dynatrace.com` URL is rewritten to `*.live.dynatrace.com`, where the classic API lives.

This repo's own [`.cursor/mcp.json`](.cursor/mcp.json) uses `${workspaceFolder}`, so opening the repo in Cursor launches the server directly (handy for development).

If Cursor cannot find `uv`, use its absolute path (`which uv`, e.g. `/opt/homebrew/bin/uv`) as `command`.

### 5. Check it

Cursor Settings -> **MCP**: `dynatrace` shows a green dot and 23 tools. Then ask the agent:

```text
Run check_connection for Dynatrace and tell me which scopes are missing.
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DT_BASE_URL` | (required) | Managed: `https://<host>/e/<env-id>`. SaaS: `https://<env>.live.dynatrace.com`. A trailing `/api/v2` or a pasted UI link is trimmed |
| `DT_API_TOKEN` | (required) | API token, bare value (no `Api-Token ` prefix) |
| `DT_CA_BUNDLE` | | PEM file with your company's root/intermediate CAs (Managed with internal certificates) |
| `DT_INSECURE_TLS` | `false` | Skip TLS verification. Last resort; prefer `DT_CA_BUNDLE` |
| `DT_PROXY` | | Proxy URL for Dynatrace calls, e.g. `http://proxy.corp:3128`. Standard `HTTPS_PROXY` / `NO_PROXY` / `SSL_CERT_FILE` are honored too |
| `DT_TIMEOUT` | `30` | Request timeout in seconds |
| `DT_MAX_RETRIES` | `2` | Retries for GET on 429/502/503/504 (honors `Retry-After`, max 10 s) |
| `DT_ENABLE_WRITE` | `false` | Register `add_problem_comment` and `close_problem` |
| `DT_DISABLE_TOOLS` | | Comma-separated tool names to hide (e.g. to stay under Cursor's tool limit) |
| `DT_REDACT` | `true` | Mask secrets in tool output (passwords, tokens, keys, JWTs, credentials in URLs). The configured tokens are masked even when `false` |
| `DT_MAX_OUTPUT_CHARS` | `60000` | Output budget per tool call; the largest lists are trimmed and `_truncated` says what was dropped |
| `DT_MAX_LOG_LINE_CHARS` | `2000` | Max characters kept per log line |
| `DT_ALLOW_HTTP` | `false` | Allow a plain `http://` base URL (refused by default because the token would travel unencrypted) |
| `DT_PLATFORM_URL`, `DT_PLATFORM_TOKEN` | | Optional Grail DQL on SaaS: `https://<env>.apps.dynatrace.com` and a platform token with `storage:logs:read`, `storage:spans:read`, `storage:buckets:read` (plus other `storage:*:read` scopes you query) |
| `DT_LOG_LEVEL` | `WARNING` | Server log level; logs go to stderr (Cursor: Output -> MCP Logs) |

If `DT_BASE_URL` or `DT_API_TOKEN` is missing, the server still starts and lists its tools, and every call returns the configuration error, so the problem shows up in chat instead of as a silent red dot.

## Use

```text
Investigate Dynatrace problem P-24101 and write an RCA.
```

```text
checkout-service has been throwing 500s since 10:20 UTC. Debug it with Dynatrace.
```

```text
List open Dynatrace problems in the last 6 hours that touch services tagged team:payments.
```

```text
Search Dynatrace logs for status="ERROR" AND content="SQLTimeoutException" in the last 30 minutes, grouped by log.source.
```

### Companion skill and rule

- [`.cursor/skills/dynatrace-rca/SKILL.md`](.cursor/skills/dynatrace-rca/SKILL.md): RCA workflow (frame, `investigate_problem`, verify with drill-down tools, report template with Confirmed / Likely / Unknown, write-back only on confirmation) and a selector/query cheat sheet. Invoke with `/dynatrace-rca` or let the agent pick it up.
- [`.cursor/rules/dynatrace-guardrails.mdc`](.cursor/rules/dynatrace-guardrails.mdc): agent-requested rule (evidence first, narrow queries, never handle the token, confirm writes).

Copy them into the project where you investigate incidents, or install the skill for all projects:

```bash
mkdir -p ~/.cursor/skills && cp -R .cursor/skills/dynatrace-rca ~/.cursor/skills/
```

```bash
mkdir -p /path/to/project/.cursor/rules && cp .cursor/rules/dynatrace-guardrails.mdc /path/to/project/.cursor/rules/
```

## How the RCA tool correlates

`investigate_problem(problem_id, lookback_minutes=60)`:

1. Loads the problem with evidence, impact analysis and comments (`P-12345` is resolved to the internal id).
2. Takes the root-cause entity (or the first affected entity) and its relationships. Services are mapped to the process groups, instances and hosts they run on, because classic logs and deployment events are attached to those, not to the service.
3. In parallel, for `[start - lookback, end + 5 min]`:
   - events on those entities; deployments, config changes, restarts and annotations before the start become `changesBeforeProblem` with minutes-before;
   - error logs (`status="ERROR"` by default) scoped by `dt.entity.process_group` (or instance/host), grouped by a normalized signature (numbers, ids, timestamps removed);
   - key metrics for the entity type (service: failure rate, failed requests, requests, response time avg/p90 in ms; host: CPU, memory, fullest disk; process: CPU, memory) with baseline vs during averages and `changePct`.
4. Builds `findings`, a Markdown `summary` and `nextSteps`. A missing scope or failed sub-query is recorded in `errors` and the rest of the report still comes back.

The findings are hypotheses ranked by evidence; the skill tells the agent to verify them before calling something the root cause.

## Security model

- **Read-only by default.** Write tools are not registered unless `DT_ENABLE_WRITE=true`, carry MCP `destructiveHint`/`readOnlyHint` annotations, and are only `POST`s to problem comments/close. GETs are the only requests retried.
- **Token handling.** Read from the environment only; sent only to `DT_BASE_URL`; redirects are not followed (so the token is never replayed to an SSO page or another host); masked in errors, logs and tool output; `check_connection` shows `"api_token": "set"`, never the value. Plain `http://` is refused.
- **Output hygiene.** Secret-looking values in log lines, event properties and errors are masked; long lines and huge result sets are cut to a budget so a single call cannot flood the agent's context.
- **TLS.** Verification is on by default; `DT_CA_BUNDLE` adds internal CAs; `DT_INSECURE_TLS` logs a warning at startup.
- **Least privilege.** Grant only the read scopes you need. `check_connection` shows what works.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `401` / "token was rejected" | Wrong token or token from another environment. On Managed, the token must be created inside the environment you point `DT_BASE_URL` at |
| `403` "lacks a scope" | Add the scope named in the hint; `check_connection` lists all missing ones |
| `404` on everything (Managed) | `DT_BASE_URL` is missing `/e/<environment-id>` |
| "TLS verification failed" | Export your corporate root CA as PEM and set `DT_CA_BUNDLE` |
| "unexpected redirect" / "not JSON" | A proxy or SSO page is in the way; set `DT_PROXY` or `NO_PROXY`, check the URL |
| Logs empty in RCA | Log Monitoring not enabled for those process groups, or logs carry other entity attributes; run `search_logs` with your own query and pass it as `log_filter` |
| Security tools 404/403 | Application Security not licensed/enabled in that environment |
| Server red in Cursor | Cursor -> Output -> "MCP Logs"; run `uv run --directory <repo> dynatrace-mcp --version` in a terminal |

## Development

```bash
uv sync && uv run pytest -v
```

```text
src/dynatrace_mcp/
  config.py   env -> Settings (URL normalization, TLS, proxy, flags)
  client.py   async httpx client: Api-Token auth, retries, errors with scope hints
  redact.py   secret masking and output budget
  api.py      one function per API v2 operation, compact output
  rca.py      investigate_problem and debug_service
  server.py   FastMCP tool registration (stdio)
tests/        HTTP mocked with httpx.MockTransport; includes a real stdio launch that lists the tools
```

Tests never touch the network; `tests/test_server.py::test_stdio_end_to_end` starts the server the way Cursor does and checks the tool list.

## License

MIT, see [LICENSE](LICENSE).
