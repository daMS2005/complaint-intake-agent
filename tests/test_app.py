import json
import os
import tempfile

os.environ["FORCE_LOCAL"] = "1"
os.environ["DATA_DIR"] = tempfile.mkdtemp()

from fastapi.testclient import TestClient  # noqa: E402

from app import agent, backend, main  # noqa: E402

client = TestClient(main.app)


def events():
    return [json.loads(line) for line in open(backend.DATA_DIR / "events.jsonl")]


def test_runtime_check_falls_back_to_local():
    assert backend.vertex_enabled() == (False, None)
    assert client.get("/health").json()["backend"] == "local"


def test_redacts_card_numbers_and_ssn():
    assert main.redact("card 4111 1111 1111 1111, ssn 123-45-6789, $480") == "card [REDACTED], ssn [REDACTED], $480"


def test_high_urgency_case_is_stored_and_escalated():
    result = agent.file_complaint("Credit card", "Unauthorized transaction", "Two $480 charges.", "high", ["Reg Z"])
    assert result["escalated"]
    assert agent.get_case(result["case_id"])["status"] == "new"
    assert events()[-1] == {"event": "escalated", "case": backend.load_case(result["case_id"])}


def test_desk_status_change_emits_event_and_keeps_notes():
    case_id = client.get("/api/cases").json()[0]["id"]
    r = client.patch(f"/api/cases/{case_id}", json={"status": "resolved", "note": "Refunded, card 4111111111111111"})
    assert r.json()["status"] == "resolved"
    assert r.json()["notes"][-1]["text"] == "Refunded, card [REDACTED]"
    assert events()[-1]["event"] == "status_changed"


def test_analyst_tools_use_seeded_cases_and_cfpb_sample():
    assert agent.case_breakdown("urgency", days=60)["cases"] >= 16
    top = agent.industry_breakdown("credit card", "issue")
    assert top["top"][0]["complaints"] > 0 and "sample" in top["source"]


def test_dialogflow_webhook_returns_fulfillment(monkeypatch):
    async def fake_ask(runner, session_id, text):
        return f"{session_id}:{text}"
    monkeypatch.setattr(main, "ask", fake_ask)
    body = {"sessionInfo": {"session": "projects/p/locations/global/agents/a/sessions/abc"}, "text": "hi"}
    r = client.post("/dialogflow/webhook", json=body).json()
    assert r["fulfillmentResponse"]["messages"][0]["text"]["text"] == ["abc:hi"]
