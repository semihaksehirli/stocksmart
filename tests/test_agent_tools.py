import json
from types import SimpleNamespace

from app import agent_tools, services
from app.agent_tools import check_stock, inventory_report, list_low_stock, record_delivery, record_sale


def setup_items(db):
    services.create_product(db, sku="COF-001", name="House Blend", price=10, cost=5, quantity=10, reorder_level=3)
    services.create_product(db, sku="DAI-002", name="Oat Milk", price=5, cost=3, quantity=2, reorder_level=4)


def test_tools(db):
    setup_items(db)
    assert "10 on hand" in check_stock("COF-001")
    assert "Oat Milk" in list_low_stock() and "House Blend" not in list_low_stock()
    assert "7 left" in record_sale("house blend", 3)
    assert "Not enough stock" in record_sale("COF-001", 50)
    assert "Now 14 on hand" in record_delivery("DAI-002", 12)
    assert "No products are low" in list_low_stock()
    assert "Units sold: 3" in inventory_report()
    assert "No product matches" in check_stock("nothing")


def test_builtin_chat_routes_to_tools(db, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    setup_items(db)
    assert "Oat Milk" in agent_tools.answer_offline("what's running low?")
    assert "Recorded sale of 2" in agent_tools.answer_offline("sold 2 COF-001")
    assert "8 on hand" in agent_tools.answer_offline("check COF-001")
    assert "Inventory value" in agent_tools.answer_offline("give me a report")


def test_llm_mode_calls_tools(db, monkeypatch):
    """Fake an OpenAI-compatible model: it asks for a tool, then answers with the result."""
    setup_items(db)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    calls = []

    def create(model, messages, tools):
        calls.append(messages)
        if len(calls) == 1:
            tc = SimpleNamespace(id="1", function=SimpleNamespace(name="record_sale", arguments=json.dumps({"product": "COF-001", "quantity": 4})))
            msg = SimpleNamespace(tool_calls=[tc], content=None)
        else:
            msg = SimpleNamespace(tool_calls=None, content="Done: " + messages[-1]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr("openai.OpenAI", lambda **kw: fake)
    reply = agent_tools.answer("I sold 4 house blend")
    assert "Recorded sale of 4" in reply and "6 left" in reply
    assert len(calls) == 2
