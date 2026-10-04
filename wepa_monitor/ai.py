"""AI-written insights, grounded in the monitor's own numbers.

The model is the interpreter, never the calculator. The app computes every figure (the same metrics,
stories, forecasts and statistics the pages show) and hands the model a fact sheet plus read-only
tools to look further (analyst.py). The model reads across them like an expert analyst and writes
what a ResNet supervisor needs: what happened, why, what it means for students, what to do next.
Every figure in a reply is checked against what the model was shown. If the AI is off, slow, or fails, every page falls
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

Agent: with a toolkit (analyst.py) the model gets read-only analysis tools over all the data and
looks things up itself before answering; every reply is then fact-checked (unsupported()).

Safety: the Copilot session runs in the SDK's "empty" mode with only those custom, read-only
tools; built-in tools (shell, files, web) are not available and any permission request is refused. Only printer data is sent; there is no data
about students or site visitors in the fact sheet.
"""
from __future__ import annotations

from . import durations

import asyncio
import hashlib
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

from . import config

SYSTEM_PROMPT = """You are the analyst built into BSU Student Printing Ops, Bridgewater State University's monitor for
the Wepa print stations students use across campus (residence halls, the library, labs, the student union).
You are an expert operations and data analyst, and you work for the people who keep those printers running:
ResNet and the IT Service Center (supervisors, student workers, IT leadership).

Why this exists: students need to print (papers, forms, tickets) and a broken printer at 11 PM before a
deadline matters. The monitor reads Wepa's public status page about once a minute and turns it into
availability, outages, faults, supplies, usage and workload, so staff can fix things faster, stock the
right supplies, place printers where students need them, and report outcomes to the university.

How you work:
- You have read-only tools over all of the monitoring data and the campus context. Use them. For anything
  beyond the fact sheet, look it up; for a "why" or "what should we do", look at more than one angle
  (causes, timing vs desk hours, the calendar, backups nearby, supplies, the report card, statistics)
  before you conclude. Start with data_overview when you need to know what the data covers.
- Interpret, don't just repeat: say what the numbers mean for students and staff, what stands out, what's
  normal for this campus, and the one or two most useful next steps. Rank by impact.

Facts only. This is the most important rule:
- Every number, name, date and cause you state must come from the fact sheet or a tool result in this
  conversation. Copy figures as given (rounding is fine: 93.27% -> 93.3%). Never estimate, extrapolate or
  invent a figure, station, cause or event. Never fill a gap with general knowledge about printers.
- If the data can't answer, say so plainly and say what would answer it (or where in the dashboard to look).
- Separate what the data shows from your interpretation ("the data shows X; that suggests Y").
- Respect uncertainty: say when there are only a few days of data, few events, or wide intervals; don't
  call one or two points a trend; correlation is not cause. Anything before monitoring began is unknown.
- If the data is DEMO (synthetic), say so in your answer.

Scope and ethics:
- Stay within BSU's print stations, this data, and the university context around them (calendar, desk
  hours, residence halls, buildings, class schedules). Politely decline anything else (other topics,
  general chat, writing unrelated content) and steer back to what you can help with.
- The data is about printers, never people. Never guess who caused a problem, never single out a staff
  member or student, and don't infer anything about individuals. Be fair to the support teams: after-hours
  outages reflect desk hours, not effort.
- Don't overstate. Recommendations are suggestions for staff to weigh, not orders. No security, legal or
  purchasing advice beyond what the data supports. Wepa's data is shown for internal operations.

Vocabulary (use these words, as the dashboard does): a station is Operational, Degraded (printing with a
warning), Out of service, or No signal (hasn't reported). An outage or warning is Ongoing or Resolved; never
say "in progress" (the data can't show whether anyone is working on it). Issues: Jammed, Unreachable
(network), Out of paper, Tray disengaged, Toner depleted, Drum expired, System fault, Service required.
When the responsible desk is closed, say "support unavailable until <time>".
Durations: write every length of time in whole units, never decimals: "3 hours, 15 minutes", and past 24
hours "2 days, 4 hours, 10 minutes". Tool tables give hours as decimals (e.g. down_h 27.25); convert them
(27.25 hours -> "1 day, 3 hours, 15 minutes"). Totals across printers are "printer time" ("4 days, 2 hours
of printer downtime"), not "printer-hours".

Speaking about the app itself: you are part of BSU Student Printing Ops, so when asked about the app (is it
healthy, what version is running, is it secure or up to date, what changed, are backups working), speak
in the first person as the app ("I collected...", "I'm running version...") and answer from the
self_check tool, nothing else. Report its findings as facts with their status; never claim a check you
didn't see, never call the app "secure" or "unhackable" in absolute terms (say what is protected and what
the known limits are), and never say you have feelings, awareness or consciousness: you are software that
checks itself. You can't change, update or restart anything; point administrators to the Software page.

Style: a thoughtful senior colleague: warm, direct, specific, plain English. Name stations and buildings.
No jargon unless asked (say "average time to fix", not "MTTR"). US units, 12-hour times, Eastern time.
Short by default: 2-4 sentences for a summary, up to ~180 words for an answer, longer only when asked for a
deep dive. Markdown is fine for a short list or bold key figure; no headings, no emoji, no tables."""


