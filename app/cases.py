"""Case lifecycle: opening a case with its legal deadline, the timeline, and what each role may see."""
import datetime
import json
import pathlib
import uuid

from . import backend

STATUSES = ("new", "in_review", "resolved")


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def iso(t: datetime.datetime) -> str:
    return t.isoformat(timespec="seconds")


def add_business_days(start: datetime.datetime, days: int) -> datetime.datetime:
    while days:
        start += datetime.timedelta(days=1)
        days -= start.weekday() < 5
    return start


def deadline(created: datetime.datetime, regulations: list[str]) -> datetime.datetime:
    """Reg E errors must be investigated within 10 business days (or provisionally credited); everything
    else gets the 15-day response the CFPB expects for complaints."""
    if any(r.replace(" ", "").lower() == "rege" for r in regulations):
        return add_business_days(created, 10)
    return created + datetime.timedelta(days=15)


def open_case(owner: str, product: str, issue: str, summary: str, urgency: str, regulations: list[str],
              created: datetime.datetime | None = None, notify: bool = True) -> dict:
    created = created or now()
    case = {
        "id": f"C-{uuid.uuid4().hex[:8].upper()}", "owner": owner,
        "created": iso(created), "due": iso(deadline(created, regulations)),
        "product": product, "issue": issue, "summary": summary,
        "urgency": urgency.lower(), "regulations": regulations, "status": "new",
        "timeline": [{"at": iso(created), "kind": "status", "by": "assistant", "text": "Case opened"}],
    }
    backend.put("cases", case["id"], case)
    if notify:
        backend.publish("escalated" if case["urgency"] == "high" else "filed", case)
    return case


def add_entry(case: dict, kind: str, text: str, by: str) -> None:
    """kind: "reply" (customer sees it), "note" (staff only) or "status"."""
    case["timeline"].append({"at": iso(now()), "kind": kind, "by": by, "text": text})


def customer_view(case: dict) -> dict:
    fields = ("id", "created", "due", "product", "issue", "summary", "status")
    return {k: case[k] for k in fields} | {"timeline": [e for e in case["timeline"] if e["kind"] != "note"]}


def all_cases() -> list[dict]:
    return sorted(backend.find("cases"), key=lambda c: c["created"], reverse=True)


def cases_of(owner: str) -> list[dict]:
    return sorted(backend.find("cases", owner=owner), key=lambda c: c["created"], reverse=True)


def seed_if_empty() -> None:
    """Demo cases from other (fictional) customers so the Case Desk and Insights have data on first run."""
    if backend.find("cases"):
        return
    for s in json.loads((pathlib.Path(__file__).parent / "data" / "seed_cases.json").read_text()):
        created = now() - datetime.timedelta(days=s["days_ago"], hours=3)
        case = open_case(f"seed-customer-{s['id'][-2:]}", s["product"], s["issue"], s["summary"], s["urgency"],
                         s["regulations"], created, notify=False)
        if s["status"] != "new":
            case["status"] = s["status"]
            case["timeline"].append({"at": iso(created + datetime.timedelta(hours=5)), "kind": "reply", "by": "staff",
                                     "text": "We're looking into this and will update you here."})
        if s["status"] == "resolved":
            case["timeline"].append({"at": iso(created + datetime.timedelta(days=2)), "kind": "reply",
                                     "by": "staff", "text": "We've corrected this on your account."})
        backend.put("cases", case["id"], case)
