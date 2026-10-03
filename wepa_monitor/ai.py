"""AI-written insights, grounded in the monitor's own numbers.

The AI never sees raw data and never computes anything. The app computes every figure (the same
metrics, stories and answers the pages show), hands the model a compact fact sheet, and the model
turns it into the kind of plain, human summary a ResNet supervisor would write: what happened,
what it means for students, what to do next. If the AI is off, slow, or fails, every page falls
back to the built-in, rule-written text, so the site never depends on it.

Providers (environment variables; nothing is sent anywhere unless one is configured):

  WEPA_AI_PROVIDER=copilot     GitHub Copilot SDK (pip package github-copilot-sdk). Auth with
                               COPILOT_GITHUB_TOKEN (a Copilot-licensed account's token), or bring
                               your own model key: WEPA_AI_BASE_URL, WEPA_AI_API_KEY and
                               WEPA_AI_BYOK_TYPE (openai | azure | anthropic).
  WEPA_AI_PROVIDER=anthropic   Claude, through the Anthropic API (pip package anthropic). Auth with
                               ANTHROPIC_API_KEY (a key from the Claude Console, billed per use; a
                               claude.ai subscription can't be used for this). Model:
                               WEPA_AI_MODEL, default claude-opus-5-5.
  WEPA_AI_PROVIDER=openai      Any OpenAI-compatible chat endpoint (OpenAI, Azure OpenAI / Azure AI
                               Foundry): WEPA_AI_BASE_URL (…/v1 or the Azure deployment URL),
                               WEPA_AI_API_KEY, WEPA_AI_MODEL.
  WEPA_AI_MODEL                Model name (optional for Copilot: its default model is used).
  WEPA_AI_MAX_PER_HOUR         Spending guard: AI calls allowed per hour (default 120).

Safety: the Copilot session runs in the SDK's "empty" mode with one custom, read-only tool
(answer a question from the monitoring data); built-in tools (shell, files, web) are not
available and any permission request is refused. Only printer data is sent; there is no data
about students or site visitors in the fact sheet.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from . import config

SYSTEM_PROMPT = """You write for Bridgewater State University's ResNet and IT Service Center staff about
the Wepa print stations on campus. Your readers are busy, non-technical supervisors and student workers.

Rules:
- Use ONLY the facts provided (and the wepa_query tool if available). Never invent stations, numbers,
  causes or dates. Copy numbers exactly as given; don't recompute or round them differently.
- If the facts don't answer the question, say so plainly and suggest what to look at in the dashboard.
- Write like a thoughtful colleague: warm, direct, specific. Name the station or building. Say what it
  meant for students (could they print somewhere nearby?) and what's worth doing next.
- Be honest about uncertainty: a few days of data, small counts, or demo data must be mentioned when they
  matter. Don't call something a trend from one or two data points.
- No jargon (no "MTTR", "p-value", "regression" unless the reader asked). US units and 12-hour times.
- Keep it short: 2-4 sentences for a summary, up to ~150 words for an answer. Plain text, no headings,
  no bullet lists unless the question asks for a list. No emoji.
- The data is about printers, never about people. Don't speculate about who caused a problem."""


@dataclass
class Reply:
    text: str
    provider: str
    model: str
    seconds: float


class AIError(Exception):
    pass


_cache: dict[str, tuple[float, Reply]] = {}
_calls: deque = deque()
_state = {"last_error": "", "last_ok": 0.0, "calls": 0}
_lock = threading.Lock()
CACHE_S = 15 * 60


def provider() -> str:
    return os.environ.get("WEPA_AI_PROVIDER", "").strip().lower()


def enabled() -> bool:
    p = provider()
    if p == "copilot":
        return bool(os.environ.get("COPILOT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
                    or os.environ.get("WEPA_AI_API_KEY"))
    if p == "openai":
        return bool(os.environ.get("WEPA_AI_BASE_URL") and os.environ.get("WEPA_AI_API_KEY"))
    if p == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("WEPA_AI_API_KEY"))
    return p == "fake"            # tests


def label() -> str:
    return {"copilot": "GitHub Copilot", "anthropic": "Claude", "openai": "your AI model",
            "fake": "test model"}.get(provider(), "AI")


def status() -> dict:
    return {"enabled": enabled(), "provider": label() if enabled() else "off",
            "model": os.environ.get("WEPA_AI_MODEL", ANTHROPIC_DEFAULT_MODEL if provider() == "anthropic" else "default"), "calls": _state["calls"],
            "last_error": _state["last_error"], "last_ok": _state["last_ok"]}


def _allow() -> bool:
    limit = int(os.environ.get("WEPA_AI_MAX_PER_HOUR", "120"))
    now = time.time()
    with _lock:
        while _calls and now - _calls[0] > 3600:
            _calls.popleft()
        if len(_calls) >= limit:
            return False
        _calls.append(now)
        return True


