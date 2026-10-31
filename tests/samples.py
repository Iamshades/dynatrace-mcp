"""Sample API v2 payloads, trimmed from the documented response shapes."""

from dynatrace_mcp.timeutil import now_ms

NOW = now_ms()
START = NOW - 30 * 60_000  # problem started 30 minutes ago

SERVICE = "SERVICE-1111111111111111"
PG = "PROCESS_GROUP-2222222222222222"
PGI = "PROCESS_GROUP_INSTANCE-3333333333333333"
HOST = "HOST-4444444444444444"
PID = "-1234567890123456789_1730000000000V2"


def problem(open_: bool = True) -> dict:
    return {
        "problemId": PID,
        "displayId": "P-24101",
        "title": "Failure rate increase",
        "impactLevel": "SERVICES",
        "severityLevel": "ERROR",
        "status": "OPEN" if open_ else "CLOSED",
        "startTime": START,
        "endTime": -1 if open_ else START + 20 * 60_000,
        "rootCauseEntity": {"entityId": {"id": SERVICE, "type": "SERVICE"}, "name": "checkout-service"},
        "affectedEntities": [{"entityId": {"id": SERVICE, "type": "SERVICE"}, "name": "checkout-service"}],
        "impactedEntities": [{"entityId": {"id": "APPLICATION-5555555555555555", "type": "APPLICATION"},
                              "name": "webshop"}],
        "managementZones": [{"id": "1", "name": "prod"}],
        "entityTags": [{"key": "team", "value": "payments", "stringRepresentation": "team:payments"}],
        "evidenceDetails": {"totalCount": 2, "details": [
            {"evidenceType": "TRANSACTIONAL", "displayName": "Failure rate increase",
             "entity": {"entityId": {"id": SERVICE, "type": "SERVICE"}, "name": "checkout-service"},
             "rootCauseRelevant": True, "startTime": START, "endTime": -1,
             "valueBeforeChangePoint": 0.2, "valueAfterChangePoint": 12.5, "unit": "Percent"},
            {"evidenceType": "EVENT", "displayName": "Process restart",
             "entity": {"entityId": {"id": PGI, "type": "PROCESS_GROUP_INSTANCE"}, "name": "checkout"},
             "rootCauseRelevant": False, "startTime": START - 60_000, "eventType": "PROCESS_RESTART",
             "data": {"properties": [{"key": "reason", "value": "OOM password=hunter2"}]}},
        ]},
        "impactAnalysis": {"impacts": [
            {"impactType": "APPLICATION", "estimatedAffectedUsers": 120,
             "impactedEntity": {"entityId": {"id": "APPLICATION-5555555555555555", "type": "APPLICATION"},
                                "name": "webshop"}}]},
        "recentComments": {"totalCount": 1, "comments": [
            {"id": "c1", "createdAtTimestamp": START + 60_000, "authorName": "oncall", "content": "looking",
             "context": "slack"}]},
    }


def service_entity() -> dict:
    return {
        "entityId": SERVICE, "type": "SERVICE", "displayName": "checkout-service",
        "firstSeenTms": NOW - 86_400_000, "lastSeenTms": NOW,
        "properties": {"serviceType": "WEB_REQUEST_SERVICE", "serviceTechnologyTypes": ["Java"]},
        "tags": [{"key": "team", "stringRepresentation": "team:payments"}],
        "fromRelationships": {"runsOn": [{"id": PG, "type": "PROCESS_GROUP"}],
                              "runsOnProcessGroupInstance": [{"id": PGI, "type": "PROCESS_GROUP_INSTANCE"}],
                              "calls": [{"id": "SERVICE-9999999999999999", "type": "SERVICE"}]},
        "toRelationships": {"calls": [{"id": "SERVICE-8888888888888888", "type": "SERVICE"}]},
    }


def events() -> dict:
    return {"totalCount": 2, "events": [
        {"eventId": "e1", "eventType": "CUSTOM_DEPLOYMENT", "title": "Deploy checkout 2.4.1",
         "startTime": START - 10 * 60_000, "endTime": START - 10 * 60_000, "status": "CLOSED",
         "entityId": {"entityId": {"id": PG, "type": "PROCESS_GROUP"}, "name": "checkout"},
         "properties": [{"key": "dt.event.deployment.version", "value": "2.4.1"}]},
        {"eventId": "e2", "eventType": "ERROR_EVENT", "title": "Failure rate increase",
         "startTime": START, "endTime": -1, "status": "OPEN",
         "entityId": {"entityId": {"id": SERVICE, "type": "SERVICE"}, "name": "checkout-service"}},
    ]}


def logs() -> dict:
    lines = [
        "java.sql.SQLTimeoutException: query timed out after 30000 ms on pool db-7",
        "java.sql.SQLTimeoutException: query timed out after 31000 ms on pool db-2",
        "java.sql.SQLTimeoutException: query timed out after 30500 ms on pool db-1",
        "NullPointerException at CartController.java:88 token=abcdef123",
    ]
    return {"sliceSize": 4, "results": [
        {"timestamp": START + i * 60_000, "status": "ERROR", "content": line, "event.type": "LOG",
         "additionalColumns": {"dt.entity.process_group": [PG], "log.source": ["/var/log/app.log"], "empty": []}}
        for i, line in enumerate(lines)]}


def metric_series(metric_id: str, before: float, during: float, entity: str = SERVICE) -> dict:
    step = 60_000
    ts = list(range(START - 30 * step, START + 30 * step, step))
    values = [before if t < START else during for t in ts]
    values[3] = None
    return {"totalCount": 1, "resolution": "1m", "result": [
        {"metricId": metric_id, "data": [{"dimensions": [entity], "dimensionMap": {"dt.entity.service": entity},
                                          "timestamps": ts, "values": values}]}]}
