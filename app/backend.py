"""Runtime check: route through GCP when the Vertex AI API is available, else a local open-source stack.
Without Vertex AI, a valid OPENAI_API_KEY swaps only the model and embeddings to OpenAI; the rest stays local.

Each function below has a GCP branch and a local branch:
  sign-in     Identity Platform    | developer tokens ("dev:<uid>")
  documents   Firestore            | SQLite
  files       Cloud Storage        | data/files/
  events      Pub/Sub              | data/events.jsonl
  embeddings  Vertex AI            | OpenAI if keyed, else EmbeddingGemma on Ollama
  industry    BigQuery CFPB data   | bundled CFPB sample (app/data/cfpb_sample.csv.gz)
"""
import collections
import csv
import functools
import gzip
import json
import logging
import os
import pathlib
import sqlite3

import google.auth
import httpx
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2 import id_token

log = logging.getLogger(__name__)


CHECK: dict = {}  # what the startup check found, shown on /status


def vertex_enabled() -> tuple[bool, str | None]:
    """True when credentials resolve and aiplatform.googleapis.com is ENABLED on the project."""
    import datetime
    CHECK.update(checked_at=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"), steps=[])
    step = lambda name, ok, detail: CHECK["steps"].append({"name": name, "ok": ok, "detail": detail})
    if os.getenv("FORCE_LOCAL"):
        step("FORCE_LOCAL", False, "Set, so the check is skipped and the local stack is used")
        return False, None
    try:
        creds, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        project = os.getenv("GOOGLE_CLOUD_PROJECT") or project
        step("Google credentials", True, f"Found, project {project}")
        url = f"https://serviceusage.googleapis.com/v1/projects/{project}/services/aiplatform.googleapis.com"
        r = AuthorizedSession(creds).get(url, timeout=5)
        state = r.json().get("state") if r.ok else f"HTTP {r.status_code}"
        step("Vertex AI API (aiplatform.googleapis.com)", state == "ENABLED", state)
        return state == "ENABLED", project
    except Exception as e:  # no credentials, no network, no permission: all mean "go local"
        log.warning("Vertex AI check failed, using local stack: %s", e)
        step("Google credentials" if not CHECK["steps"] else "Vertex AI API (aiplatform.googleapis.com)", False,
             type(e).__name__)
        return False, None


GCP, PROJECT = vertex_enabled()
FIRESTORE_DB = os.getenv("FIRESTORE_DATABASE") if GCP else None
TOPIC = os.getenv("EVENTS_TOPIC") if GCP else None  # projects/<p>/topics/<t>
WEB_API_KEY = os.getenv("WEB_API_KEY") if GCP else None  # browser key for Identity Platform sign-in
EVIDENCE_BUCKET = os.getenv("EVIDENCE_BUCKET") if GCP else None  # customer documents
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "data"))
CFPB_SAMPLE = pathlib.Path(__file__).parent / "data" / "cfpb_sample.csv.gz"

def openai_usable() -> bool:
    """Second tier: an OpenAI key that the API accepts (checked once, with a free model-list call)."""
    key = os.getenv("OPENAI_API_KEY")
    if GCP or not key:
        return False
    try:
        r = httpx.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=5)
        ok, detail = r.status_code == 200, "Accepted" if r.status_code == 200 else f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        ok, detail = False, type(e).__name__
    CHECK["steps"].append({"name": "OpenAI API key", "ok": ok, "detail": detail})
    return ok


LLM = "vertex" if GCP else "openai" if openai_usable() else "ollama"

if GCP:  # ADK reads these to send Gemini calls through Vertex AI
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
    os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    MODEL = os.getenv("VERTEX_MODEL", "gemini-3.5-flash")
    EMBED_MODEL = os.getenv("VERTEX_EMBED_MODEL", "gemini-embedding-001")
elif LLM == "openai":
    MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
    EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
else:
    MODEL = os.getenv("LOCAL_MODEL", "qwen3:4b-instruct")
    EMBED_MODEL = os.getenv("LOCAL_EMBED_MODEL", "embeddinggemma")
OLLAMA = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")

log.info("backend=%s llm=%s model=%s firestore=%s", "gcp" if GCP else "local", LLM, MODEL, FIRESTORE_DB)


def verify_token(token: str) -> str | None:
    """Returns the signed-in user's id, or None. Identity Platform ID tokens on GCP, "dev:<uid>" locally."""
    if WEB_API_KEY:
        try:
            return id_token.verify_firebase_token(token, Request(), audience=PROJECT)["sub"]
        except Exception:
            return None
    return token.removeprefix("dev:") if token.startswith("dev:") and len(token) < 80 else None


@functools.cache
def _db():
    if FIRESTORE_DB:
        from google.cloud import firestore
        return firestore.Client(project=PROJECT, database=FIRESTORE_DB)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DATA_DIR / "northwind.db", check_same_thread=False)


def _table(name: str):
    con = _db()
    con.execute(f"CREATE TABLE IF NOT EXISTS {name} (id TEXT PRIMARY KEY, data TEXT)")
    return con


