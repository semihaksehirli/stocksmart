"""AI providers for the assistant, with automatic fallback.

The owner adds free API keys for several OpenAI-compatible providers (Gemini, Groq,
Cerebras, OpenRouter, Mistral, GitHub Models, a local Ollama, or any custom endpoint)
and puts them in order. For each question the assistant tries them in order:

  * rate limited / quota used up  -> rest that provider for a while, try the next one
  * bad key, unknown model, down   -> try the next one
  * every provider unavailable     -> built-in mode (simple keyword commands)

Safety rule: if a provider has already *changed stock* (recorded a sale or delivery)
and then fails, we stop there instead of asking the next provider, which would
record it a second time.

This module has no database code so it can be tested with a fake client.
"""
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------- presets ----------

PRESETS: Dict[str, Dict[str, Any]] = {
    "gemini": {
        "label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-2.5-flash", "key_url": "https://aistudio.google.com/apikey",
        "free": "Free key with a Google account. Roughly 1,500 requests a day on Flash models.",
        "note": "Outside the EU/UK, Google may use free-tier prompts to improve its products.",
    },
    "groq": {
        "label": "Groq", "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile", "key_url": "https://console.groq.com/keys",
        "free": "Free tier, no card. Roughly 1,000 requests a day. Very fast.", "note": "",
    },
    "cerebras": {
        "label": "Cerebras", "base_url": "https://api.cerebras.ai/v1",
        "model": "", "key_url": "https://cloud.cerebras.ai",
        "free": "Free tier, no card. Roughly 1 million tokens a day.", "note": "Press Load models to pick a model.",
    },
    "openrouter": {
        "label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
        "model": "", "key_url": "https://openrouter.ai/keys",
        "free": "Free models end in “:free”. About 50 requests a day without credit.",
        "note": "Press Load models and pick a “:free” model that supports tools.",
    },
    "mistral": {
        "label": "Mistral", "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest", "key_url": "https://console.mistral.ai/api-keys",
        "free": "Free Experiment plan with a generous monthly allowance.",
        "note": "The free plan requires agreeing that Mistral may train on your requests.",
    },
    "github": {
        "label": "GitHub Models", "base_url": "https://models.github.ai/inference",
        "model": "openai/gpt-4.1-mini", "key_url": "https://github.com/settings/personal-access-tokens",
        "free": "Free with a GitHub account. Use a token with the “Models” permission. Roughly 150 requests a day.",
        "note": "",
    },
    "ollama": {
        "label": "Ollama (on this computer)", "base_url": "http://localhost:11434/v1",
        "model": "llama3.1", "key_url": "https://ollama.com/download",
        "free": "Runs on your own computer: unlimited and private, no key needed.",
        "note": "Install Ollama and run “ollama pull llama3.1” first.",
    },
    "custom": {
        "label": "Other (OpenAI-compatible)", "base_url": "", "model": "", "key_url": "",
        "free": "Any service with an OpenAI-compatible chat API.", "note": "",
    },
}
NO_KEY_NEEDED = {"ollama"}


@dataclass
class ProviderConfig:
    id: int
    preset: str
    name: str
    base_url: str
    api_key: str
    model: str


@dataclass
class Attempt:
    provider_id: int
    name: str
    outcome: str            # ok | rate_limited | auth | bad_request | unavailable | error
    message: str = ""
    cooldown_seconds: int = 0


@dataclass
class ChainResult:
    reply: str
    provider: Optional[str] = None   # name of the provider that answered, None = built-in
    model: Optional[str] = None
    attempts: List[Attempt] = field(default_factory=list)


# ---------- error classification ----------

def classify(exc: Exception) -> Tuple[str, int, str]:
    """Map an OpenAI-SDK (or similar) error to (outcome, cooldown_seconds, message)."""
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    text = str(exc)
    short = text[:200]
    if status == 429:
        retry = 0
        try:
            retry = int(float(response.headers.get("retry-after", 0))) if response is not None else 0
        except (TypeError, ValueError, AttributeError):
            retry = 0
        lowered = text.lower()
        daily = any(w in lowered for w in ("per day", "daily", "quota", "exhausted", "insufficient", "credits"))
        cooldown = retry or (60 * 60 if daily else 60)
        return "rate_limited", max(30, min(cooldown, 24 * 3600)), "Free limit reached" + (" for today" if daily else "") + "."
    if status in (401, 403):
        return "auth", 0, "The API key was rejected. Check it, or create a new one."
    if status == 402:
        return "rate_limited", 6 * 3600, "No free credit left on this account."
    if status in (400, 404, 422):
        return "bad_request", 0, f"The provider refused the request (model name or tool support?): {short}"
    if status is None and any(n in type(exc).__name__ for n in ("Connection", "Timeout")):
        return "unavailable", 120, "Couldn't reach the provider."
    if status is not None and status >= 500:
        return "unavailable", 120, f"The provider had an error ({status})."
    return "error", 60, short


# ---------- running one provider ----------

