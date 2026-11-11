import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sessions


class SessionTests(unittest.TestCase):
    def test_sliding_idle_boundary_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conversations.json"
            now = [1000]
            state = sessions.ConversationSessions(path, clock=lambda: now[0])
            base = "telegram:42"
            self.assertEqual(state.select(base), (base, False))
            now[0] += sessions.IDLE_SECONDS - 1
            self.assertEqual(state.select(base), (base, False))
            # Sliding inactivity, not time since conversation creation.
            now[0] += sessions.IDLE_SECONDS - 1
            state = sessions.ConversationSessions(path, clock=lambda: now[0])
            self.assertEqual(state.select(base), (base, False))
            now[0] += sessions.IDLE_SECONDS
            key, rotated = state.select(base)
            self.assertTrue(rotated)
            self.assertTrue(key.startswith(base + ":conversation:"))
            reloaded = sessions.ConversationSessions(path, clock=lambda: now[0])
            self.assertEqual(reloaded.select(base), (key, False))
            now[0] += sessions.IDLE_SECONDS + 1
            next_key, rotated = reloaded.select(base)
            self.assertTrue(rotated)
            self.assertNotEqual(next_key, key)

    def test_chats_topics_are_independent_and_clock_rollback_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000]
            state = sessions.ConversationSessions(Path(directory) / "state.json", clock=lambda: now[0])
            first = "telegram:42:topic:1"
            second = "telegram:42:topic:2"
            state.select(first)
            now[0] += 100
            state.select(second)
            now[0] = 1000 + sessions.IDLE_SECONDS
            self.assertTrue(state.select(first)[1])
            self.assertEqual(state.select(second), (second, False))
            now[0] -= 1000
            self.assertEqual(state.select(second), (second, False))

    def test_corrupt_state_fails_instead_of_silent_session_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for value in ([], {"chat": {}}, {"chat": {"key": "key", "last_received": True}},
                          {"chat": {"key": "key", "last_received": float("nan")}}):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(sessions.SessionStateError):
                    sessions.ConversationSessions(path)

    def test_save_failure_rolls_back_and_state_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conversations.json"
            state = sessions.ConversationSessions(path, clock=lambda: 1000)
            state.select("telegram:42")
            before = path.read_bytes()
            with patch.object(sessions.os, "replace", side_effect=OSError("disk")):
                with self.assertRaises(sessions.SessionStateError):
                    state.select("telegram:999")
            self.assertNotIn("telegram:999", state.conversations)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