def ask(prompt: str, facts: str, cache_key: str = "", tool=None, timeout: float = 45.0) -> Reply:
    """One grounded completion. `tool(question) -> str` lets Copilot look things up itself.
    Raises AIError when unavailable (callers fall back to rule-written text)."""
    if not enabled():
        raise AIError("AI is not configured")
    key = hashlib.sha256((cache_key or prompt + facts).encode()).hexdigest()
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    if not _allow():
        raise AIError("AI hourly limit reached")
    t0 = time.time()
    message = f"FACTS (computed by the monitor; the only source of truth):\n{facts}\n\nTASK:\n{prompt}"
    try:
        p = provider()
        if p == "copilot":
            text = _copilot(message, tool, timeout)
        elif p == "openai":
            text = _openai(message, timeout)
        elif p == "anthropic":
            text = _anthropic(message, timeout)
        else:
            text = _fake(message)
    except AIError:
        raise
    except Exception as exc:  # noqa: BLE001 - any provider failure means "fall back"
        _state["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        _log(f"AI request failed ({_state['last_error']})", "warn")
        raise AIError(_state["last_error"]) from exc
    text = (text or "").strip()
    if not text:
        raise AIError("empty reply")
    reply = Reply(text, label(), os.environ.get("WEPA_AI_MODEL", "default"), time.time() - t0)
    _state["last_ok"], _state["last_error"] = time.time(), ""
    _state["calls"] += 1
    _cache[key] = (time.time(), reply)
    if len(_cache) > 500:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    _log(f"AI wrote a summary with {reply.provider} in {reply.seconds:.1f} s", "ai")
    return reply


def _log(msg: str, kind: str) -> None:
    try:
        from . import security
        security.log(msg, kind)
    except Exception:  # noqa: BLE001
        print(msg, flush=True)


# --- Claude (Anthropic API) -------------------------------------------------------------------------

ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"


def _anthropic(message: str, timeout: float) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("WEPA_AI_API_KEY"),
                                 timeout=timeout, max_retries=1)
    # Short summaries of computed facts: low effort is plenty. If a safety classifier declines, the
    # server-side fallback lets another model answer instead of returning nothing.
    r = client.beta.messages.create(
        model=os.environ.get("WEPA_AI_MODEL", ANTHROPIC_DEFAULT_MODEL), max_tokens=4000,
        system=SYSTEM_PROMPT, messages=[{"role": "user", "content": message}],
        output_config={"effort": os.environ.get("WEPA_AI_EFFORT", "low")},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    if r.stop_reason == "refusal":
        raise AIError("Claude declined to answer")
    return "".join(b.text for b in r.content if b.type == "text")


# --- OpenAI-compatible -------------------------------------------------------------------------

def _openai(message: str, timeout: float) -> str:
    import requests
    base = os.environ["WEPA_AI_BASE_URL"].rstrip("/")
    key = os.environ["WEPA_AI_API_KEY"]
    azure = ".azure.com" in base or "azure-api" in base
    url = base if base.endswith("/chat/completions") or "chat/completions?" in base else base + "/chat/completions"
    headers = {"Content-Type": "application/json", "User-Agent": config.USER_AGENT}
    headers.update({"api-key": key} if azure else {"Authorization": f"Bearer {key}"})
    body = {"messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": message}],
            "temperature": 0.3, "max_tokens": 600}
    if os.environ.get("WEPA_AI_MODEL"):
        body["model"] = os.environ["WEPA_AI_MODEL"]
    r = requests.post(url, json=body, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise AIError(f"HTTP {r.status_code}: {r.text[:160]}")
    return r.json()["choices"][0]["message"]["content"]


# --- GitHub Copilot SDK ------------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_client = None
_client_lock = threading.Lock()


def _event_loop() -> asyncio.AbstractEventLoop:
    """One background asyncio loop for the SDK (Dash callbacks are synchronous)."""
    global _loop
    with _client_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="copilot-sdk", daemon=True).start()
    return _loop