@dataclass
class Reply:
    text: str
    provider: str
    model: str
    seconds: float
    tools_used: int = 0
    unverified: tuple = ()      # figures in the reply that the fact check couldn't find in the data shown


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


def ask(prompt: str, facts: str, cache_key: str = "", tool=None, timeout: float = 45.0, toolkit=None,
        effort: str | None = None) -> Reply:
    """One grounded completion. `toolkit` (an analyst.Toolkit) gives the model read-only tools over all
    the data; `tool(question) -> str` is the older single lookup. Every reply is fact-checked: figures
    that appear nowhere in the facts or the tool results get one rewrite, and any still unsupported are
    reported on the Reply. Raises AIError when unavailable (callers fall back to rule-written text)."""
    if not enabled():
        raise AIError("AI is not configured")
    key = hashlib.sha256((cache_key or prompt + facts).encode()).hexdigest()
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    if not _allow():
        raise AIError("AI hourly limit reached")
    tk = toolkit if toolkit is not None else (_QueryKit(tool) if tool is not None else None)
    t0 = time.time()
    message = (f"FACTS (computed by the monitor; with the tool results, the only source of truth):\n{facts}\n\n"
               f"TASK:\n{prompt}")
    try:
        text = _complete(message, tk, timeout, effort)
        bad = unsupported(text, [facts, prompt] + (tk.outputs if tk else []))
        if bad and _allow():
            _log(f"AI fact check: {len(bad)} figure(s) not in the data ({', '.join(bad[:5])}); asking for a fix", "warn")
            text2 = _complete(f"{message}\n\nYOUR DRAFT ANSWER:\n{text}\n\nFACT CHECK: these figures in your draft "
                              f"appear nowhere in the facts or your tool results: {', '.join(bad)}. Rewrite the "
                              "answer using only figures you were given (look them up with the tools if needed); "
                              "drop anything you can't support. Reply with the corrected answer only.",
                              tk, timeout, effort)
            if text2.strip():
                text = text2
                bad = unsupported(text, [facts, prompt] + (tk.outputs if tk else []))
    except AIError:
        raise
    except Exception as exc:  # noqa: BLE001 - any provider failure means "fall back"
        _state["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        _log(f"AI request failed ({_state['last_error']})", "warn")
        raise AIError(_state["last_error"]) from exc
    text = (text or "").strip()
    if not text:
        raise AIError("empty reply")
    reply = Reply(text, label(), os.environ.get("WEPA_AI_MODEL", "default"), time.time() - t0,
                  len(tk.calls) if tk else 0, tuple(bad))
    _state["last_ok"], _state["last_error"] = time.time(), ""
    _state["calls"] += 1
    _cache[key] = (time.time(), reply)
    if len(_cache) > 500:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    _log(f"AI answered with {reply.provider} in {reply.seconds:.1f} s"
         + (f" after {reply.tools_used} data lookup(s)" if reply.tools_used else "")
         + (f"; {len(bad)} figure(s) unverified" if bad else "; fact check passed"), "ai")
    return reply


def _complete(message: str, tk, timeout: float, effort: str | None) -> str:
    p = provider()
    if p == "copilot":
        return _copilot(message, tk, timeout)
    if p == "openai":
        return _openai(message, timeout, tk)
    if p == "anthropic":
        return _anthropic(message, timeout, tk, effort)
    return _fake(message)


class _QueryKit:
    """Adapts a plain `question -> answer` function to the toolkit interface."""

    def __init__(self, fn):
        self.fn, self.outputs, self.calls = fn, [], []

    def specs(self) -> list[dict]:
        return [{"name": "wepa_query", "description": "Answer a plain-English question from the BSU print-station "
                 "monitoring data (computed numbers, read-only).",
                 "input_schema": {"type": "object", "properties": {"question": {"type": "string"}},
                                  "required": ["question"]}}]

    def run(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        try:
            out = str(self.fn(str((args or {}).get("question", ""))))[:6000]
        except Exception as exc:  # noqa: BLE001
            out = f"Lookup failed: {exc}"
        self.outputs.append(out)
        return out


# --- fact check -------------------------------------------------------------------------------------

_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_SKIP = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:AM|PM|am|pm|a\.m\.|p\.m\.)|\b\d{1,2}:\d{2}\b|"
                   r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?\b|"
                   r"\b(?:19|20)\d{2}\b|\b\d{4}-\d{2}(?:-\d{2})?\b")
