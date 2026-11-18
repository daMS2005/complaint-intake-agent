"""Northwind Resolve: complaint handling for a retail bank.

Customers sign in, talk to the assistant and follow their cases. Staff work the case queue against legal
deadlines and ask the analyst about trends. Every /api route checks the caller's sign-in and role.
"""
import collections
import datetime
import pathlib
import re
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
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
cases.seed_if_empty()

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


@app.post("/dialogflow/webhook")
async def dialogflow_webhook(request: Request) -> dict:
    """Dialogflow CX channel. CX callers are not signed in, so each CX session is its own customer id."""
    req = await request.json()
    session_id = req["sessionInfo"]["session"].rsplit("/", 1)[-1]
    reply = await ask(customer_runner, f"cx-{session_id}", session_id, req.get("text") or req.get("transcript") or "hello")
    return {"fulfillmentResponse": {"messages": [{"text": {"text": [reply]}}]}}


# ---- staff ----

@app.get("/api/cases")
def list_cases(user: dict = Depends(staff_user)) -> list[dict]:
    return cases.all_cases()


class CaseUpdate(BaseModel):
    status: Literal[cases.STATUSES] | None = None
    reply: str | None = Field(default=None, max_length=2000)
    note: str | None = Field(default=None, max_length=2000)


@app.patch("/api/cases/{case_id}")
def update_case(case_id: str, body: CaseUpdate, user: dict = Depends(staff_user)) -> dict:
    case = backend.get("cases", case_id)
    if not case:
        raise HTTPException(404, "case not found")
    if body.reply:
        cases.add_entry(case, "reply", redact(body.reply), "staff")
    if body.note:
        cases.add_entry(case, "note", redact(body.note), "staff")
    if body.status and body.status != case["status"]:
        case["status"] = body.status
        cases.add_entry(case, "status", f"Status changed to {body.status.replace('_', ' ')}", "staff")
        backend.publish("status_changed", case)
    backend.put("cases", case_id, case)
    return case


@app.get("/api/stats")
def stats(user: dict = Depends(staff_user)) -> dict:
    t, soon = cases.now(), cases.iso(cases.now() + datetime.timedelta(days=2))
    open_cases = [c for c in cases.all_cases() if c["status"] != "resolved"]
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
        "model": backend.MODEL,
        "embeddings": backend.EMBED_MODEL,
        "auth": "identity_platform" if backend.WEB_API_KEY else "dev",
        "store": f"firestore:{backend.FIRESTORE_DB}" if backend.FIRESTORE_DB else "sqlite",
        "events": "pubsub" if backend.TOPIC else "jsonl",
    }


def _page(name: str):
    return lambda: FileResponse(STATIC / name)


for _path, _name in {"/": "index.html", "/app": "app.html", "/desk": "desk.html", "/insights": "insights.html"}.items():
    app.add_api_route(_path, _page(_name), methods=["GET"], include_in_schema=False)
