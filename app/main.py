"""Northwind Resolve: complaint handling for a retail bank.

Customers sign in, talk to the assistant and follow their cases. Staff work the case queue against legal
deadlines and ask the analyst about trends. Every /api route except /api/config checks sign-in and role.
"""
import collections
import datetime
import pathlib
import re
import uuid
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

from . import agent, backend, cases

STATIC = pathlib.Path(__file__).parent / "static"
app = FastAPI(title="Northwind Resolve")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

sessions = InMemorySessionService()
customer_runner = Runner(app_name="customer", agent=agent.customer_agent, session_service=sessions,
                         auto_create_session=True)
analyst_runner = Runner(app_name="analyst", agent=agent.analyst_agent, session_service=sessions,
                        auto_create_session=True)
cases.refresh_seeds()

# Card numbers (13-19 digits, optional separators) and SSNs never reach a model, storage or a staff note.
PII = re.compile(r"\b(?:\d[ -]?){12,18}\d\b|\b\d{3}-\d{2}-\d{4}\b")


def redact(text: str) -> str:
    return PII.sub("[REDACTED]", text)


async def ask(runner: Runner, user_id: str, session_id: str, text: str) -> str:
    message = types.Content(role="user", parts=[types.Part(text=redact(text))])
    reply = ""
    today = {"today": cases.now().date().isoformat()}  # agents' instructions reference {today}
    async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=message, state_delta=today):
        if event.is_final_response() and event.content and event.content.parts:
            reply = "".join(p.text or "" for p in event.content.parts if not p.thought)
    return reply


# ---- sign-in and roles ----

def current_user(authorization: str = Header("")) -> dict:
    uid = backend.verify_token(authorization.removeprefix("Bearer "))
    if not uid:
        raise HTTPException(401, "sign in required")
    user = backend.get("users", uid)
    if not user:  # first visit: everyone starts as a customer
        user = {"uid": uid, "role": "customer", "created": cases.iso(cases.now())}
        backend.put("users", uid, user)
    return user


def staff_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "staff":
        raise HTTPException(403, "staff only")
    return user


@app.get("/api/config")
def config() -> dict:
    """What the browser needs to sign in: Identity Platform on GCP, developer sign-in locally."""
    if backend.WEB_API_KEY:
        return {"auth": "identity_platform", "apiKey": backend.WEB_API_KEY, "projectId": backend.PROJECT}
    return {"auth": "dev"}


@app.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict:
    return user


class RoleIn(BaseModel):
    role: Literal["customer", "staff"]


@app.post("/api/me/role")
def switch_role(body: RoleIn, user: dict = Depends(current_user)) -> dict:
    """Demo only: lets a visitor try both sides. In production an admin grants the staff role."""
    user["role"] = body.role
    backend.put("users", user["uid"], user)
    return user


# ---- customers ----

class ChatIn(BaseModel):  # caps keep a public demo cheap to run
    session_id: str = Field(max_length=64)
    message: str = Field(min_length=1, max_length=2000)


@app.post("/api/chat")
async def chat(body: ChatIn, user: dict = Depends(current_user)) -> dict:
    return {"reply": await ask(customer_runner, user["uid"], body.session_id, body.message)}


@app.get("/api/my/cases")
def my_cases(user: dict = Depends(current_user)) -> list[dict]:
    return [cases.customer_view(c) for c in cases.cases_of(user["uid"])]


DOCUMENT_TYPES = {"application/pdf", "image/png", "image/jpeg"}


@app.post("/api/my/cases/{case_id}/files")
async def add_document(case_id: str, request: Request, name: str = Query(max_length=120),
                       user: dict = Depends(current_user)) -> dict:
    """A customer attaches evidence (a statement, a letter) to their own case. Raw body, so no multipart parser."""
    case = backend.get("cases", case_id)
    if not case or case["owner"] != user["uid"]:
        raise HTTPException(404, "case not found")
    if case["status"] == "resolved":
        raise HTTPException(409, "This case is closed.")
    content_type = request.headers.get("content-type", "")
    if content_type not in DOCUMENT_TYPES:
        raise HTTPException(415, "Upload a PDF, PNG or JPEG.")
    if len(case.get("files", [])) >= 5:
        raise HTTPException(409, "A case can have up to 5 documents.")
    data = await request.body()
    if not data or len(data) > 5_000_000:
        raise HTTPException(413, "Documents can be up to 5 MB.")
    safe = re.sub(r"[^\w.\- ]", "_", name, flags=re.ASCII).strip()[:80] or "document"
    path = f"cases/{case_id}/{uuid.uuid4().hex[:8]}-{safe}"
    backend.put_file(path, data, content_type)
    case.setdefault("files", []).append({"name": safe, "path": path, "type": content_type, "size": len(data),
                                         "at": cases.iso(cases.now())})
    cases.add_entry(case, "file", f"Customer added {safe}", "customer")
    backend.put("cases", case_id, case)
    return cases.customer_view(case)