def default_client_factory(cfg: ProviderConfig):
    from openai import OpenAI

    # max_retries=0: on a rate limit we move to the next provider instead of waiting.
    return OpenAI(api_key=cfg.api_key or "not-needed", base_url=cfg.base_url, max_retries=0, timeout=45)


def _extra_content(call) -> Optional[dict]:
    """Provider-specific data on a tool call, e.g. Gemini's thought signature, which must be sent back."""
    extra = getattr(call, "extra_content", None)
    if extra is None:
        extra = (getattr(call, "model_extra", None) or {}).get("extra_content")
    if extra is not None and hasattr(extra, "model_dump"):
        extra = extra.model_dump()
    return extra if isinstance(extra, dict) else None


def _assistant_message(reply, cfg: Optional["ProviderConfig"] = None) -> dict:
    msg: Dict[str, Any] = {"role": "assistant", "content": reply.content or ""}
    if reply.tool_calls:
        calls = []
        for c in reply.tool_calls:
            d: Dict[str, Any] = {"id": c.id, "type": "function",
                                 "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
            extra = _extra_content(c)
            if extra:
                d["extra_content"] = extra
            calls.append(d)
        # Gemini 3 needs a thought signature on the first function call of a turn. If the reply
        # didn't include one, Google documents this placeholder to skip the check.
        if cfg is not None and "googleapis.com" in (cfg.base_url or "") and calls:
            google = calls[0].setdefault("extra_content", {}).setdefault("google", {})
            google.setdefault("thought_signature", "skip_thought_signature_validator")
        msg["tool_calls"] = calls
    return msg


class StockChanged(Exception):
    """The provider failed after it had already changed stock; don't retry elsewhere."""

    def __init__(self, done: List[str], cause: Exception):
        super().__init__(str(cause))
        self.done, self.cause = done, cause


def run_one(cfg: ProviderConfig, messages: List[dict], tools: List[dict],
            execute_tool: Callable[[str, dict], Tuple[str, bool]], client_factory=None,
            max_steps: int = 5) -> str:
    """Tool-calling loop against one provider. `execute_tool` returns (result_text, changed_stock)."""
    client = (client_factory or default_client_factory)(cfg)
    messages = list(messages)
    changes: List[str] = []
    try:
        for _ in range(max_steps):
            reply = client.chat.completions.create(model=cfg.model, messages=messages, tools=tools).choices[0].message
            if not reply.tool_calls:
                return reply.content or "(No answer.)"
            messages.append(_assistant_message(reply, cfg))
            for call in reply.tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                    result, changed = execute_tool(call.function.name, args)
                except (TypeError, ValueError) as exc:
                    result, changed = f"Bad tool arguments: {exc}", False
                if changed:
                    changes.append(result)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        return "\n".join(changes) or "Sorry, I couldn't finish that request."
    except Exception as exc:
        if changes:
            raise StockChanged(changes, exc) from exc
        raise


def run_chain(providers: List[ProviderConfig], messages: List[dict], tools: List[dict],
              execute_tool: Callable[[str, dict], Tuple[str, bool]], fallback: Callable[[], str],
              client_factory=None) -> ChainResult:
    attempts: List[Attempt] = []
    for cfg in providers:
        try:
            reply = run_one(cfg, messages, tools, execute_tool, client_factory)
            attempts.append(Attempt(cfg.id, cfg.name, "ok"))
            return ChainResult(reply, cfg.name, cfg.model, attempts)
        except StockChanged as sc:
            outcome, cooldown, msg = classify(sc.cause)
            attempts.append(Attempt(cfg.id, cfg.name, outcome, msg, cooldown))
            return ChainResult("\n".join(sc.done) + f"\n({cfg.name} stopped before finishing, so I didn't retry "
                               "with another provider to avoid recording this twice.)", cfg.name, cfg.model, attempts)
        except Exception as exc:  # any provider problem: note it and try the next one
            outcome, cooldown, msg = classify(exc)
            attempts.append(Attempt(cfg.id, cfg.name, outcome, msg, cooldown))
    note = ""
    if providers:
        note = "(All AI providers are unavailable right now, so I'm in built-in mode.)\n"
    return ChainResult(note + fallback(), None, None, attempts)


def probe_tools(cfg: ProviderConfig, tools: List[dict], client_factory=None) -> Tuple[bool, str]:
    """Check a provider answers and can call tools. Executes nothing."""
    client = (client_factory or default_client_factory)(cfg)
    msgs = [{"role": "system", "content": "You are testing tool use."},
            {"role": "user", "content": "Use the list_low_stock tool to see what's running low."}]
    start = time.time()
    reply = client.chat.completions.create(model=cfg.model, messages=msgs, tools=tools).choices[0].message
    secs = time.time() - start
    if reply.tool_calls:
        return True, f"Works and can use StockSmart's tools ({secs:.1f}s)."
    return False, "It answered, but didn't use tools. The assistant needs a model with tool (function) calling; try another model."
