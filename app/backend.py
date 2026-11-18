"""Runtime check: route through GCP when the Vertex AI API is available, else a local open-source stack."""
import json
import logging
import os
import pathlib

import google.auth
from google.auth.transport.requests import AuthorizedSession

log = logging.getLogger(__name__)


def vertex_enabled() -> tuple[bool, str | None]:
    """True when credentials resolve and aiplatform.googleapis.com is ENABLED on the project."""
    if os.getenv("FORCE_LOCAL"):
        return False, None
    try:
        creds, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        project = os.getenv("GOOGLE_CLOUD_PROJECT") or project
        url = f"https://serviceusage.googleapis.com/v1/projects/{project}/services/aiplatform.googleapis.com"
        r = AuthorizedSession(creds).get(url, timeout=5)
        return r.ok and r.json().get("state") == "ENABLED", project
    except Exception as e:  # no credentials, no network, no permission: all mean "go local"
        log.warning("Vertex AI check failed, using local stack: %s", e)
        return False, None


GCP, PROJECT = vertex_enabled()
BUCKET = os.getenv("CASES_BUCKET") if GCP else None
TOPIC = os.getenv("ESCALATION_TOPIC") if GCP else None  # projects/<p>/topics/<t>
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "data"))

if GCP:  # ADK reads these to send Gemini calls through Vertex AI
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
    os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    MODEL = os.getenv("VERTEX_MODEL", "gemini-3.5-flash")
else:
    MODEL = os.getenv("LOCAL_MODEL", "qwen3:4b-instruct")

log.info("backend=%s model=%s bucket=%s topic=%s", "gcp" if GCP else "local", MODEL, BUCKET, TOPIC)


def save_case(case: dict) -> None:
    if BUCKET:
        from google.cloud import storage
        storage.Client().bucket(BUCKET).blob(f"cases/{case['id']}.json").upload_from_string(
            json.dumps(case), content_type="application/json")
    else:
        (DATA_DIR / "cases").mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "cases" / f"{case['id']}.json").write_text(json.dumps(case, indent=2))


def load_case(case_id: str) -> dict | None:
    if BUCKET:
        from google.cloud import storage
        blob = storage.Client().bucket(BUCKET).blob(f"cases/{case_id}.json")
        return json.loads(blob.download_as_text()) if blob.exists() else None
    path = DATA_DIR / "cases" / f"{case_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


def escalate(case: dict) -> None:
    if TOPIC:
        from google.cloud import pubsub_v1
        pubsub_v1.PublisherClient().publish(TOPIC, json.dumps(case).encode()).result(timeout=10)
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "escalations.jsonl", "a") as f:
            f.write(json.dumps(case) + "\n")