@app.get("/api/cases/{case_id}/files/{index}")
def get_document(case_id: str, index: int, user: dict = Depends(current_user)) -> Response:
    case = backend.get("cases", case_id)
    if not case or not (cases.on_desk(case, user["uid"]) if user["role"] == "staff" else case["owner"] == user["uid"]):
        raise HTTPException(404, "case not found")
    files = case.get("files", [])
    if not 0 <= index < len(files):
        raise HTTPException(404, "document not found")
    f = files[index]
    return Response(backend.get_file(f["path"]), media_type=f["type"],
                    headers={"Content-Disposition": f'attachment; filename="{f["name"]}"'})


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@app.post("/api/my/cases/{case_id}/messages")
def write_to_bank(case_id: str, body: MessageIn, user: dict = Depends(current_user)) -> dict:
    """The customer answers the specialist on the case itself; staff see it flagged in the Case Desk."""
    case = backend.get("cases", case_id)
    if not case or case["owner"] != user["uid"]:
        raise HTTPException(404, "case not found")
    if case["status"] == "resolved":
        raise HTTPException(409, "This case is closed. Tell the assistant if the problem is back, and it will open a new case.")
    cases.add_entry(case, "customer", redact(body.text), "customer")
    backend.put("cases", case_id, case)
    backend.publish("customer_message", case)
    return cases.customer_view(case)


@app.post("/dialogflow/webhook")
async def dialogflow_webhook(request: Request, authorization: str = Header("")) -> dict:
    """Dialogflow CX channel. CX callers are not signed in, so each CX session is its own customer id."""
    if not backend.verify_dialogflow(authorization.removeprefix("Bearer ")):
        raise HTTPException(401, "Dialogflow only")
    req = await request.json()
    session_id = str(req.get("sessionInfo", {}).get("session", "")).rsplit("/", 1)[-1][:64]
    if not session_id:
        raise HTTPException(400, "sessionInfo.session missing")
    text = str(req.get("text") or req.get("transcript") or "hello")[:2000]
    reply = await ask(customer_runner, f"cx-{session_id}", session_id, text)
    return {"fulfillmentResponse": {"messages": [{"text": {"text": [reply]}}]}}


# ---- staff ----

@app.get("/api/cases")
def list_cases(user: dict = Depends(staff_user)) -> list[dict]:
    return cases.desk_cases(user["uid"])


class CaseUpdate(BaseModel):
    status: Literal[cases.STATUSES] | None = None
    reply: str | None = Field(default=None, max_length=2000)
    note: str | None = Field(default=None, max_length=2000)
    urgency: Literal["high", "medium", "low"] | None = None  # staff override of the AI triage
    assign: Literal["me", "nobody"] | None = None
    regulations: list[str] | None = Field(default=None, max_length=6)


@app.patch("/api/cases/{case_id}")
def update_case(case_id: str, body: CaseUpdate, user: dict = Depends(staff_user)) -> dict:
    case = backend.get("cases", case_id)
    if not case or not cases.on_desk(case, user["uid"]):
        raise HTTPException(404, "case not found")
    if body.assign:
        case["assignee"] = user["uid"] if body.assign == "me" else None
    if body.reply:
        cases.add_entry(case, "reply", redact(body.reply), "staff")
    if body.note:
        cases.add_entry(case, "note", redact(body.note), "staff")
    suggested = case.get("triage", {})
    if body.urgency and body.urgency != case["urgency"]:
        cases.add_entry(case, "triage", f"Urgency changed from {case['urgency']} to {body.urgency} "
                        f"(AI suggested {suggested.get('urgency', 'n/a')})", "staff")
        case["urgency"] = body.urgency
    if body.regulations is not None and body.regulations != case["regulations"]:
        case["regulations"] = [r.strip()[:20] for r in body.regulations if r.strip()]
        case["due"] = cases.iso(cases.deadline(datetime.datetime.fromisoformat(case["created"]), case["regulations"]))
        cases.add_entry(case, "triage", f"Rules changed to {', '.join(case['regulations']) or 'none'}; deadline now "
                        f"{case['due'][:10]} (AI suggested {', '.join(suggested.get('regulations', [])) or 'none'})", "staff")
    if body.status and body.status != case["status"]:
        case["status"] = body.status
        cases.add_entry(case, "status", f"Status changed to {body.status.replace('_', ' ')}", "staff")
        backend.publish("status_changed", case)
    backend.put("cases", case_id, case)
    return case


@app.get("/api/stats")
def stats(user: dict = Depends(staff_user)) -> dict:
    t, soon = cases.now(), cases.iso(cases.now() + datetime.timedelta(days=2))
    open_cases = [c for c in cases.desk_cases(user["uid"]) if c["status"] != "resolved"]
    return {
        "open": len(open_cases),
        "overdue": sum(c["due"] < cases.iso(t) for c in open_cases),
        "due_in_2_days": sum(cases.iso(t) <= c["due"] < soon for c in open_cases),
        "high_urgency_open": sum(c["urgency"] == "high" for c in open_cases),
        "open_by_product": dict(collections.Counter(c["product"] for c in open_cases).most_common()),
    }


@app.post("/api/analyst")
async def analyst_chat(body: ChatIn, user: dict = Depends(staff_user)) -> dict:
    return {"reply": await ask(analyst_runner, user["uid"], body.session_id, body.message)}


# ---- pages ----

@app.get("/health")
def health() -> dict:
    return {
        "backend": "gcp" if backend.GCP else "local",
        "llm": backend.LLM,
        "model": backend.MODEL,
        "embeddings": backend.EMBED_MODEL,
        "auth": "identity_platform" if backend.WEB_API_KEY else "dev",
        "store": f"firestore:{backend.FIRESTORE_DB}" if backend.FIRESTORE_DB else "sqlite",
        "events": "pubsub" if backend.TOPIC else "jsonl",
        "check": backend.CHECK,
    }


def _page(name: str):
    return lambda: FileResponse(STATIC / name)


for _path, _name in {"/": "index.html", "/app": "app.html", "/cases": "cases.html", "/desk": "desk.html",
                     "/insights": "insights.html", "/status": "status.html"}.items():
    app.add_api_route(_path, _page(_name), methods=["GET"], include_in_schema=False)
