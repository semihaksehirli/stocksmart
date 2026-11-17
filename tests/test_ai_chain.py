"""Assistant provider fallback (app/ai.py) with fake AI clients — no network, no database."""
import json
from types import SimpleNamespace

from app import ai
from app.ai import ProviderConfig, classify, probe_tools, run_chain


class ApiErr(Exception):
    def __init__(self, status, text="error", headers=None):
        super().__init__(text)
        self.status_code = status
        self.response = SimpleNamespace(headers=headers or {})


def msg(content=None, calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))])


def tool_call(name, args, cid="c1"):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def factory(scripts, log):
    """scripts: provider name -> list of responses (or exceptions) returned in order."""
    def make(cfg):
        def create(model, messages, tools):
            log.append((cfg.name, model, messages))
            step = scripts[cfg.name].pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return make


P1 = ProviderConfig(1, "gemini", "Gemini", "u1", "k1", "gemini-x")
P2 = ProviderConfig(2, "groq", "Groq", "u2", "k2", "llama-x")
P3 = ProviderConfig(3, "ollama", "Ollama", "u3", "", "llama3.1")
MSGS = [{"role": "user", "content": "what's low?"}]
TOOLS = [{"type": "function", "function": {"name": "list_low_stock"}}]


def tools_exec(log):
    def run(name, args):
        log.append((name, args))
        if name == "record_sale":
            return f"Recorded sale of {args['quantity']} x Chips. 4 left.", True
        return "- Oat Milk: 2 left", False
    return run


def test_rate_limited_provider_falls_through_to_next():
    log, ran = [], []
    scripts = {"Gemini": [ApiErr(429, "Rate limit")], "Groq": [msg(calls=[tool_call("list_low_stock", {})]), msg("Oat Milk is low.")]}
    r = run_chain([P1, P2], MSGS, TOOLS, tools_exec(ran), lambda: "offline", factory(scripts, log))
    assert r.reply == "Oat Milk is low." and r.provider == "Groq" and r.model == "llama-x"
    assert [(a.name, a.outcome) for a in r.attempts] == [("Gemini", "rate_limited"), ("Groq", "ok")]
    assert r.attempts[0].cooldown_seconds == 60
    assert ran == [("list_low_stock", {})]
    assert log[-1][2][-1]["role"] == "tool"  # tool result was sent back to the model


def test_cooldowns_follow_the_provider():
    assert classify(ApiErr(429, "x", {"retry-after": "120"}))[1] == 120
    assert classify(ApiErr(429, "You exceeded your current quota, requests per day"))[1] == 3600
    assert classify(ApiErr(401))[0] == "auth"
    assert classify(ApiErr(404, "model not found"))[0] == "bad_request"
    assert classify(ApiErr(503))[0] == "unavailable"

    class APIConnectionError(Exception):
        pass
    assert classify(APIConnectionError("refused"))[0] == "unavailable"


def test_bad_key_and_bad_model_are_skipped():
    log = []
    scripts = {"Gemini": [ApiErr(401)], "Groq": [ApiErr(400, "model does not support tools")], "Ollama": [msg("Hi")]}
    r = run_chain([P1, P2, P3], MSGS, TOOLS, tools_exec([]), lambda: "offline", factory(scripts, log))
    assert r.provider == "Ollama" and [a.outcome for a in r.attempts] == ["auth", "bad_request", "ok"]


def test_everything_down_uses_built_in_mode():
    scripts = {"Gemini": [ApiErr(429)], "Groq": [ApiErr(500)]}
    r = run_chain([P1, P2], MSGS, TOOLS, tools_exec([]), lambda: "offline answer", factory(scripts, []))
    assert r.provider is None and r.reply.endswith("offline answer") and "built-in mode" in r.reply
    assert run_chain([], MSGS, TOOLS, tools_exec([]), lambda: "offline answer", factory({}, [])).reply == "offline answer"


def test_never_records_a_sale_twice():
    """Gemini records the sale, then hits its limit. Groq must NOT be asked to redo it."""
    log, ran = [], []
    scripts = {"Gemini": [msg(calls=[tool_call("record_sale", {"product": "SNK-001", "quantity": 2})]), ApiErr(429)],
               "Groq": [msg("should not be called")]}
    r = run_chain([P1, P2], MSGS, TOOLS, tools_exec(ran), lambda: "offline", factory(scripts, log))
    assert ran == [("record_sale", {"product": "SNK-001", "quantity": 2})]
    assert "Recorded sale of 2" in r.reply and "didn't retry" in r.reply
    assert all(name == "Gemini" for name, _, _ in log)
    assert r.attempts[0].outcome == "rate_limited"


def test_read_only_failure_still_falls_through():
    ran = []
    scripts = {"Gemini": [msg(calls=[tool_call("list_low_stock", {})]), ApiErr(429)], "Groq": [msg("Oat Milk.")]}
    r = run_chain([P1, P2], MSGS, TOOLS, tools_exec(ran), lambda: "offline", factory(scripts, []))
    assert r.provider == "Groq"


def test_probe_detects_tool_support():
    ok, text = probe_tools(P1, TOOLS, factory({"Gemini": [msg(calls=[tool_call("list_low_stock", {})])]}, []))
    assert ok and "can use" in text
    ok, text = probe_tools(P1, TOOLS, factory({"Gemini": [msg("Here is a list...")]}, []))
    assert not ok and "tool" in text


def test_presets_are_complete():
    for key, p in ai.PRESETS.items():
        assert {"label", "base_url", "model", "key_url", "free", "note"} <= set(p)
        if key != "custom":
            assert p["base_url"].startswith("http")


def test_gemini_thought_signature_is_sent_back():
    """Gemini 3 rejects the follow-up request unless the tool call's thought_signature is echoed."""
    gem = ProviderConfig(9, "gemini", "Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", "k", "gemini-3-flash-preview")
    call = tool_call("list_low_stock", {})
    call.extra_content = {"google": {"thought_signature": "SIG123"}}
    log = []
    scripts = {"Gemini": [msg(calls=[call]), msg("Oat Milk is low.")]}
    r = run_chain([gem], MSGS, TOOLS, tools_exec([]), lambda: "offline", factory(scripts, log))
    assert r.reply == "Oat Milk is low."
    sent = log[1][2][-2]  # the assistant message replayed in the second request
    assert sent["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "SIG123"}}

    # pydantic-style objects keep unknown fields in model_extra
    call2 = tool_call("list_low_stock", {})
    call2.model_extra = {"extra_content": {"google": {"thought_signature": "SIG456"}}}
    log2 = []
    run_chain([gem], MSGS, TOOLS, tools_exec([]), lambda: "o", factory({"Gemini": [msg(calls=[call2]), msg("ok")]}, log2))
    assert log2[1][2][-2]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "SIG456"

    # no signature returned: the documented placeholder is used, only for Google
    log3 = []
    run_chain([gem], MSGS, TOOLS, tools_exec([]), lambda: "o", factory({"Gemini": [msg(calls=[tool_call("list_low_stock", {})]), msg("ok")]}, log3))
    assert log3[1][2][-2]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "skip_thought_signature_validator"
    log4 = []
    run_chain([P2], MSGS, TOOLS, tools_exec([]), lambda: "o", factory({"Groq": [msg(calls=[tool_call("list_low_stock", {})]), msg("ok")]}, log4))
    assert "extra_content" not in log4[1][2][-2]["tool_calls"][0]
