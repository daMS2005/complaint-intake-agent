"""ADK complaint-intake agent. The same agent runs on Gemini (Vertex AI) or Gemma (Ollama)."""
import datetime
import uuid

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from . import backend

PRODUCTS = [
    "Checking or savings account", "Credit card", "Credit reporting", "Debt collection",
    "Money transfer or virtual currency", "Mortgage", "Personal or student loan", "Vehicle loan or lease",
]


def file_complaint(product: str, issue: str, summary: str, urgency: str, regulations: list[str]) -> dict:
    """Files the customer's complaint as a case and escalates it to the compliance team if urgent.

    Args:
        product: One of the bank's product categories.
        issue: Short label for the problem, e.g. "Unauthorized transaction".
        summary: Neutral two to three sentence summary of what happened, with dates and amounts.
        urgency: "high" if money is lost or at risk right now (fraud, identity theft, frozen funds,
            imminent foreclosure or repossession), "medium" if there is a financial impact, else "low".
        regulations: US consumer-finance rules that may apply, e.g. "Reg E", "FCRA", "FDCPA", "TILA", "RESPA".
    """
    case = {
        "id": f"C-{uuid.uuid4().hex[:8].upper()}",
        "created": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "product": product, "issue": issue, "summary": summary,
        "urgency": urgency.lower(), "regulations": regulations,
    }
    backend.save_case(case)
    escalated = case["urgency"] == "high"
    if escalated:
        backend.escalate(case)
    return {"case_id": case["id"], "escalated": escalated}


def get_case(case_id: str) -> dict:
    """Looks up a previously filed case by its id, e.g. "C-1A2B3C4D"."""
    return backend.load_case(case_id.strip().upper()) or {"error": f"No case {case_id}"}


INSTRUCTION = f"""You are the complaint intake assistant for Northwind Bank, a fictional US bank.
Help customers report a problem and file it as a case.
- Find out what happened, which product (one of: {", ".join(PRODUCTS)}), when, and any amounts.
- Ask at most one round of short follow-up questions. On the customer's next message, call file_complaint
  with what you have; never ask for the same detail twice.
- Never ask for or repeat full account numbers, card numbers, SSNs or passwords.
- After filing, give the case id. If it was escalated, say a specialist will contact them within one
  business day; otherwise within 15 days.
- If the customer gives a case id, use get_case to report its status.
Keep every reply under 80 words, in plain text without markdown."""

root_agent = Agent(
    name="complaint_intake",
    model=backend.MODEL if backend.GCP else LiteLlm(model=f"ollama_chat/{backend.MODEL}"),
    instruction=INSTRUCTION,
    tools=[file_complaint, get_case],
)