async def _get_client():
    global _client
    if _client is None:
        from copilot import CopilotClient
        token = os.environ.get("COPILOT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
        import tempfile
        home = os.environ.get("WEPA_COPILOT_HOME", os.path.join(tempfile.gettempdir(), "wepa-copilot"))
        os.makedirs(home, exist_ok=True)
        kwargs = {"mode": "empty", "log_level": "error", "base_directory": home}
        if token:
            kwargs["github_token"] = token
        _client = CopilotClient(**kwargs)
        await _client.start()
    return _client


def _deny(request, invocation):
    try:
        from copilot.generated.rpc import PermissionDecisionReject
    except ImportError:                       # pragma: no cover - older SDKs
        from copilot.session import PermissionNoResult
        return PermissionNoResult()
    return PermissionDecisionReject("This assistant can only read printer data.")


async def _copilot_async(message: str, tool) -> str:
    from copilot import ToolSet, define_tool
    from pydantic import BaseModel, Field

    client = await _get_client()
    tools, allowed = [], ToolSet()
    if tool is not None:
        class Query(BaseModel):
            question: str = Field(description="A plain-English question about BSU's print stations, e.g. "
                                              "'Which station was down the longest last week?'")

        def handler(params, inv):             # params_type below; no annotations (postponed evaluation)
            try:
                return tool(params.question)[:6000]
            except Exception as exc:  # noqa: BLE001
                return f"Lookup failed: {exc}"

        tools.append(define_tool("wepa_query", description="Answer a question from the BSU print-station "
                                 "monitoring data (computed numbers, read-only).", handler=handler,
                                 params_type=Query, skip_permission=True))
        allowed.add_custom("wepa_query")
    kwargs = dict(on_permission_request=_deny, tools=tools, available_tools=allowed,
                  system_message={"mode": "replace", "content": SYSTEM_PROMPT}, streaming=False)
    if os.environ.get("WEPA_AI_MODEL"):
        kwargs["model"] = os.environ["WEPA_AI_MODEL"]
    if os.environ.get("WEPA_AI_API_KEY") and os.environ.get("WEPA_AI_BASE_URL"):
        kwargs["provider"] = {"type": os.environ.get("WEPA_AI_BYOK_TYPE", "openai"),
                              "base_url": os.environ["WEPA_AI_BASE_URL"], "api_key": os.environ["WEPA_AI_API_KEY"]}
    session = await client.create_session(**kwargs)
    try:
        event = await session.send_and_wait(message, timeout=40.0)
        data = getattr(event, "data", None)
        return getattr(data, "content", "") or ""
    finally:
        try:
            await session.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _copilot(message: str, tool, timeout: float) -> str:
    import importlib.util
    if importlib.util.find_spec("copilot") is None:
        raise AIError("the github-copilot-sdk package isn't installed")
    fut = asyncio.run_coroutine_threadsafe(_copilot_async(message, tool), _event_loop())
    try:
        return fut.result(timeout=timeout)
    except TimeoutError as exc:
        fut.cancel()
        raise AIError("Copilot took too long to answer") from exc


# --- tests -----------------------------------------------------------------------------------------

def _fake(message: str) -> str:
    """Deterministic stand-in used by the test suite (WEPA_AI_PROVIDER=fake)."""
    facts = message.split("TASK:")[0]
    first = next((ln.strip("- ").strip() for ln in facts.splitlines()[1:] if ln.strip()), "")
    return f"In short: {first}"


# --- the fact sheet ------------------------------------------------------------------------------------

def facts(ds, ids=None, scope_text: str = "BSU print stations", period=None) -> str:
    """Everything the model may say, as short labelled lines (all numbers computed by the app)."""
    import pandas as pd

    from . import metrics as M, narrative as N, ops, report_card, support
    lines = [f"Scope: {scope_text}. Data as of {ds.as_of.tz_convert(config.LOCAL_TZ):%a %b %-d, %Y %-I:%M %p} "
             "(US Eastern)." + (" DEMO DATA: synthetic, not real printers." if ds.is_demo else "")]
    if ds.data_start is not None:
        lines.append(f"Monitoring began {ds.data_start.tz_convert(config.LOCAL_TZ):%a %b %-d, %Y}; "
                     f"{N.history_days(ds):.1f} days recorded.")
    cur = ops.current_status(ds, ids)
    down = cur[cur["state"] == "red"]
    warn = cur[cur["state"] == "yellow"]
    from .dashboard.views.common import station_messages
    lines.append(f"Right now: {len(cur) - len(down)} of {len(cur)} stations can print; {len(down)} down, "
                 f"{len(warn)} with a warning.")
    for _, r in pd.concat([down, warn]).head(8).iterrows():
        lines.append(f"- {r['label']} in {r['building']}: {'DOWN' if r['state'] == 'red' else 'warning'}: "
                     f"{'; '.join(station_messages(r)) or 'no details'}")
    for o in support.TEAMS:
        lines.append(f"{o} desk ({support.team(o)['base']}): {support.desk_status(o, ds.as_of)[1]}; hours "
                     f"{support.hours_text(o)}.")
    p = period or N.period(ds, "this_week")
    st = N.story(ds, p, ids, scope_text)
    lines.append(f"Story for {p.label}: " + N.to_text(st.paragraphs).replace("\n\n", " "))
    for days in (1, 7, 30):
        a = M.availability(ds, ds.as_of - pd.Timedelta(days=days), ds.as_of, ids)
        if a.value is not None:
            lines.append(f"Availability last {days} day(s): {a.value:.1f}% ({a.extra.get('down_h', 0):,.0f} "
                         "printer-hours down).")
    start, end = M.window(ds, 30)
    card = report_card.build(ds, start, end, ids)
    graded = card[card["score"].notna()]
    if len(graded):
        worst = graded.head(3)
        best = graded.sort_values("score", ascending=False).head(2)
        lines.append("Report card (30 days), weakest: " + "; ".join(
            f"{r.label} grade {r.grade} ({r.why})" for r in worst.itertuples()))
        lines.append("Strongest: " + "; ".join(f"{r.label} grade {r.grade}" for r in best.itertuples()))
    use = M.usage_by_station(ds, start, end, ids)
    use = use[use["relative"].notna()]
    if len(use):
        lines.append("Busiest printers (30 days): " + "; ".join(
            f"{r.description} {r.relative:.1f}x typical" for r in use.head(3).itertuples()))
    return "\n".join(lines)
