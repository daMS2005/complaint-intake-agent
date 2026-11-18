"""ADK agents. The same agents run on Gemini (Vertex AI) or a local Ollama model.

customer_agent  for signed-in customers; delegates to intake, policy and my-cases sub-agents
analyst_agent   for staff; answers questions over all cases and CFPB industry data

Customer tools read the signed-in user from tool_context.user_id, so an agent can only file and read
cases for the person it is talking to.
"""
import collections
import datetime
import math
import pathlib

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools import ToolContext
from google.genai import types

from . import backend, cases

PRODUCTS = [
    "Checking or savings account", "Credit card", "Credit reporting", "Debt collection",
    "Money transfer or virtual currency", "Mortgage", "Personal or student loan", "Vehicle loan or lease",
]
HERE = pathlib.Path(__file__).parent


def _model():
    if backend.GCP:  # retry Vertex AI's 429s (shared capacity) with backoff instead of failing the turn
        return Gemini(model=backend.MODEL, retry_options=types.HttpRetryOptions(attempts=5, initial_delay=1, max_delay=16))
    return LiteLlm(model=f"ollama_chat/{backend.MODEL}")


# ---- customer tools ----

def file_complaint(product: str, issue: str, summary: str, urgency: str, regulations: list[str],
                   tool_context: ToolContext) -> dict:
    """Files the customer's complaint as a case and escalates it to the compliance team if urgent.

    Args:
        product: One of the bank's product categories.
        issue: Short label for the problem, e.g. "Unauthorized transaction".
        summary: Neutral two to three sentence summary of what happened, with dates and amounts.
        urgency: "high" if money is lost or at risk right now (fraud, identity theft, frozen funds,
            imminent foreclosure or repossession), "medium" if there is a financial impact, else "low".
        regulations: US consumer-finance rules that may apply, e.g. "Reg E", "FCRA", "FDCPA", "TILA", "RESPA".
    """
    case = cases.open_case(tool_context.user_id, product, issue, summary, urgency, regulations)
    return {"case_id": case["id"], "escalated": case["urgency"] == "high", "response_due": case["due"][:10]}


def my_cases(tool_context: ToolContext) -> dict:
    """Returns the signed-in customer's cases with status, response deadline and replies from bank staff."""
    return {"cases": [cases.customer_view(c) for c in cases.cases_of(tool_context.user_id)]}


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
    """Counts Northwind cases opened in the last `days` days, grouped by a field.

    Args:
        field: One of "product", "issue", "urgency", "status", "regulations".
        days: Look-back window in days.
    """
    since = cases.iso(cases.now() - datetime.timedelta(days=days))
    recent = [c for c in cases.all_cases() if c["created"] >= since]
    values = [v for c in recent for v in (c.get(field) if isinstance(c.get(field), list) else [c.get(field)])]
    return {"cases": len(recent), "days": days, "counts": dict(collections.Counter(values).most_common())}


def find_cases(urgency: str = "", status: str = "", product: str = "", overdue_only: bool = False) -> dict:
    """Lists up to 15 Northwind cases, newest first. Filters: urgency ("high", "medium", "low"), status ("new",
    "in_review", "resolved"), a product keyword, and overdue_only (open cases past their response deadline).
    Empty filters match everything."""
    t = cases.iso(cases.now())
    hits = [c for c in cases.all_cases()
            if (not urgency or c["urgency"] == urgency) and (not status or c["status"] == status)
            and product.lower() in c["product"].lower()
            and (not overdue_only or (c["status"] != "resolved" and c["due"] < t))]
    keys = ("id", "created", "due", "product", "issue", "urgency", "status", "summary")
    return {"now": t, "total": len(hits), "cases": [{k: c[k] for k in keys} for c in hits[:15]]}


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
- After filing, give the case id and say they can follow it under My cases. If escalated, say a specialist
  will contact them within one business day; otherwise give the response date.""",
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
    description="Tells the customer where their existing cases stand.",
    instruction="""Use my_cases and summarize the status, the response date and the latest reply from the bank for
the case the customer asks about (or all open ones).""",
    tools=[my_cases],
)

COMMON = """
Today's date is {today}.
Never ask for or repeat full account numbers, card numbers, SSNs or passwords.
Keep every reply under 80 words, in plain text without markdown.
Write like a calm, experienced bank specialist: plain words, say what happens next, no filler or praise,
and at most one short apology per conversation."""

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
- the status of their existing cases: status_agent
Transfer right away without replying yourself. Only if the customer has not said what they need yet,
greet them in one sentence and ask.""" + COMMON,
    sub_agents=[intake_agent, policy_agent, status_agent],
)

analyst_agent = Agent(
    name="ops_analyst",
    model=_model(),
    instruction="""You are an internal analyst for Northwind Bank's compliance and operations teams. Today's date is {today}.
Answer questions about Northwind's complaint cases with case_breakdown and find_cases (which knows each case's
response deadline), and compare them with
the wider industry using industry_breakdown (public CFPB complaint data). Always use the tools rather than
guessing, give concrete numbers, name the data source and time window, and keep answers short. Point out
anything that looks like a compliance risk (for example many high-urgency Reg E cases still marked new).
Use plain text without markdown. Lead with the answer, then the supporting numbers; no filler.""",
    tools=[case_breakdown, find_cases, industry_breakdown],
)
