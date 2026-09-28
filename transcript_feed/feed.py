"""Compose a full feed payload: parsed events plus derived, honest state.

State is inferred from the transcript alone. There is no terminal, no PTY and no session
manager here — so "working vs idle" is a coarse liveness read off the file's modification
time, and it says so. It is deliberately conservative: it never claims more than the file
can support.
"""
import threading
import time
from pathlib import Path

from .parser import parse_transcript, _epoch

# How recently the transcript must have been written to read as "working". A completed
# turn appends and then goes quiet, so this can read "working" for a few seconds after the
# agent actually stopped — an accepted, documented coarseness, not a lie about the source.
LIVE_WINDOW = 10.0


def transcript_state(path):
    """'working' | 'idle' | 'gone' — inferred from the transcript's mtime."""
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return "gone"
    return "working" if (time.time() - mtime) < LIVE_WINDOW else "idle"


def seed_since(state, events, now):
    """When this state STARTED, seeded from evidence when there is no stored clock.

    A convenient `now` would be a lie that looks fine after a restart. So:
      • working -> the last PROMPT (when this turn was handed over).
      • idle    -> the last thing the agent DID (when it stopped).
    With no usable evidence, `now` is the honest floor.
    """
    if state == "gone" or not events:
        return now
    want = "prompt" if state == "working" else None
    for ev in reversed(events):
        if want and ev.get("kind") != want:
            continue
        t = _epoch(ev.get("ts"))
        if 0 < t <= now + 1:
            return t
    for ev in reversed(events):            # no prompt found: the last event will do
        t = _epoch(ev.get("ts"))
        if 0 < t <= now + 1:
            return t
    return now


class FeedComposer:
    """Builds the JSON payload the page polls. Tracks state transitions in memory, so the
    'since' clock survives across many builds within one process (e.g. a serving loop)."""

    def __init__(self, path, label=None, max_events=None):
        self.path = Path(path)
        self.label = label or self.path.stem or "agent"
        self.max_events = max_events
        self._lock = threading.Lock()
        self._prev_state = None
        self._since = None
        self._cache_key = None      # (st_mtime, st_size) the cached parse was built from
        self._cache_parsed = None   # last parse_transcript() result, reused until the file moves

    def _track_since(self, state, events, now):
        # First observation: seed from evidence. A real transition: it started when we saw
        # it. Unchanged: keep the clock running.
        if self._prev_state is None:
            self._since = seed_since(state, events, now)
        elif self._prev_state != state:
            self._since = now
        self._prev_state = state
        return self._since

    def _parsed(self):
        """Parse the transcript, but reuse the last result while the file is unchanged.

        Every GET calls build(), and re-parsing the whole transcript each time is pure waste
        when nothing was appended between polls. Key a cache on the file's (mtime, size) and
        rebuild only when one of them moves. Called under self._lock, so it is safe for the
        threaded server. NOTE: only the PARSE is cached — state/`since`/`ts` are still derived
        fresh on every build (they track the wall clock, not the file), so liveness is never
        served stale even when the transcript sits quiet.
        """
        try:
            st = self.path.stat()
            key = (st.st_mtime, st.st_size)
        except OSError:
            key = None
        if self._cache_parsed is None or key != self._cache_key:
            kwargs = {} if self.max_events is None else {"max_events": self.max_events}
            self._cache_parsed = parse_transcript(self.path, **kwargs)
            self._cache_key = key
        return self._cache_parsed

    def build(self):
        with self._lock:
            now = time.time()
            parsed = self._parsed()
            state = transcript_state(self.path)
            since = self._track_since(state, parsed["events"], now)
            return {
                "label": self.label,
                "ts": now,
                "state": state,
                "since": since,
                "phase": parsed["phase"],
                "thinks": parsed["thinks"],
                "prompt": parsed["prompt"],
                "events": parsed["events"],
            }
