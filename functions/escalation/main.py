"""Pub/Sub-triggered Cloud Function for case events (filed, escalated, status_changed).

Escalations become ALERT log entries, which the log-match alert policy in infra/monitoring.tf emails to the
compliance team. Everything else is logged at INFO for the audit trail.
"""
import base64
import json

import functions_framework


@functions_framework.cloud_event
def notify(event):
    message = event.data["message"]
    case = json.loads(base64.b64decode(message["data"]))
    kind = message.get("attributes", {}).get("event", "filed")
    print(json.dumps({
        "severity": "ALERT" if kind == "escalated" else "INFO",
        "message": f"Case {case['id']} {kind}: {case['product']} / {case['issue']} ({case['status']})",
        "event": kind,
        "case": case,
    }))
