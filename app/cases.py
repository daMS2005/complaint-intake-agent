"""Case lifecycle: opening a case with its legal deadline, the timeline, and what each role may see."""
import datetime
import json
import pathlib
import re
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
    if any(re.fullmatch(r"reg(ulation)?e", re.sub(r"[\s.]", "", r.lower())) for r in regulations):
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
        "triage": {"model": backend.MODEL, "urgency": urgency.lower(), "regulations": regulations},
        "timeline": [{"at": iso(created), "kind": "status", "by": "assistant", "text": "Case opened"}],
    }
    backend.put("cases", case["id"], case)
    if notify:
        backend.publish("escalated" if case["urgency"] == "high" else "filed", case)
    return case


CUSTOMER_KINDS = ("status", "reply", "file", "customer")  # "note" and "triage" entries are staff-only


def add_entry(case: dict, kind: str, text: str, by: str) -> None:
    """kind: "reply", "customer", "status" and "file" are shown to the customer; "note" and "triage" are staff-only."""
    case["timeline"].append({"at": iso(now()), "kind": kind, "by": by, "text": text})


def customer_view(case: dict) -> dict:
    fields = ("id", "created", "due", "product", "issue", "summary", "status")
    files = [{k: f[k] for k in ("name", "type", "size", "at")} for f in case.get("files", [])]
    return {k: case[k] for k in fields} | {"files": files,
                                           "timeline": [e for e in case["timeline"] if e["kind"] in CUSTOMER_KINDS]}


def on_desk(case: dict, staff_uid: str) -> bool:
    """Demo staff see the sample cases and their own, never another visitor's."""
    return bool(case.get("seed")) or case["owner"] == staff_uid


def desk_cases(staff_uid: str) -> list[dict]:
    return sorted((c for c in backend.find("cases") if on_desk(c, staff_uid)), key=lambda c: c["created"], reverse=True)


def cases_of(owner: str) -> list[dict]:
    return sorted(backend.find("cases", owner=owner), key=lambda c: c["created"], reverse=True)


def refresh_seeds() -> None:
    """Demo cases from other (fictional) customers so the Case Desk and Insights have data. Rewritten once a day,
    so their deadlines stay current on a long-running demo."""
    seeds = backend.find("cases", seed=True)
    if seeds and min(c.get("seeded", "") for c in seeds) > iso(now() - datetime.timedelta(days=1)):
        return
    for s in json.loads((pathlib.Path(__file__).parent / "data" / "seed_cases.json").read_text()):
        created = now() - datetime.timedelta(days=s["days_ago"], hours=3)
        case = open_case(f"seed-customer-{s['id'][-2:]}", s["product"], s["issue"], s["summary"], s["urgency"],
                         s["regulations"], created, notify=False) | {"id": s["id"], "seed": True, "seeded": iso(now())}
        if s["status"] != "new":
            case["status"] = s["status"]
            case["timeline"].append({"at": iso(created + datetime.timedelta(hours=5)), "kind": "reply", "by": "staff",
                                     "text": "We're looking into this and will update you here."})
        if s["status"] == "resolved":
            case["timeline"].append({"at": iso(created + datetime.timedelta(days=2)), "kind": "reply",
                                     "by": "staff", "text": "We've corrected this on your account."})
        backend.put("cases", case["id"], case)