# Ways a figure can be legitimately restated: minutes<->hours<->days, percent<->share, per day<->week/month.
_RESTATE = (1, 60, 1 / 60, 24, 1 / 24, 100, 1 / 100, 7, 1 / 7, 30.44, 1 / 30.44, 1440, 1 / 1440)


def _numbers(text: str) -> list[tuple[str, float]]:
    out = []
    for m in _NUM.finditer(text):
        raw = m.group(0).rstrip(",")
        try:
            out.append((raw, float(raw.replace(",", ""))))
        except ValueError:
            pass
    return out


def unsupported(text: str, sources: list[str]) -> list[str]:
    """Figures in `text` that can't be found in `sources` (allowing rounding and unit restatements).
    Small whole numbers (0-10), years, dates and clock times are not checked."""
    known = {v for src in sources for _, v in _numbers(src or "")}
    bad = []
    for raw, v in _numbers(_SKIP.sub(" ", text)):
        if v <= 10 and v == int(v):
            continue
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        tol = 0.5 * 10 ** -decimals + 1e-9
        if any(abs(v - k * f) <= max(tol, abs(v) * 0.005) for k in known for f in _RESTATE):
            continue
        if raw not in bad:
            bad.append(raw)
    return bad


def _log(msg: str, kind: str) -> None:
    try:
        from . import security
        security.log(msg, kind)
    except Exception:  # noqa: BLE001
        print(msg, flush=True)


# --- Claude (Anthropic API) -------------------------------------------------------------------------

ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 10


def _anthropic(message: str, timeout: float, tk=None, effort: str | None = None) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("WEPA_AI_API_KEY"),
                                 timeout=timeout, max_retries=1)
    tools = tk.specs() if tk is not None else []
    messages = [{"role": "user", "content": message}]
    # If a safety classifier declines, the server-side fallback lets another model answer instead of
    # returning nothing. With tools, Claude looks things up in the data (read-only) before answering;
    # automatic prompt caching means each lookup round re-reads the conversation so far at cache price.
    for _ in range(MAX_TOOL_ROUNDS + 1):
        r = client.beta.messages.create(
            model=os.environ.get("WEPA_AI_MODEL", ANTHROPIC_DEFAULT_MODEL), max_tokens=8000,
            system=SYSTEM_PROMPT, messages=messages, tools=tools, cache_control={"type": "ephemeral"},
            output_config={"effort": effort or os.environ.get("WEPA_AI_EFFORT", "low")},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        if r.stop_reason == "refusal":
            raise AIError("Claude declined to answer")
        if r.stop_reason != "tool_use":
            return "".join(b.text for b in r.content if b.type == "text")
        messages.append({"role": "assistant", "content": r.content})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": b.id, "content": tk.run(b.name, dict(b.input or {}))}
            for b in r.content if b.type == "tool_use"]})
    raise AIError("Claude kept looking things up without answering")


# --- OpenAI-compatible -------------------------------------------------------------------------

