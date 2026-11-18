"""Pub/Sub-triggered Cloud Function: turns a high-urgency case into an ALERT log entry.

Cloud Monitoring's log-match alert policy (infra/monitoring.tf) emails the compliance team on each one.
"""
import base64
import json

import functions_framework


@functions_framework.cloud_event
def notify(event):
    case = json.loads(base64.b64decode(event.data["message"]["data"]))
    print(json.dumps({
        "severity": "ALERT",
        "message": f"High-urgency complaint {case['id']}: {case['product']} / {case['issue']}",
        "case": case,
    }))