def put(collection: str, doc_id: str, doc: dict) -> None:
    if FIRESTORE_DB:
        _db().collection(collection).document(doc_id).set(doc)
    else:
        _table(collection).execute(f"INSERT OR REPLACE INTO {collection} VALUES (?, ?)", (doc_id, json.dumps(doc)))
        _db().commit()


def get(collection: str, doc_id: str) -> dict | None:
    if FIRESTORE_DB:
        doc = _db().collection(collection).document(doc_id).get()
        return doc.to_dict() if doc.exists else None
    row = _table(collection).execute(f"SELECT data FROM {collection} WHERE id = ?", (doc_id,)).fetchone()
    return json.loads(row[0]) if row else None


def find(collection: str, **equals) -> list[dict]:
    """Documents whose fields equal the given values (all documents if none given)."""
    if FIRESTORE_DB:
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = _db().collection(collection)
        for field, value in equals.items():
            query = query.where(filter=FieldFilter(field, "==", value))
        return [d.to_dict() for d in query.stream()]
    docs = [json.loads(r[0]) for r in _table(collection).execute(f"SELECT data FROM {collection}")]
    return [d for d in docs if all(d.get(k) == v for k, v in equals.items())]


@functools.cache
def _bucket():
    from google.cloud import storage
    return storage.Client(project=PROJECT).bucket(EVIDENCE_BUCKET)


def put_file(path: str, data: bytes, content_type: str) -> None:
    if EVIDENCE_BUCKET:
        _bucket().blob(path).upload_from_string(data, content_type=content_type)
    else:
        target = DATA_DIR / "files" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def get_file(path: str) -> bytes:
    if EVIDENCE_BUCKET:
        return _bucket().blob(path).download_as_bytes()
    return (DATA_DIR / "files" / path).read_bytes()


@functools.cache
def _publisher():
    from google.cloud import pubsub_v1
    return pubsub_v1.PublisherClient()


def publish(event: str, case: dict) -> None:
    """Case events (filed, escalated, status_changed) for downstream consumers."""
    if TOPIC:
        _publisher().publish(TOPIC, json.dumps(case).encode(), event=event).result(timeout=10)
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "events.jsonl", "a") as f:
            f.write(json.dumps({"event": event, "case": case}) + "\n")


def embed(texts: list[str]) -> list[list[float]]:
    if GCP:
        from google import genai
        client = genai.Client(vertexai=True, project=PROJECT, location=os.getenv("EMBED_LOCATION", "us-central1"))
        return [client.models.embed_content(model=EMBED_MODEL, contents=t).embeddings[0].values for t in texts]
    if LLM == "openai":
        r = httpx.post("https://api.openai.com/v1/embeddings", json={"model": EMBED_MODEL, "input": texts}, timeout=60,
                       headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
        r.raise_for_status()
        return [d["embedding"] for d in r.json()["data"]]
    r = httpx.post(f"{OLLAMA}/api/embed", json={"model": EMBED_MODEL, "input": texts}, timeout=120)
    r.raise_for_status()
    return r.json()["embeddings"]


INDUSTRY_FIELDS = {"issue": "issue", "company": "company_name", "state": "state", "year": "year"}


def industry_breakdown(product_keyword: str, field: str) -> dict:
    """Top 10 counts of public CFPB complaints for products matching the keyword."""
    if field not in INDUSTRY_FIELDS:
        return {"error": f"field must be one of {list(INDUSTRY_FIELDS)}"}
    if GCP:
        from google.cloud import bigquery
        col = "CAST(EXTRACT(YEAR FROM date_received) AS STRING)" if field == "year" else INDUSTRY_FIELDS[field]
        sql = (f"SELECT {col} AS label, COUNT(*) AS n "
               "FROM `bigquery-public-data.cfpb_complaints.complaint_database` "
               "WHERE LOWER(product) LIKE @p GROUP BY label ORDER BY n DESC LIMIT 10")
        config = bigquery.QueryJobConfig(maximum_bytes_billed=5 * 10**9, query_parameters=[
            bigquery.ScalarQueryParameter("p", "STRING", f"%{product_keyword.lower()}%")])
        rows = bigquery.Client(project=PROJECT).query(sql, job_config=config).result()
        counts = [(r.label, r.n) for r in rows]
        source = "BigQuery bigquery-public-data.cfpb_complaints (all complaints through Mar 2023)"
    else:
        with gzip.open(CFPB_SAMPLE, "rt") as f:
            key = INDUSTRY_FIELDS[field]
            counts = collections.Counter(
                r["date_received"][:4] if field == "year" else r[key]
                for r in csv.DictReader(f) if product_keyword.lower() in r["product"].lower()).most_common(10)
        source = "Bundled CFPB sample (1,500 complaints per product, Nov 2025 - Oct 2026)"
    return {"source": source, "top": [{"label": k, "complaints": n} for k, n in counts]}
