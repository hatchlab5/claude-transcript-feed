"""claude-transcript-feed — relay a Claude Code agent's JSONL transcript to a live page.

Public API:
    parse_transcript(path)  -> dict with "events", "prompt", "phase", "thinks"
    FeedComposer            -> wraps parse output with derived state + "since" tracking
"""
from .parser import parse_transcript
from .feed import FeedComposer, transcript_state

__all__ = ["parse_transcript", "FeedComposer", "transcript_state"]
__version__ = "0.1.0"
