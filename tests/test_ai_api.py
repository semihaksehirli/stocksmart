"""AI provider settings and the assistant's fallback, through the REST API (fake AI clients)."""
import json
from types import SimpleNamespace

import pytest

from app import ai
from tests.conftest import login


class ApiErr(Exception):
    def __init__(self, status, text="error"):
        super().__init__(text)
        self.status_code = status
        self.response = SimpleNamespace(headers={})


@pytest.fixture()
def fake_ai(monkeypatch):
    """Each provider (by model name) answers from a queue; an empty queue means 'Hello from <model>'."""
    queues, seen = {}, []

    def make(cfg):
        def create(model, messages, tools):
            seen.append(model)
            q = queues.get(model, [])
            step = q.pop(0) if q else SimpleNamespace(content=f"Hello from {model}", tool_calls=None)
            if isinstance(step, Exception):
                raise step
            return SimpleNamespace(choices=[SimpleNamespace(message=step)])
        models = SimpleNamespace(list=lambda: [SimpleNamespace(id="models/gemini-a"), SimpleNamespace(id="x:free")])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), models=models)

    monkeypatch.setattr(ai, "default_client_factory", make)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    return queues, seen


def add(client, h, preset, model, key="key-123456789"):
    r = client.post("/ai/providers", json={"preset": preset, "api_key": key, "model": model}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def test_settings_are_owner_only(client):
    staff = login(client, "staff", "staff1234")
    assert client.get("/ai/providers", headers=staff).status_code == 403


def test_add_mask_reorder(client, fake_ai):
    h = login(client, "owner", "owner1234")
    assert client.post("/ai/providers", json={"preset": "groq", "model": "m"}, headers=h).status_code == 400  # no key
    g = add(client, h, "gemini", "")  # preset default model used
    assert g["model"] == "gemini-2.5-flash" and g["api_key"] == "•••• 6789" and g["base_url"].startswith("https://generativelanguage")
    q = add(client, h, "groq", "llama-x")
    o = client.post("/ai/providers", json={"preset": "ollama", "model": "llama3.1"}, headers=h).json()  # no key needed
    order = client.post("/ai/providers/order", json={"ids": [q["id"], o["id"], g["id"]]}, headers=h).json()
    assert [p["name"] for p in order] == ["Groq", "Ollama (on this computer)", "Google Gemini"]
    r = client.patch(f"/ai/providers/{q['id']}", json={"api_key": "", "model": "llama-y"}, headers=h).json()
    assert r["api_key"] == "•••• 6789" and r["model"] == "llama-y"
    models = client.post("/ai/models", json={"preset": "gemini", "provider_id": g["id"]}, headers=h).json()["models"]
    assert models == ["x:free", "gemini-a"]


def test_assistant_falls_back_and_rests_limited_provider(client, fake_ai):
    queues, seen = fake_ai
    h = login(client, "owner", "owner1234")
    add(client, h, "gemini", "gem")
    add(client, h, "groq", "llama")
    queues["gem"] = [ApiErr(429, "quota exceeded per day")]

    r = client.post("/assistant", json={"message": "hi"}, headers=h).json()
    assert r["provider"] == "Groq" and r["reply"] == "Hello from llama" and r["mode"] == "ai"
    assert r["skipped"] and r["skipped"][0].startswith("Google Gemini: Free limit reached for today")
    gem = client.get("/ai/providers", headers=h).json()[0]
    assert gem["resting_until"] is not None and gem["last_ok"] is False

    seen.clear()
    r = client.post("/assistant", json={"message": "again", "history": [{"role": "user", "content": "hi"}]}, headers=h).json()
    assert seen == ["llama"]  # Gemini is resting, so it isn't even tried
    # turning a provider back on clears its rest period
    client.patch(f"/ai/providers/{gem['id']}", json={"enabled": True}, headers=h)
    assert client.get("/ai/providers", headers=h).json()[0]["resting_until"] is None


def test_assistant_built_in_when_no_providers(client, fake_ai):
    h = login(client, "owner", "owner1234")
    r = client.post("/assistant", json={"message": "what's running low?"}, headers=h).json()
    assert r["mode"] == "built-in" and r["provider"] is None


def test_test_button_reports_tool_support(client, fake_ai):
    queues, _ = fake_ai
    h = login(client, "owner", "owner1234")
    p = add(client, h, "groq", "llama")
    call = SimpleNamespace(id="1", function=SimpleNamespace(name="list_low_stock", arguments="{}"))
    queues["llama"] = [SimpleNamespace(content=None, tool_calls=[call])]
    assert client.post(f"/ai/providers/{p['id']}/test", headers=h).json()["ok"] is True
    queues["llama"] = [ApiErr(401)]
    r = client.post(f"/ai/providers/{p['id']}/test", headers=h).json()
    assert r["ok"] is False and "key was rejected" in r["message"]
