"""Turn a Claude Code JSONL transcript into a stream of discrete, stable-id events.

This is the whole reason for using the transcript instead of a terminal capture: each
record is read ONCE and never re-interpreted. An event appears exactly once, never
changes, and arrives in order — so the page can append and never re-render.

The transcript carries the public Claude Code shape (see README). Everything here is
derived from that format alone; nothing reaches outside the file it is handed, except
the optional background-agent output file a task-notification names.
"""
import calendar
import json
import re
import time
from pathlib import Path

# ── Caps ────────────────────────────────────────────────────────────────────────────
MAX_EVENTS = 250      # SCROLLBACK depth carried in the feed — not "what fits on screen".
                      # The page shows the tail and lets you scroll up into the rest.

MAX_TEXT = 4000       # A SAFETY CEILING, NOT A DISPLAY BUDGET. Its only job is to stop a
                      # pasted megabyte from bloating the JSON re-fetched every poll. It
                      # sits well above the longest real paragraph, so it never fires on
                      # genuine output. The PRODUCER must not chop for display: a
                      # producer-side chop is unrecoverable (the bytes never reach the
                      # page). How much of a paragraph is *shown* is the page's job.

MAX_PROMPT = 4000     # A human's ask, kept in full for the pinned/expandable header.
MAX_PARAS = 10        # Paragraphs carried from a single reply. A sectioned answer is many
                      # beats — but a 40-section monster must not flood the window. When it
                      # overflows we say so; we never drop the tail silently.
MAX_DETAIL = 90       # A tool call's one-line subject.
MAX_RESULT = 150      # An ORPHANED result (no call to attach to) — one line, never a dump.
MAX_OUT = 2400        # Output kept WITH its command, for the dropdown.
MAX_OUT_LINES = 40    # ...and a line ceiling on that same output.

ELLIPSIS = "\u2026"

# The transcript carries MARKDOWN (that is what renders in a terminal). A page is not a
# markdown renderer, so raw ** and ` would leak onto the surface as literal characters.
_MD = [
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", re.S), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"^#{1,6}\s+", re.M), ""),
    (re.compile(r"^[-*]\s+", re.M), ""),
]

# All of these arrive typed "user" and none of them is a person asking for something.
# They are the public harness envelopes a transcript interleaves with real prompts.
_NOT_A_PROMPT = (
    "<system-reminder>", "<local-command-caveat>", "<command-name>", "<command-message>",
    "<command-args>", "Caveat: The messages below", "Base directory for this skill:",
    "[Request interrupted", "Result of calling the",
    "<task-notification>",                 # a background agent finishing
    "[Image:",                             # placeholder for a pasted image
    "This session is being continued",     # a compaction handoff
    "<bash-input>", "<bash-stdout>", "<bash-stderr>",
)


def demarkdown(t):
    for rx, rep in _MD:
        t = rx.sub(rep, t)
    return t.strip()


def _clip(t, limit):
    """Word-safe truncation with a visible ellipsis, so a cut is never silent."""
    if len(t) <= limit:
        return t
    return t[:limit].rsplit(" ", 1)[0] + ELLIPSIS


def tool_detail(name, inp):
    """One honest line describing what a tool call is actually touching."""
    inp = inp or {}
    if name == "Bash":
        return (inp.get("description") or inp.get("command") or "")[:MAX_DETAIL]
    for k in ("file_path", "path", "pattern", "query", "url", "description", "skill", "prompt"):
        if inp.get(k):
            return str(inp[k])[:MAX_DETAIL]
    return ""


