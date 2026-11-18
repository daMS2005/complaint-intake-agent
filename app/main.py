"""Northwind customer operations: customer assistant, Case Desk and Analyst, plus the Dialogflow CX webhook."""
import datetime
import pathlib
import re
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

from . import agent, backend

STATIC = pathlib.Path(__file__).parent / "static"
app = FastAPI(title="Northwind customer operations")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

sessions = InMemorySessionService()
customer = Runner(app_name="customer", agent=agent.customer_agent, session_service=sessions, auto_create_session=True)
analyst = Runner(app_name="analyst", agent=agent.analyst_agent, session_service=sessions, auto_create_session=True)
agent.seed_if_empty()

# Card numbers (13-19 digits, optional separators) and SSNs never reach the model or storage.
PII = re.compile(r"\b(?:\d[ -]?){12,18}\d\b|\b\d{3}-\d{2}-\d{4}\b")


def redact(text: str) -> str:
    return PII.sub("[REDACTED]", text)


async def ask(runner: Runner, session_id: str, text: str) -> str:
    message = types.Content(role="user", parts=[types.Part(text=redact(text))])
    reply = ""
    async for event in runner.run_async(user_id=session_id, session_id=session_id, new_message=message):
        if event.is_final_response() and event.content and event.content.parts:
            reply = "".join(p.text or "" for p in event.content.parts if not p.thought)
    return reply


class ChatIn(BaseModel):  # caps keep a public demo cheap to run
    session_id: str = Field(max_length=64)
    message: str = Field(min_length=1, max_length=2000)


@app.post("/chat")
async def chat(body: ChatIn) -> dict:
    return {"reply": await ask(customer, body.session_id, body.message)}


@app.post("/analyst")
async def analyst_chat(body: ChatIn) -> dict:
    return {"reply": await ask(analyst, body.session_id, body.message)}


@app.post("/dialogflow/webhook")
async def dialogflow_webhook(request: Request) -> dict:
    """Dialogflow CX WebhookRequest in, WebhookResponse out; the CX session id keys the ADK session."""
    req = await request.json()
    session_id = req["sessionInfo"]["session"].rsplit("/", 1)[-1]
    reply = await ask(customer, session_id, req.get("text") or req.get("transcript") or "hello")
    return {"fulfillmentResponse": {"messages": [{"text": {"text": [reply]}}]}}


@app.get("/api/cases")
def list_cases() -> list[dict]:
    return backend.list_cases()


class CaseUpdate(BaseModel):
    status: Literal["new", "in_review", "resolved"] | None = None
    note: str | None = Field(default=None, max_length=1000)


@app.patch("/api/cases/{case_id}")
def update_case(case_id: str, body: CaseUpdate) -> dict:
    case = backend.load_case(case_id)
    if not case:
        raise HTTPException(404, "case not found")
    if body.note:
        at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        case["notes"] = case.get("notes", []) + [{"at": at, "text": redact(body.note)}]
    if body.status and body.status != case["status"]:
        case["status"] = body.status
        backend.publish("status_changed", case)
    backend.save_case(case)
    return case


@app.get("/health")
def health() -> dict:
    return {
        "backend": "gcp" if backend.GCP else "local",
        "model": backend.MODEL,
        "embeddings": backend.EMBED_MODEL,
        "cases": f"firestore:{backend.FIRESTORE_DB}" if backend.FIRESTORE_DB else "sqlite",
        "events": "pubsub" if backend.TOPIC else "jsonl",
    }


@app.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC / "home.html")


@app.get("/assistant")
@app.get("/analyst")
def chat_page() -> FileResponse:
    return FileResponse(STATIC / "chat.html")


@app.get("/desk")
def desk_page() -> FileResponse:
    return FileResponse(STATIC / "desk.html")
