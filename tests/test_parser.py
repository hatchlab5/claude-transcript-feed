"""Tests for the transcript parser — the core algorithm. Standard library only."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transcript_feed.parser import parse_transcript, MAX_TEXT, MAX_PARAS, ELLIPSIS
from transcript_feed.feed import FeedComposer, transcript_state, seed_since


TS = "2026-09-28T12:00:00.000Z"


def assistant(blocks, ts=TS):
    return {"type": "assistant", "timestamp": ts, "message": {"content": blocks}}


def user_text(text, ts=TS):
    return {"type": "user", "timestamp": ts, "message": {"content": text}}


def write_jsonl(records):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return Path(path)


class ParseTest(unittest.TestCase):
    def tearDown(self):
        for p in getattr(self, "_paths", []):
            try:
                p.unlink()
            except OSError:
                pass

    def parse(self, records, **kw):
        p = write_jsonl(records)
        self._paths = getattr(self, "_paths", []) + [p]
        return parse_transcript(p, **kw)

    def kinds(self, events):
        return [e["kind"] for e in events]

    # ── classification ────────────────────────────────────────────────────────────────
    def test_text_and_tool_use_classified(self):
        out = self.parse([
            user_text("please list the files"),
            assistant([
                {"type": "text", "text": "On it."},
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": "ls -la", "description": "list files"}},
            ]),
        ])
        ks = self.kinds(out["events"])
        self.assertEqual(ks, ["prompt", "say", "action"])
        action = out["events"][-1]
        self.assertEqual(action["text"], "Bash")
        self.assertEqual(action["cmd"], "ls -la")
        self.assertEqual(action["detail"], "list files")

    def test_prompt_extracted_and_reminders_filtered(self):
        out = self.parse([
            user_text("<system-reminder>internal envelope</system-reminder>"),
            user_text("the real question"),
        ])
        prompts = [e for e in out["events"] if e["kind"] == "prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["text"], "the real question")
        self.assertEqual(out["prompt"], "the real question")

    def test_prompt_strips_trailing_reminder(self):
        out = self.parse([user_text("do the thing<system-reminder>noise</system-reminder>")])
        self.assertEqual(out["prompt"], "do the thing")

    def test_tool_result_as_user_is_not_a_prompt(self):
        out = self.parse([
            {"type": "user", "timestamp": TS, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "output"}]}},
        ])
        self.assertEqual([e for e in out["events"] if e["kind"] == "prompt"], [])

    # ── tool_result joins its call ──────────────────────────────────────────────────────
    def test_result_attaches_to_its_call(self):
        out = self.parse([
            assistant([{"type": "tool_use", "id": "t1", "name": "Bash",
                        "input": {"command": "echo hi"}}]),
            {"type": "user", "timestamp": TS, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "hi"}]}},
        ])
        # exactly one action event, carrying its output — no orphan result line
        actions = [e for e in out["events"] if e["kind"] == "action"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["out"], "hi")
        self.assertNotIn("result", self.kinds(out["events"]))

    def test_orphan_result_becomes_its_own_line(self):
        out = self.parse([
            {"type": "user", "timestamp": TS, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "unknown", "content": "stray"}]}},
        ])
        self.assertIn("result", self.kinds(out["events"]))

    # ── background agent ────────────────────────────────────────────────────────────────
    def test_agent_launch_receipt_suppressed_and_notification_attaches(self):
        out_file = write_jsonl([])  # reuse tmp path as an output file
        out_file.write_text("the finished agent output")
        self._paths = getattr(self, "_paths", []) + [out_file]
        out = self.parse([
            assistant([{"type": "tool_use", "id": "a1", "name": "Task",
                        "input": {"subagent_type": "explorer", "description": "scan repo"}}]),
            # the launch receipt must NOT overwrite the agent block
            {"type": "user", "timestamp": TS, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "a1",
                 "content": "Async agent launched successfully (internal metadata)"}]}},
            # the real report arrives as a task-notification pointing at a file
            user_text("<task-notification><tool-use-id>a1</tool-use-id>"
                      "<output-file>%s</output-file></task-notification>" % out_file),
        ])
        agents = [e for e in out["events"] if e["kind"] == "agent"]
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["text"], "explorer")
        self.assertEqual(agents[0]["out"], "the finished agent output")

    # ── caps ────────────────────────────────────────────────────────────────────────────
    def test_max_text_truncates_with_ellipsis(self):
        big = "word " * 2000  # ~10000 chars
        out = self.parse([assistant([{"type": "text", "text": big}])])
        say = [e for e in out["events"] if e["kind"] == "say"][0]
        self.assertLessEqual(len(say["text"]), MAX_TEXT + 1)
        self.assertTrue(say["text"].endswith(ELLIPSIS))

    def test_max_paras_emits_overflow_marker_never_silent(self):
        paras = "\n\n".join("para %d" % i for i in range(MAX_PARAS + 5))
        out = self.parse([assistant([{"type": "text", "text": paras}])])
        says = [e for e in out["events"] if e["kind"] == "say"]
        self.assertEqual(len(says), MAX_PARAS)
        overflow = [e for e in out["events"]
                    if e["kind"] == "result" and "not carried" in e["text"]]
        self.assertEqual(len(overflow), 1)
        self.assertIn("5 more paragraphs", overflow[0]["text"])

    # ── stable ids ──────────────────────────────────────────────────────────────────────
    def test_ids_stable_and_unique(self):
        records = [user_text("q"), assistant([{"type": "text", "text": "a"}])]
        p = write_jsonl(records)
        self._paths = getattr(self, "_paths", []) + [p]
        first = parse_transcript(p)["events"]
        second = parse_transcript(p)["events"]
        self.assertEqual([e["id"] for e in first], [e["id"] for e in second])
        self.assertEqual(len({e["id"] for e in first}), len(first))

    # ── thinking count ──────────────────────────────────────────────────────────────────
    def test_thinking_counted_even_when_redacted(self):
        out = self.parse([
            user_text("go"),
            assistant([
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "done"},
            ]),
        ])
        self.assertEqual(out["thinks"], 2)
        # last block was text, so phase is not 'reasoning'
        self.assertNotEqual(out["phase"], "reasoning")

    def test_thinks_reset_on_new_prompt(self):
        out = self.parse([
            user_text("first"),
            assistant([{"type": "thinking", "thinking": "", "signature": "s"}]),
            user_text("second"),
        ])
        self.assertEqual(out["thinks"], 0)

    # ── queue-operation ─────────────────────────────────────────────────────────────────
    def test_queue_operation_becomes_prompt(self):
        out = self.parse([
            {"type": "queue-operation", "operation": "enqueue",
             "content": "typed while working", "timestamp": TS},
        ])
        prompts = [e for e in out["events"] if e["kind"] == "prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["text"], "typed while working")

    # ── robustness ──────────────────────────────────────────────────────────────────────
    def test_missing_file_is_empty_not_error(self):
        out = parse_transcript(Path("/no/such/transcript.jsonl"))
        self.assertEqual(out["events"], [])
        self.assertEqual(out["prompt"], "")

    def test_malformed_line_skipped(self):
        p = write_jsonl([])
        p.write_text('{"broken\n' + json.dumps(user_text("good")) + "\n")
        self._paths = getattr(self, "_paths", []) + [p]
        out = parse_transcript(p)
        self.assertEqual(out["prompt"], "good")


class FeedTest(unittest.TestCase):
    def test_state_gone_for_missing(self):
        self.assertEqual(transcript_state("/no/such/file.jsonl"), "gone")

    def test_state_working_when_fresh(self):
        p = write_jsonl([user_text("hi")])
        try:
            self.assertEqual(transcript_state(p), "working")
        finally:
            p.unlink()

    def test_state_idle_when_old(self):
        p = write_jsonl([user_text("hi")])
        try:
            old = time.time() - 3600
            os.utime(p, (old, old))
            self.assertEqual(transcript_state(p), "idle")
        finally:
            p.unlink()

    def test_seed_since_working_prefers_last_prompt(self):
        now = 1_700_000_000.0
        events = [
            {"kind": "prompt", "ts": "2023-11-14T22:13:00Z"},
            {"kind": "say", "ts": "2023-11-14T22:14:00Z"},
        ]
        # it picks the last PROMPT (22:13), not the later say (22:14, which is past `now`)
        import calendar
        prompt_epoch = calendar.timegm(time.strptime("2023-11-14T22:13:00", "%Y-%m-%dT%H:%M:%S"))
        self.assertEqual(seed_since("working", events, now), prompt_epoch)

    def test_composer_builds_payload(self):
        p = write_jsonl([user_text("do X"), assistant([{"type": "text", "text": "ok"}])])
        try:
            payload = FeedComposer(p, label="demo").build()
            self.assertEqual(payload["label"], "demo")
            self.assertIn("events", payload)
            self.assertEqual(payload["prompt"], "do X")
            self.assertIn(payload["state"], ("working", "idle", "gone"))
            self.assertIsInstance(payload["since"], float)
        finally:
            p.unlink()


if __name__ == "__main__":
    unittest.main()
