import importlib.util
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("telegram_worker", Path(__file__).with_name("worker.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


def update(number=10, user=42, chat_type="private", text="Oi"):
    return {"update_id": number, "message": {"message_id": 7,
            "from": {"id": user, "is_bot": False},
            "chat": {"id": 42, "type": chat_type}, "text": text}}


class WorkerTests(unittest.TestCase):
    def test_deny_by_default(self):
        with self.assertRaises(w.WorkerError):
            w.parse_ids("", required=True)
        with self.assertRaises(w.WorkerError):
            w.parse_ids("abc")
        self.assertEqual(w.parse_ids("42, -100,42"), {42, -100})
        self.assertIsNone(w.wakeup_params(update(user=9), {42}, set()))
        self.assertIsNone(w.wakeup_params(update(chat_type="group"), {42}, set()))
        self.assertIsNone(w.wakeup_params(update(text=" "), {42}, set()))
        event = update()
        event["message"]["from"]["is_bot"] = True
        self.assertIsNone(w.wakeup_params(event, {42}, set()))

    def test_explicit_allow_all_users_keeps_chat_and_bot_filters(self):
        import os
        with patch.dict(os.environ, {"TELEGRAM_ALLOW_ALL_USERS": "true", "TELEGRAM_ALLOWED_USER_IDS": ""}):
            users = w.configured_users()
        self.assertIsNone(users)
        self.assertIsNotNone(w.wakeup_params(update(user=999), users, set()))
        self.assertIsNone(w.wakeup_params(update(chat_type="group"), users, set()))
        self.assertIsNone(w.wakeup_params(update(), users, {999}))
        event = update()
        event["message"]["from"]["is_bot"] = True
        self.assertIsNone(w.wakeup_params(event, users, set()))
        event["message"]["from"] = {}
        self.assertIsNone(w.wakeup_params(event, users, set()))
        with patch.dict(os.environ, {"TELEGRAM_ALLOW_ALL_USERS": "false", "TELEGRAM_ALLOWED_USER_IDS": "42"}):
            self.assertEqual(w.configured_users(), {42})
        with patch.dict(os.environ, {"TELEGRAM_ALLOW_ALL_USERS": "", "TELEGRAM_ALLOWED_USER_IDS": ""}):
            with self.assertRaises(w.WorkerError):
                w.configured_users()

    def test_event_routing_and_topics(self):
        event = update(chat_type="supergroup")
        event["message"]["chat"]["id"] = -100
        event["message"]["message_thread_id"] = 3
        params = w.wakeup_params(event, {42}, {-100})
        self.assertEqual(params["session_key"], "telegram:-100:topic:3")
        self.assertEqual(params["event_id"], "telegram:10")
        self.assertEqual(params["context"]["message_id"], 7)
        self.assertEqual(params["context"]["user_id"], 42)
        self.assertEqual(params["prompt"], "Oi")
        self.assertIsNone(w.wakeup_params(event, {42}, {999}))

    def test_typing_routing_and_short_deadline(self):
        from unittest.mock import Mock
        api = Mock()
        w.send_typing(api, -100, 3)
        api.call.assert_called_once_with("sendChatAction", {
            "chat_id": -100, "action": "typing", "message_thread_id": 3}, timeout=3)
        api.reset_mock()
        w.send_typing(api, 42)
        api.call.assert_called_once_with("sendChatAction", {"chat_id": 42, "action": "typing"}, timeout=3)

    def test_typing_cli(self):
        with patch.object(sys, "argv", ["worker.py", "typing", "--chat-id", "42", "--thread-id", "3"]):
            with patch.object(w, "token_value", return_value="123:secret"), patch.object(w, "send_typing") as send:
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(w.main(), 0)
                self.assertEqual(send.call_args.args[1:], (42, 3))

    def test_typing_failure_does_not_block_admission_or_cursor(self):
        stop = threading.Event()
        actions, admitted = [], []

        class API:
            polls = 0

            def call(self, method, params, **kwargs):
                if method == "sendChatAction":
                    actions.append(params)
                    raise w.APIError(429, 10)
                self.polls += 1
                if self.polls == 1:
                    return [update(10, user=999), update(11)]
                stop.set()
                return []

        class RPC:
            def wakeup(self, params):
                admitted.append(params)

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "cursor.json"
            w.run_worker(API(), RPC(), stop, state, {42}, set())
            self.assertEqual(w.load_offset(state), 12)
        self.assertEqual(len(actions), 1)  # No notification for unauthorized update.
        self.assertEqual(len(admitted), 1)
        self.assertEqual(admitted[0]["event_id"], "telegram:11")

    def test_worker_rotates_session_key_after_three_hours(self):
        stop = threading.Event()
        admitted = []

        class API:
            polls = 0

            def call(self, method, params, **kwargs):
                if method == "sendChatAction":
                    return True
                self.polls += 1
                if self.polls <= 2:
                    return [update(10 + self.polls)]
                stop.set()
                return []

        class RPC:
            def wakeup(self, params):
                admitted.append(params["session_key"])

        with tempfile.TemporaryDirectory() as directory:
            original = w.sessions.ConversationSessions
            clock = iter([1000, 11800]).__next__
            with patch.object(w.sessions, "ConversationSessions", side_effect=lambda path: original(path, clock=clock)):
                w.run_worker(API(), RPC(), stop, Path(directory) / "cursor.json", {42}, set())
        self.assertEqual(admitted[0], "telegram:42")
        self.assertTrue(admitted[1].startswith("telegram:42:conversation:"))

    def test_cursor_atomic_and_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "cursor.json"
            self.assertIsNone(w.load_offset(path))
            w.save_offset(path, 11)
            self.assertEqual(w.load_offset(path), 11)
            self.assertEqual(len(list(path.parent.iterdir())), 1)
            path.write_text('{"next_offset":true}', encoding="utf-8")
            with self.assertRaises(w.WorkerError):
                w.load_offset(path)

    def test_cursor_only_advances_after_ack(self):
        class API:
            def call(self, method, params, **kwargs):
                return True if method == "sendChatAction" else [update()]

        class RPC:
            def wakeup(self, params):
                raise w.WorkerError("ambiguous")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursor.json"
            with self.assertRaises(w.WorkerError):
                w.run_worker(API(), RPC(), threading.Event(), path, {42}, set())
            self.assertFalse(path.exists())

    def test_filtered_and_accepted_updates_advance_cursor(self):
        stop = threading.Event()
        calls, events = [], []

        class API:
            def call(self, method, params, **kwargs):
                if method == "sendChatAction":
                    return True
                calls.append(params.copy())
                if len(calls) == 1:
                    return [update(10, user=999), update(11)]
                stop.set()
                return []

        class RPC:
            def wakeup(self, params):
                events.append(params)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursor.json"
            w.run_worker(API(), RPC(), stop, path, {42}, set())
            self.assertEqual(w.load_offset(path), 12)
            self.assertEqual(calls[1]["offset"], 12)
            self.assertEqual(len(events), 1)

    def test_chunks_respect_utf16(self):
        text = "a" * 3999 + "\U0001f600" * 3000
        parts = list(w.chunks(text))
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 4000 for p in parts))

    def rpc(self):
        # Construct the synchronous side without starting a stdin reader.
        rpc = object.__new__(w.RPC)
        rpc.stop = threading.Event()
        rpc.frames = queue.Queue()
        rpc.target = io.BytesIO()
        rpc.limit = w.MAX_FRAME
        rpc.next_id = 0
        rpc.image_paths = False
        return rpc

    def test_initialize_and_wakeup(self):
        rpc = self.rpc()
        rpc.frames.put({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"version": "1", "max_frame_size": 4096}})
        rpc.initialize()
        self.assertEqual(rpc.limit, 4096)
        rpc.frames.put({"jsonrpc": "2.0", "id": "telegram-1", "result": {"status": "accepted"}})
        rpc.wakeup(w.wakeup_params(update(), {42}, set()))
        frames = [json.loads(line) for line in rpc.target.getvalue().splitlines()]
        self.assertEqual(frames[0]["id"], 1)
        self.assertEqual(frames[1]["method"], "wakeup")

    def test_protocol_rejection_limits_and_timeout(self):
        rpc = self.rpc()
        rpc.frames.put({"id": "telegram-1", "error": {"code": -1}})
        with self.assertRaises(w.WorkerError):
            rpc.wakeup({"prompt": "oi"})
        rpc.limit = 256
        with self.assertRaises(w.WorkerError):
            rpc.send({"text": "x" * 300})
        with self.assertRaises(w.WorkerError):
            rpc.receive(0.001)

    def test_stdin_eof_and_oversized_frames_stop_reader(self):
        for data in (b"", b"x" * (w.MAX_FRAME + 1)):
            stop = threading.Event()
            w.RPC(io.BytesIO(data), io.BytesIO(), stop)
            self.assertTrue(stop.wait(2))

    def test_api_failure_does_not_leak_token(self):
        api = w.Telegram("123:TOPSECRET")
        with patch.object(w.urllib.request, "urlopen", side_effect=OSError(api.base)):
            with self.assertRaises(w.WorkerError) as error:
                api.call("getUpdates", {})
        self.assertNotIn("TOPSECRET", str(error.exception))

    def test_conflict_fails_without_retry(self):
        class API:
            def call(self, *_):
                raise w.APIError(409)
        with self.assertRaises(w.APIError):
            w.run_worker(API(), None, threading.Event(), Path("unused"), {42}, set())

    def test_polling_retries_rate_limits_with_bounded_backoff(self):
        class Stop:
            stopped = False
            delays = []

            def is_set(self):
                return self.stopped

            def wait(self, delay):
                self.delays.append(delay)
                self.stopped = len(self.delays) == 3

        class API:
            def call(self, *_):
                raise w.APIError(429, retry_after=5)

        stop = Stop()
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.stderr", new_callable=io.StringIO):
                w.run_worker(API(), None, stop, Path(directory) / "cursor.json", {42}, set())
        self.assertEqual(stop.delays, [5, 5, 5])

    def test_send_uses_routing_and_plain_text_chunks(self):
        calls = []
        argv = ["worker.py", "send", "--chat-id", "-100", "--reply-to", "7",
                "--thread-id", "3", "--text", "x" * 4500]
        with patch.object(sys, "argv", argv), patch.object(w, "token_value", return_value="123:secret"):
            with patch.object(w.Telegram, "call", side_effect=lambda method, params: calls.append((method, params))):
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(w.main(), 0)
        self.assertEqual(len(calls), 2)
        for method, params in calls:
            self.assertEqual(method, "sendMessage")
            self.assertEqual(params["chat_id"], -100)
            self.assertEqual(params["reply_parameters"], {"message_id": 7})
            self.assertEqual(params["message_thread_id"], 3)
            self.assertNotIn("parse_mode", params)

    def test_file_logging_routing_without_message_content(self):
        stop = threading.Event()
        secret_text = "PRIVATE MESSAGE CONTENT"

        class API:
            count = 0

            def call(self, method, *_, **kwargs):
                if method == "sendChatAction":
                    return True
                self.count += 1
                if self.count == 1:
                    return [update(10, user=999, text=secret_text), update(11, text=secret_text)]
                stop.set()
                return []

        class RPC:
            def wakeup(self, params):
                pass

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "worker.log"
            handler = w.configure_log(log_path)
            try:
                w.run_worker(API(), RPC(), stop, Path(directory) / "cursor.json", {42}, set())
                log = log_path.read_text(encoding="utf-8")
                self.assertIn("Update filtered update_id=10", log)
                self.assertIn("Wakeup accepted update_id=11", log)
                self.assertIn("Cursor saved next_offset=12", log)
                self.assertNotIn(secret_text, log)
                self.assertNotIn("123456789", log)
                self.assertEqual(handler.maxBytes, 1048576)
                self.assertEqual(handler.backupCount, 3)
            finally:
                w.LOG.removeHandler(handler)
                handler.close()

    def test_real_stdio_handshake(self):
        # Run the real entry point with a missing token/allowlist. The handshake
        # must be the first stdout frame and errors must stay off stdout.
        import os
        env = os.environ.copy()
        env.pop("TELEGRAM_BOT_TOKEN", None)
        env["TELEGRAM_ALLOWED_USER_IDS"] = ""
        env.pop("TELEGRAM_ALLOW_ALL_USERS", None)
        log_dir = tempfile.TemporaryDirectory()
        self.addCleanup(log_dir.cleanup)
        log_path = Path(log_dir.name) / "worker.log"
        env["TELEGRAM_LOG_FILE"] = str(log_path)
        process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).with_name("worker.py")), "run"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            process.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"version":"1"}}\n')
            process.stdin.flush()
            # communicate closes stdin; wait for the actual response before EOF.
            result = queue.Queue()
            threading.Thread(target=lambda: result.put(process.stdout.readline()), daemon=True).start()
            response = json.loads(result.get(timeout=5))
            self.assertEqual(response["id"], 1)
            self.assertIn("result", response)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(stdout, b"")
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"worker stopped", stderr)
            log = log_path.read_text(encoding="utf-8")
            self.assertIn("handshake complete", log)
            self.assertIn("TELEGRAM_ALLOWED_USER_IDS is required", log)
            self.assertIn("Worker exiting", log)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
