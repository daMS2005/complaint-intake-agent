import datetime
import json
import os
import tempfile
from types import SimpleNamespace

os.environ["FORCE_LOCAL"] = "1"
os.environ.pop("OPENAI_API_KEY", None)  # tests never call a paid API
os.environ["DATA_DIR"] = tempfile.mkdtemp()

from fastapi.testclient import TestClient  # noqa: E402

from app import agent, backend, cases, main  # noqa: E402

client = TestClient(main.app)


def as_user(uid, role="customer"):
    headers = {"Authorization": f"Bearer dev:{uid}"}
    client.post("/api/me/role", json={"role": role}, headers=headers)
    return headers


def last_event():
    return json.loads(open(backend.DATA_DIR / "events.jsonl").read().splitlines()[-1])


def test_runtime_check_falls_back_to_local():
    assert backend.vertex_enabled() == (False, None)
    assert client.get("/health").json()["backend"] == "local"


def test_redacts_card_numbers_and_ssn():
    assert main.redact("card 4111 1111 1111 1111, ssn 123-45-6789, $480") == "card [REDACTED], ssn [REDACTED], $480"


def test_staff_routes_need_sign_in_and_staff_role():
    assert client.get("/api/cases").status_code == 401
    assert client.get("/api/cases", headers=as_user("cust")).status_code == 403
    assert len(client.get("/api/cases", headers=as_user("agent1", "staff")).json()) >= 16  # seeded


def test_filed_case_belongs_to_customer_with_reg_e_deadline_and_escalates():
    result = agent.file_complaint("Checking or savings account", "Unauthorized transaction", "Two $480 charges.",
                                  "high", ["Reg E"], SimpleNamespace(user_id="alice"))
    case = backend.get("cases", result["case_id"])
    opened = datetime.datetime.fromisoformat(case["created"])
    assert case["owner"] == "alice" and case["due"] == cases.iso(cases.add_business_days(opened, 10))
    assert last_event() == {"event": "escalated", "case": case}
    assert [c["id"] for c in client.get("/api/my/cases", headers=as_user("alice")).json()] == [case["id"]]
    assert client.get("/api/my/cases", headers=as_user("bob")).json() == []


def test_staff_reply_reaches_customer_but_internal_note_does_not():
    case = cases.open_case("carol", "Credit card", "Billing dispute", "Refund missing.", "medium", ["Reg Z"])
    body = {"status": "resolved", "reply": "Refunded.", "note": "Card 4111111111111111 checked"}
    updated = client.patch(f"/api/cases/{case['id']}", json=body, headers=as_user("agent2", "staff")).json()
    assert updated["timeline"][-2]["text"] == "Card [REDACTED] checked"
    assert last_event()["event"] == "status_changed"
    seen = client.get("/api/my/cases", headers=as_user("carol")).json()[0]["timeline"]
    assert [e["kind"] for e in seen] == ["status", "reply", "status"]


def test_analyst_tools_use_cases_and_cfpb_sample():
    assert agent.case_breakdown("urgency", days=60)["cases"] >= 16
    top = agent.industry_breakdown("credit card", "issue")
    assert top["top"][0]["complaints"] > 0 and "sample" in top["source"]


def test_dialogflow_webhook_returns_fulfillment(monkeypatch):
    async def fake_ask(runner, user_id, session_id, text):
        return f"{user_id}:{text}"
    monkeypatch.setattr(main, "ask", fake_ask)
    body = {"sessionInfo": {"session": "projects/p/locations/global/agents/a/sessions/abc"}, "text": "hi"}
    r = client.post("/dialogflow/webhook", json=body).json()
    assert r["fulfillmentResponse"]["messages"][0]["text"]["text"] == ["cx-abc:hi"]


def test_staff_override_of_ai_triage_is_logged_recomputes_deadline_and_stays_internal():
    case = cases.open_case("dave", "Checking or savings account", "Fee dispute", "Charged twice.", "low", [])
    body = {"urgency": "high", "regulations": ["Reg E"]}
    updated = client.patch(f"/api/cases/{case['id']}", json=body, headers=as_user("agent3", "staff")).json()
    opened = datetime.datetime.fromisoformat(case["created"])
    assert updated["urgency"] == "high" and updated["triage"]["urgency"] == "low"
    assert updated["due"] == cases.iso(cases.add_business_days(opened, 10))
    assert "AI suggested low" in updated["timeline"][-2]["text"]
    seen = client.get("/api/my/cases", headers=as_user("dave")).json()[0]["timeline"]
    assert [e["kind"] for e in seen] == ["status"]


def test_customer_attaches_document_only_owner_and_staff_can_read_it():
    case = cases.open_case("erin", "Mortgage", "Payment not credited", "Paid in full.", "high", ["RESPA"])
    url = f"/api/my/cases/{case['id']}/files?name=statement.pdf"
    pdf = {**as_user("erin"), "Content-Type": "application/pdf"}
    assert client.post(url, content=b"%PDF-1.4 demo", headers=pdf).json()["files"][0]["name"] == "statement.pdf"
    assert client.post(url, content=b"x", headers={**as_user("erin"), "Content-Type": "text/html"}).status_code == 415
    assert client.post(url, content=b"x", headers={**as_user("mallory"), "Content-Type": "application/pdf"}).status_code == 404
    get = f"/api/cases/{case['id']}/files/0"
    assert client.get(get, headers=as_user("agent4", "staff")).content == b"%PDF-1.4 demo"
    assert client.get(get, headers=as_user("mallory")).status_code == 404


def test_customer_writes_on_own_open_case_and_staff_can_assign_it():
    case = cases.open_case("fay", "Credit card", "Billing dispute", "Refund missing.", "medium", ["Reg Z"])
    url = f"/api/my/cases/{case['id']}/messages"
    seen = client.post(url, json={"text": "Receipt attached, card 4111111111111111"}, headers=as_user("fay")).json()
    assert seen["timeline"][-1] == {**seen["timeline"][-1], "kind": "customer", "text": "Receipt attached, card [REDACTED]"}
    assert client.post(url, json={"text": "hi"}, headers=as_user("mallory")).status_code == 404
    staff = as_user("agent5", "staff")
    assert client.patch(f"/api/cases/{case['id']}", json={"assign": "me", "status": "resolved"}, headers=staff).json()["assignee"] == "agent5"
    assert client.post(url, json={"text": "still wrong"}, headers=as_user("fay")).status_code == 409
