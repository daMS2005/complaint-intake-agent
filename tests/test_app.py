import json
import os

os.environ["FORCE_LOCAL"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import agent, backend, main  # noqa: E402


def test_runtime_check_falls_back_to_local():
    assert backend.vertex_enabled() == (False, None)
    assert TestClient(main.app).get("/health").json()["backend"] == "local"


def test_redacts_card_numbers_and_ssn():
    assert main.redact("card 4111 1111 1111 1111, ssn 123-45-6789, $480") == "card [REDACTED], ssn [REDACTED], $480"


def test_high_urgency_case_is_stored_and_escalated(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DATA_DIR", tmp_path)
    result = agent.file_complaint("Credit card", "Unauthorized transaction", "Two $480 charges.", "high", ["Reg Z"])
    assert result["escalated"]
    assert agent.get_case(result["case_id"])["issue"] == "Unauthorized transaction"
    assert json.loads((tmp_path / "escalations.jsonl").read_text())["id"] == result["case_id"]


def test_dialogflow_webhook_returns_fulfillment(monkeypatch):
    async def fake_ask(session_id, text):
        return f"{session_id}:{text}"
    monkeypatch.setattr(main, "ask", fake_ask)
    body = {"sessionInfo": {"session": "projects/p/locations/global/agents/a/sessions/abc"}, "text": "hi"}
    r = TestClient(main.app).post("/dialogflow/webhook", json=body).json()
    assert r["fulfillmentResponse"]["messages"][0]["text"]["text"] == ["abc:hi"]
