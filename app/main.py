"""HTTP surface: web chat, Dialogflow CX webhook, and health check, all backed by one ADK runner."""
import pathlib
import re

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

from . import backend
from .agent import root_agent

app = FastAPI(title="Complaint intake agent")
runner = Runner(app_name="complaint_intake", agent=root_agent,
                session_service=InMemorySessionService(), auto_create_session=True)

# Card numbers (13-19 digits, optional separators) and SSNs never reach the model or storage.
PII = re.compile(r"\b(?:\d[ -]?){12,18}\d\b|\b\d{3}-\d{2}-\d{4}\b")


def redact(text: str) -> str:
    return PII.sub("[REDACTED]", text)


async def ask(session_id: str, text: str) -> str:
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
    return {"reply": await ask(body.session_id, body.message)}


@app.post("/dialogflow/webhook")
async def dialogflow_webhook(request: Request) -> dict:
    """Dialogflow CX WebhookRequest in, WebhookResponse out; the CX session id keys the ADK session."""
    req = await request.json()
    session_id = req["sessionInfo"]["session"].rsplit("/", 1)[-1]
    reply = await ask(session_id, req.get("text") or req.get("transcript") or "hello")
    return {"fulfillmentResponse": {"messages": [{"text": {"text": [reply]}}]}}


@app.get("/health")
def health() -> dict:
    return {
        "backend": "gcp" if backend.GCP else "local",
        "model": backend.MODEL,
        "storage": f"gs://{backend.BUCKET}" if backend.BUCKET else str(backend.DATA_DIR / "cases"),
        "escalation": backend.TOPIC or str(backend.DATA_DIR / "escalations.jsonl"),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(pathlib.Path(__file__).parent / "static" / "index.html")
