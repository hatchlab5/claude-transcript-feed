# claude-transcript-feed

**A live, flicker-free web view of a Claude Code agent — streamed from its own JSONL
transcript, not from a screenshot of the terminal.**

Point it at a Claude Code session transcript (`*.jsonl`). It parses the append-only
record into a stream of discrete events — prompts, reasoning, replies, tool calls and
their output — and serves a zero-dependency page that watches the work happen. Every
event has a stable id and never changes, so the page only ever *appends*: it flows, it
never flickers.

Pure Python standard library. Pure vanilla JS. No build step, no framework, no daemon
you have to trust with a shell.

## Relay the event log — don't bridge a terminal

The obvious way to "show what an agent is doing" is to pipe a terminal to a browser: a
PTY bridge, or a screenshot of `tmux capture-pane` on a timer. Both are the wrong shape.

- **A terminal is a *surface*, not a *stream*.** `capture-pane` re-reads a scrolling
  screen, so the *same* line keeps being re-observed with different neighbours, text
  re-wraps as the pane moves, a running tool re-renders with a growing `· 12s` suffix,
  and every poll has to re-diff the whole picture. You cannot make that flow — it is a
  photograph of a moving thing.
- **A PTY bridge is a new listener and a new auth surface** wired straight into a shell,
  to show a pane full of furniture.

Claude Code already writes exactly the right source: an **append-only JSONL transcript**,
one record per message, each with a real ISO-8601 timestamp and content split into `text`
and `tool_use` blocks. That is a genuine event log. An event appears exactly once, never
changes, and arrives in order — so "chunks that flow up" stops being an effect to
simulate and becomes what the data already *is*.

This tool relays that log. Nothing more.

## The transcript shape (public Claude Code format)

Each line of a session `.jsonl` is one JSON object. The fields this tool reads:

- `type` — `"assistant"`, `"user"`, or `"queue-operation"` (input queued mid-turn).
- `timestamp` — ISO-8601, UTC (`...Z`). Used verbatim as the event's stable ordering key.
- `message.content` — a string, or a list of blocks. Block types read:
  - `text` — assistant prose or a user prompt.
  - `thinking` — a reasoning block. Its text is usually redacted (empty + signed), so
    reasoning is *counted*, not shown: we can't say what it thought, only that it did.
  - `tool_use` — `{name, input, id}`. A call.
  - `tool_result` — `{tool_use_id, content}`. The answer to a call, joined back to it by id.

A `user` record whose content is a `tool_result` is a machine answering, not a person
asking — so it is never treated as a prompt.

## Install

```bash
pip install -e .
# or run straight from the source tree with no install:
python -m transcript_feed --help
```

## Use

Serve a live page (regenerates the feed from the transcript on every request):

```bash
claude-transcript-feed --serve /path/to/session.jsonl
# open http://127.0.0.1:8000
```

Print one JSON snapshot (pipe it wherever you like):

```bash
claude-transcript-feed --once /path/to/session.jsonl | jq .state
```

Follow a transcript and keep a JSON file up to date, to wire into your own static host:

```bash
claude-transcript-feed --watch /path/to/session.jsonl --out ./public/feed.json --every 2
```

Options: `--label NAME` (header label; defaults to the transcript's filename),
`--host`, `--port`, `--max-events` (scrollback depth, default 250).

Finding your transcript: Claude Code writes one `.jsonl` per session under your Claude
Code projects directory. Pass the path of the session you want to watch.

## Two numbers that look alike and are not

- **`MAX_TEXT` is a *safety ceiling*, not a *display budget*.** It exists only so a pasted
  megabyte can't bloat the JSON that gets re-fetched every couple of seconds. It is sized
  well above the longest real paragraph, so it never fires on genuine output. **The
  producer must not chop for *display*** — a producer-side chop is unrecoverable, because
  the bytes never reach the page and no amount of scrolling gets them back. Deciding *how
  much of a paragraph is on screen* is the *page's* job; it already scrolls a 250-event
  buffer. The relay's job is to carry the content, not to delete it.
- The event window (`--max-events`) is **scrollback**, not "what fits on screen." The
  page shows the tail and lets you scroll up into the rest.

## Why it doesn't flicker

Every event carries a stable id (`<timestamp>#<index>`). The page keeps an
`id → node` map and **only appends new ids**. A node already on screen is never
re-rendered, re-classed or re-ordered. The entrance animation fires once, as the node
arrives, and never again. That — not a CSS trick — is what makes it flow.

## What this is not

- Not a PTY bridge. It never touches a shell.
- Not a terminal screenshotter. It reads structured events, not pixels.
- Not an orchestrator. It watches one transcript file, read-only.
- Not a security boundary. It binds loopback by default; run with `--host 0.0.0.0` and the served content is only as trustworthy as whoever can write the transcript, since the parser will read and surface the contents of any file a `<output-file>` tag names.

## Prior art

The parsing-and-append-only-render approach has been running in production as an internal
live dashboard for months; this is a clean, dependency-free extraction of that core idea.

## License

MIT — see [LICENSE](LICENSE).