def _openai(message: str, timeout: float, tk=None) -> str:
    import json

    import requests
    base = os.environ["WEPA_AI_BASE_URL"].rstrip("/")
    key = os.environ["WEPA_AI_API_KEY"]
    azure = ".azure.com" in base or "azure-api" in base
    url = base if base.endswith("/chat/completions") or "chat/completions?" in base else base + "/chat/completions"
    headers = {"Content-Type": "application/json", "User-Agent": config.USER_AGENT}
    headers.update({"api-key": key} if azure else {"Authorization": f"Bearer {key}"})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": message}]
    body = {"temperature": 0.2, "max_tokens": 1200}
    if tk is not None:
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                           "parameters": t["input_schema"]}} for t in tk.specs()]
    if os.environ.get("WEPA_AI_MODEL"):
        body["model"] = os.environ["WEPA_AI_MODEL"]
    for _ in range(MAX_TOOL_ROUNDS + 1):
        r = requests.post(url, json={**body, "messages": messages}, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            raise AIError(f"HTTP {r.status_code}: {r.text[:160]}")
        msg = r.json()["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls or tk is None:
            return msg.get("content") or ""
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
        for c in calls:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": tk.run(c["function"]["name"], args)})
    raise AIError("the model kept looking things up without answering")


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


def _copilot_tools(tk):
    """One Copilot SDK tool per toolkit tool, with a pydantic model built from its JSON schema."""
    from copilot import ToolSet, define_tool
    from pydantic import Field, create_model

    types = {"string": str, "integer": int, "number": float}
    tools, allowed = [], ToolSet()
    for spec in tk.specs():
        props = spec["input_schema"].get("properties", {})
        fields = {k: (types.get(v.get("type"), str) | None,
                      Field(None, description=v.get("description", "") +
                            (f" One of: {', '.join(v['enum'])}." if v.get("enum") else "")))
                  for k, v in props.items()}
        model = create_model(f"Args_{spec['name']}", **fields)

        def handler(params, inv, _name=spec["name"]):      # no annotations (postponed evaluation)
            return tk.run(_name, params.model_dump(exclude_none=True))

        tools.append(define_tool(spec["name"], description=spec["description"], handler=handler,
                                 params_type=model, skip_permission=True))
        allowed.add_custom(spec["name"])
    return tools, allowed


async def _copilot_async(message: str, tk, timeout: float) -> str:
    from copilot import ToolSet

    client = await _get_client()
    tools, allowed = _copilot_tools(tk) if tk is not None else ([], ToolSet())
    kwargs = dict(on_permission_request=_deny, tools=tools, available_tools=allowed,
                  system_message={"mode": "replace", "content": SYSTEM_PROMPT}, streaming=False)
    if os.environ.get("WEPA_AI_MODEL"):
        kwargs["model"] = os.environ["WEPA_AI_MODEL"]
    if os.environ.get("WEPA_AI_API_KEY") and os.environ.get("WEPA_AI_BASE_URL"):
        kwargs["provider"] = {"type": os.environ.get("WEPA_AI_BYOK_TYPE", "openai"),
                              "base_url": os.environ["WEPA_AI_BASE_URL"], "api_key": os.environ["WEPA_AI_API_KEY"]}
    session = await client.create_session(**kwargs)
    try:
        event = await session.send_and_wait(message, timeout=max(timeout - 5, 10))
        data = getattr(event, "data", None)
        return getattr(data, "content", "") or ""
    finally:
        try:
            await session.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _copilot(message: str, tk, timeout: float) -> str:
    import importlib.util
    if importlib.util.find_spec("copilot") is None:
        raise AIError("the github-copilot-sdk package isn't installed")
    fut = asyncio.run_coroutine_threadsafe(_copilot_async(message, tk, timeout), _event_loop())
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
                     f"{durations.days(N.history_days(ds))} recorded.")
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
            lines.append(f"Availability last {days} day(s): {a.value:.1f}% ({durations.hours(a.extra.get('down_h', 0))} "
                         "of printer downtime).")
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


# --- safe display -----------------------------------------------------------------------------------

_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)")
_MD_REF = re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.M)
_HTML = re.compile(r"<[^>]{1,200}>")


def safe_markdown(text: str) -> str:
    """AI text for display: keep bold, italics and lists, drop images, links and HTML. Model output can
    echo text that came from outside (printer messages on Wepa's page), so nothing in it may load a
    resource or send someone to another site."""
    t = _MD_IMAGE.sub(lambda m: m.group(1), text or "")
    t = _MD_LINK.sub(lambda m: m.group(1), t)
    t = _MD_REF.sub("", t)
    return _HTML.sub("", t)
