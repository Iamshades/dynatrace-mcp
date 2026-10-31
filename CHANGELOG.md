# Changelog

## 0.1.0 - 2026-11-01

- First release: stdio MCP server for Dynatrace Managed and SaaS on the classic API v2 with API-token auth.
- Tools for logs (search, aggregate, trace logs), problems (list, details, evidence, impact, comments), events, entities, metrics, security problems and SLOs.
- `investigate_problem` (RCA) and `debug_service` composite tools.
- Opt-in write tools (`DT_ENABLE_WRITE`) and optional Grail DQL (`DT_PLATFORM_URL` + `DT_PLATFORM_TOKEN`).
- Custom CA bundle, insecure-TLS flag, proxy, retries, secret redaction, output budget.
- Cursor `mcp.json` examples, `dynatrace-rca` skill and guardrails rule.