def prompt_from(d):
    """The last thing a HUMAN actually told the agent.

    Everything in a transcript is typed "user" — tool results, hook output, skill bodies,
    pasted reminders — so a prompt is recognised by what it is NOT."""
    if d.get("type") != "user":
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, str):
        t = c
    elif isinstance(c, list):
        # A tool result is a machine answering, not a person asking.
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None
        t = " ".join(x.get("text", "") for x in c
                     if isinstance(x, dict) and x.get("type") == "text")
    else:
        return None
    t = (t or "").strip()
    if not t or t.startswith(_NOT_A_PROMPT):
        return None
    # A real prompt often has a reminder block stapled to the end; keep the human half.
    cut = t.find("<system-reminder>")
    if cut > 0:
        t = t[:cut].strip()
    # A screenshot pasted WITH a question keeps the question, drops the placeholder.
    t = re.sub(r"^\[Image:[^\]]*\]\s*", "", t).strip()
    if not t or t.startswith(_NOT_A_PROMPT):
        return None
    t = " ".join(t.split())
    return t[:MAX_PROMPT] or None


def _epoch(ts):
    """ISO-8601 stamp -> epoch seconds. It ends in Z, so it is UTC: timegm, never mktime
    (mktime reads a struct as LOCAL time — a silent multi-hour error)."""
    try:
        return float(calendar.timegm(time.strptime((ts or "")[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return 0.0


def parse_transcript(path, max_events=MAX_EVENTS):
    """Read a transcript into events (oldest first) plus the turn's derived stats.

    Returns {"events": [...], "prompt": str, "phase": str, "thinks": int}.
    `events` is empty and stats are neutral if the file is missing or unreadable.
    """
    path = Path(path)
    events = []
    calls = {}                 # tool_use id -> its event, so a result finds its call
    last_prompt = ""
    thinks = 0                 # reasoning blocks in the CURRENT turn (reset on a new prompt)
    phase = ""                 # "reasoning" | "tools" | "" — what the turn did LAST

    try:
        fh = path.open("r", errors="replace")
    except OSError:
        return {"events": [], "prompt": "", "phase": "", "thinks": 0}

    with fh:
        for ln in fh:
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            typ = d.get("type")

            # Input queued while the agent is mid-turn is its OWN record type — not
            # type:"user" — so anything that only looks at assistant/user misses the most
            # common way a human talks to a working agent.
            if typ == "queue-operation":
                if (d.get("operation") or "") == "enqueue":
                    q = " ".join(str(d.get("content") or "").split())
                    if q:
                        # Deliberately does NOT reset thinks/phase (unlike a real prompt):
                        # queued input arrives MID-turn, so the current turn's reasoning is
                        # still in progress — zeroing it here would lie about the live turn.
                        last_prompt = q[:MAX_PROMPT]
                        events.append({"kind": "prompt", "text": _clip(q, MAX_TEXT),
                                       "ts": d.get("timestamp") or ""})
                continue

            if typ not in ("assistant", "user"):
                continue

            # A task-notification is a background agent REPORTING BACK. It is typed "user"
            # (so the prompt filter drops it) but carries the launch's tool-use id and the
            # file its output went to — which is what turns a launch into finished work.
            if typ == "user":
                raw_c = (d.get("message") or {}).get("content")
                flat = raw_c if isinstance(raw_c, str) else json.dumps(raw_c)
                if "<task-notification>" in flat:
                    mid = re.search(r"<tool-use-id>([^<]+)</tool-use-id>", flat)
                    mf = re.search(r"<output-file>([^<]+)</output-file>", flat)
                    owner = calls.get(mid.group(1)) if mid else None
                    if owner is not None:
                        body = ""
                        if mf:
                            try:
                                body = Path(mf.group(1)).read_text(errors="replace")[-MAX_OUT:]
                            except OSError:
                                body = ""
                        owner["out"] = body.strip() or "returned"

            pr = prompt_from(d)
            if pr:
                last_prompt = pr
                thinks = 0          # a new ask is a new turn: the count is per-turn
                phase = ""
                events.append({"kind": "prompt", "text": _clip(pr, MAX_TEXT),
                               "ts": d.get("timestamp") or ""})

            msg = d.get("message") or {}
            ts = d.get("timestamp") or ""
            blocks = msg.get("content")
            if not isinstance(blocks, list):
                continue

            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")

                # What came back. A call without its result is half the story.
                if bt == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                    c = (c or "").strip()
                    if not c:
                        continue
                    body = c[:MAX_OUT]
                    if len(c) > MAX_OUT:
                        body = body.rsplit("\n", 1)[0] + "\n" + ELLIPSIS
                    lines = body.split("\n")
                    if len(lines) > MAX_OUT_LINES:
                        body = "\n".join(lines[:MAX_OUT_LINES]) + "\n" + ELLIPSIS
                    owner = calls.get(b.get("tool_use_id"))
                    # The LAUNCH RECEIPT is not the work. Firing a background agent returns
                    # an internal-metadata blob; attaching it would put ids on screen and
                    # make a just-launched agent look finished. Skip it — the real output
                    # arrives later via the task-notification.
                    if owner is not None and owner.get("kind") == "agent" \
                            and ("Async agent launched" in c or "internal metadata" in c):
                        continue
                    if owner is not None:
                        owner["out"] = body
                        continue
                    one = " ".join(c.split())
                    if len(one) > MAX_RESULT:
                        one = one[:MAX_RESULT].rsplit(" ", 1)[0] + ELLIPSIS
                    events.append({"kind": "result", "text": one, "ts": ts})
                    continue

                if typ != "assistant":
                    continue

                if bt == "thinking":
                    # Counted BEFORE the empty check: the block being present is the fact,
                    # and its text being redacted does not make the reasoning not happen.
                    thinks += 1
                    phase = "reasoning"
                    t = (b.get("thinking") or "").strip()
                    if not t:
                        continue
                    t = _clip(demarkdown(t.split("\n\n")[0]), MAX_TEXT)
                    events.append({"kind": "think", "text": t, "ts": ts})
                    continue

                if bt == "text":
                    # A plain reply is the answer — neither reasoning nor a tool run — so it
                    # clears the phase. A later tool_use in the same turn overrides this to
                    # "tools", keeping "what the turn did LAST" honest.
                    phase = ""
                    raw = (b.get("text") or "").strip()
                    if not raw:
                        continue
                    # The WHOLE reply, not just its first paragraph. A sectioned answer is
                    # many beats; each paragraph is its own event so the page can highlight
                    # the whole latest reply as one block.
                    paras = [x.strip() for x in raw.split("\n\n") if x.strip()]
                    for para in paras[:MAX_PARAS]:
                        t = demarkdown(para)
                        if not t:
                            continue
                        events.append({"kind": "say", "text": _clip(t, MAX_TEXT), "ts": ts})
                    # NO SILENT LOSS. Over the paragraph cap, say so — a complete final line
                    # otherwise reads as a finished answer, the same failure as an unseen cut.
                    extra = len(paras) - MAX_PARAS
                    if extra > 0:
                        events.append({
                            "kind": "result", "ts": ts,
                            "text": "%s more paragraph%s in this reply, not carried"
                                    % (extra, "" if extra == 1 else "s"),
                        })
                    continue

                if bt == "tool_use":
                    phase = "tools"          # doing, not reasoning
                    name = b.get("name") or "tool"
                    inp = b.get("input") or {}
                    # A background agent is not a plain tool call: its own kind, its own
                    # colour, and it stays "running" until its notification arrives.
                    kind = "agent" if name in ("Task", "Agent") else "action"
                    label = str(inp.get("subagent_type") or "agent") if kind == "agent" else name
                    ev = {
                        "kind": kind, "text": label, "ts": ts,
                        "detail": (str(inp.get("description") or "") if kind == "agent"
                                   else tool_detail(name, inp)),
                    }
                    # The command ITSELF, not only its description: the description is what
                    # the agent MEANT to run; the command is what ran.
                    if name == "Bash":
                        cmd = inp.get("command") or ""
                        if cmd:
                            ev["cmd"] = cmd[:MAX_OUT]
                    events.append(ev)
                    if b.get("id"):
                        calls[b["id"]] = ev

    # Stable identity, so the page appends and never re-renders what it already shows.
    for i, e in enumerate(events):
        e["id"] = "%s#%d" % (e.get("ts") or "", i)

    return {
        "events": events[-max_events:],
        "prompt": last_prompt,
        "phase": phase,
        "thinks": thinks,
    }
