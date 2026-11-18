"""ADK agents. The same agents run on Gemini (Vertex AI) or a local Ollama model.

customer_agent  coordinator for customers; delegates to intake, policy and status sub-agents
analyst_agent   internal agent for compliance/ops staff over the case store and CFPB data
"""
import collections
import datetime
import json
import math
import pathlib
import uuid

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from . import backend

PRODUCTS = [
    "Checking or savings account", "Credit card", "Credit reporting", "Debt collection",
    "Money transfer or virtual currency", "Mortgage", "Personal or student loan", "Vehicle loan or lease",
]
HERE = pathlib.Path(__file__).parent


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _model():
    return backend.MODEL if backend.GCP else LiteLlm(model=f"ollama_chat/{backend.MODEL}")


def seed_if_empty() -> None:
    """Loads demo cases so the Case Desk and Analyst have data on first run."""
    if backend.list_cases(limit=1):
        return
    for case in json.loads((HERE / "data" / "seed_cases.json").read_text()):
        created = _now() - datetime.timedelta(days=case.pop("days_ago"), hours=len(case["summary"]) % 9)
        backend.save_case({**case, "created": created.isoformat(timespec="seconds")})


# ---- customer tools ----

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
        "created": _now().isoformat(timespec="seconds"),
        "product": product, "issue": issue, "summary": summary,
        "urgency": urgency.lower(), "regulations": regulations, "status": "new", "notes": [],
    }
    backend.save_case(case)
    escalated = case["urgency"] == "high"
    backend.publish("escalated" if escalated else "filed", case)
    return {"case_id": case["id"], "escalated": escalated}


def get_case(case_id: str) -> dict:
    """Looks up a case by its id, e.g. "C-1A2B3C4D", and returns its status and staff notes."""
    return backend.load_case(case_id.strip().upper()) or {"error": f"No case {case_id}"}


_policy_index = None


def search_policies(question: str) -> dict:
    """Searches Northwind Bank's policy documents (fees, disputes, mortgage, credit reporting, collections,
    complaint handling) and returns the three most relevant sections."""
    global _policy_index
    if _policy_index is None:  # embed every "## " section once, on first use
        chunks = []
        for path in sorted((HERE / "policies").glob("*.md")):
            _, *sections = path.read_text().split("\n## ")
            chunks += [(path.stem, s.split("\n", 1)[0], s.split("\n", 1)[1].strip()) for s in sections]
        _policy_index = list(zip(chunks, backend.embed([f"{c[1]}\n{c[2]}" for c in chunks])))
    q = backend.embed([question])[0]

    def cosine(v):
        return sum(a * b for a, b in zip(q, v)) / (math.hypot(*q) * math.hypot(*v))

    top = sorted(_policy_index, key=lambda cv: -cosine(cv[1]))[:3]
    return {"results": [{"policy": c[0], "section": c[1], "text": c[2]} for c, _ in top]}


# ---- analyst tools ----

def case_breakdown(field: str, days: int = 30) -> dict:
    """Counts Northwind cases filed in the last `days` days, grouped by a field.

    Args:
        field: One of "product", "issue", "urgency", "status", "regulations".
        days: Look-back window in days.
    """
    since = (_now() - datetime.timedelta(days=days)).isoformat()
    cases = [c for c in backend.list_cases() if c["created"] >= since]
    values = [v for c in cases for v in (c.get(field) if isinstance(c.get(field), list) else [c.get(field)])]
    return {"cases": len(cases), "days": days, "counts": dict(collections.Counter(values).most_common())}


def find_cases(urgency: str = "", status: str = "", product: str = "") -> dict:
    """Lists up to 15 Northwind cases, newest first, filtered by urgency ("high", "medium", "low"),
    status ("new", "in_review", "resolved") and/or a product keyword. Empty filters match everything."""
    hits = [c for c in backend.list_cases()
            if (not urgency or c["urgency"] == urgency) and (not status or c["status"] == status)
            and product.lower() in c["product"].lower()]
    return {"total": len(hits), "cases": [
        {k: c[k] for k in ("id", "created", "product", "issue", "urgency", "status", "summary")} for c in hits[:15]]}


def industry_breakdown(product_keyword: str, field: str) -> dict:
    """Top 10 counts of US consumer complaints across all companies (CFPB public data) for products whose
    name contains `product_keyword` (e.g. "credit card", "mortgage", "checking"), grouped by `field`:
    "issue", "company", "state" or "year". Use it to compare Northwind's cases with the industry."""
    return backend.industry_breakdown(product_keyword, field)


# ---- agents ----

intake_agent = Agent(
    name="intake_agent",
    model=_model(),
    description="Takes and files new complaints about a Northwind product or service.",
    instruction=f"""You file complaints for Northwind Bank, a fictional US bank.
- Find out what happened, which product (one of: {", ".join(PRODUCTS)}), when, and any amounts.
- Ask at most one round of short follow-up questions. On the customer's next message, call file_complaint
  with what you have; never ask for the same detail twice.
- File each problem once. If you already filed it in this conversation, refer to that case id instead.
- After filing, give the case id. If escalated, say a specialist will contact them within one business day;
  otherwise within 15 days.""",
    tools=[file_complaint],
)

policy_agent = Agent(
    name="policy_agent",
    model=_model(),
    description="Answers questions about Northwind fees, card and billing disputes, mortgages, credit "
                "reporting, debt collection and how complaints are handled.",
    instruction="""Answer the customer's question using search_policies. Base the answer only on the policy text
returned, mention which policy it comes from, and say so if the policies do not cover it.
If the customer wants to report a problem, transfer to intake_agent.""",
    tools=[search_policies],
)

status_agent = Agent(
    name="status_agent",
    model=_model(),
    description="Reports the status of an existing case when the customer gives a case id.",
    instruction="""Look up the case with get_case and summarize its status and any staff notes for the customer.
If they have no case id, ask for it (format C-XXXXXXXX).""",
    tools=[get_case],
)

COMMON = """
Never ask for or repeat full account numbers, card numbers, SSNs or passwords.
Keep every reply under 80 words, in plain text without markdown."""

for _a in (intake_agent, policy_agent, status_agent):
    _a.instruction += COMMON

customer_agent = Agent(
    name="northwind_assistant",
    model=_model(),
    instruction="""You are Northwind Bank's virtual assistant (Northwind is fictional).
Route every request to the right specialist:
- something that happened to the customer (a charge they did not make, a wrong fee, collection calls,
  a notice, a frozen account): intake_agent, even if they also ask a question
- a general question about fees, rights, timelines or policies, with nothing to report: policy_agent
- the status of an existing case: status_agent
Transfer right away without replying yourself. Only if the customer has not said what they need yet,
greet them in one sentence and ask.""" + COMMON,
    sub_agents=[intake_agent, policy_agent, status_agent],
)

analyst_agent = Agent(
    name="ops_analyst",
    model=_model(),
    instruction="""You are an internal analyst for Northwind Bank's compliance and operations teams.
Answer questions about Northwind's complaint cases with case_breakdown and find_cases, and compare them with
the wider industry using industry_breakdown (public CFPB complaint data). Always use the tools rather than
guessing, give concrete numbers, name the data source and time window, and keep answers short. Point out
anything that looks like a compliance risk (for example many high-urgency Reg E cases still marked new).
Use plain text without markdown.""",
    tools=[case_breakdown, find_cases, industry_breakdown],
)
