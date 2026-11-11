#!/usr/bin/env python3
"""Telegram -> Loong wakeup plugin. Standard library only; stdout is JSON-RPC."""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import signal
import sys
import tempfile
import threading
import urllib.error
import urllib.request

import media
import sessions

MAX_FRAME = 1048576
POLL_SECONDS = 25
ACK_SECONDS = 90
LOG = logging.getLogger("loong.telegram")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


def configure_log(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1048576, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s pid=%(process)d %(message)s"))
    LOG.setLevel(logging.INFO)
    LOG.addHandler(handler)
    return handler


class WorkerError(Exception):
    pass


class APIError(WorkerError):
    def __init__(self, code, retry_after=0):
        super().__init__(f"Telegram API error ({code})")
        self.code = code
        self.retry_after = retry_after


def token_value(path):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        try:
            token = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            raise WorkerError("Set TELEGRAM_BOT_TOKEN or create the configured token file") from None
    if not token or any(c.isspace() for c in token) or ":" not in token:
        raise WorkerError("Invalid Telegram bot token")
    return token


def parse_ids(value, required=False):
    try:
        ids = {int(part.strip()) for part in value.split(",") if part.strip()}
    except ValueError:
        raise WorkerError("Telegram allowlists must contain comma-separated numeric IDs") from None
    if required and not ids:
        raise WorkerError("TELEGRAM_ALLOWED_USER_IDS is required (deny by default)")
    return ids


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, params, *, timeout=POLL_SECONDS + 10):
        request = urllib.request.Request(
            self.base + method,
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            retry_after = 0
            try:
                retry_after = int(json.load(exc).get("parameters", {}).get("retry_after", 0))
            except (ValueError, TypeError, AttributeError):
                pass
            raise APIError(exc.code, retry_after) from None
        except (OSError, ValueError):
            # Never expose exception strings: urllib URLs contain the bot token.
            raise WorkerError("Telegram network/response error") from None
        if not isinstance(data, dict) or not data.get("ok"):
            raise APIError(data.get("error_code", 502) if isinstance(data, dict) else 502)
        return data.get("result")


class RPC:
    """One reader owns stdin; EOF interrupts waits without an unbounded receive."""

    def __init__(self, source, target, stop):
        self.source, self.target, self.stop = source, target, stop
        self.frames = queue.Queue(maxsize=16)
        self.limit = MAX_FRAME
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while not self.stop.is_set():
                line = self.source.readline(MAX_FRAME + 1)
                if not line:
                    LOG.info("Loong stdin closed")
                    break
                if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                    LOG.error("Invalid or oversized Loong frame")
                    break
                frame = json.loads(line)
                if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
                    break
                self.frames.put(frame, timeout=1)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            self.stop.set()

    def receive(self, seconds):
        # A bounded wait also observes SIGTERM and stdin EOF.
        import time
        deadline = time.monotonic() + seconds
        while not self.stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError("Loong response timeout; delivery may be ambiguous")
            try:
                return self.frames.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                continue
        raise WorkerError("Loong input closed or worker stopped")

    def send(self, frame):
        data = json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
        if len(data) > self.limit:
            raise WorkerError("Wakeup frame exceeds negotiated frame limit")
        self.target.write(data)
        self.target.flush()

    def initialize(self):
        frame = self.receive(5)
        params = frame.get("params", {})
        if (frame.get("method") != "initialize" or "id" not in frame
                or not isinstance(params, dict) or params.get("version") != "1"):
            raise WorkerError("Expected wakeup protocol initialize version 1")
        limit = params.get("max_frame_size", MAX_FRAME)
        if type(limit) is not int or limit < 256:
            raise WorkerError("Invalid negotiated frame limit")
        self.limit = min(MAX_FRAME, limit)
        self.image_paths = params.get("image_paths") is True
        self.send({"jsonrpc": "2.0", "id": frame["id"], "result": {"version": "1"}})
        LOG.info("Loong handshake complete frame_limit=%d", self.limit)

    def wakeup(self, params):
        self.next_id += 1
        request_id = f"telegram-{self.next_id}"
        if params.get("images") and not self.image_paths:
            raise WorkerError("Client does not support wakeup image paths")
        self.send({"jsonrpc": "2.0", "id": request_id, "method": "wakeup", "params": params})
        frame = self.receive(ACK_SECONDS)
        if (frame.get("id") != request_id or "error" in frame
                or frame.get("result") != {"status": "accepted"}):
            raise WorkerError("Wakeup not accepted; cursor retained, delivery may be ambiguous")


def send_typing(api, chat_id, thread_id=None):
    params = {"chat_id": chat_id, "action": "typing"}
    if thread_id is not None:
        params["message_thread_id"] = thread_id
    # Cosmetic feedback must not hold up admission on a long-poll deadline.
    api.call("sendChatAction", params, timeout=3)


def notify_typing(api, context):
    try:
        send_typing(api, context["chat_id"], context.get("message_thread_id"))
    except WorkerError:
        # Best effort, no retries or external error descriptions in logs.
        LOG.warning("Typing notification failed; continuing wakeup")


def configured_users():
    if os.environ.get("TELEGRAM_ALLOW_ALL_USERS", "").strip().lower() == "true":
        LOG.warning("User allowlist DISABLED for testing; any user can trigger the agent")
        return None
    return parse_ids(os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""), required=True)


def wakeup_params(update, allowed_users, allowed_chats):
    message = update.get("message", {})
    sender = message.get("from", {})
    chat = message.get("chat", {})
    text = message.get("text") or message.get("caption") or ""
    has_media = bool(message.get("photo") or message.get("document"))
    if (sender.get("is_bot") or type(sender.get("id")) is not int
            or (allowed_users is not None and sender["id"] not in allowed_users)
            or not isinstance(text, str) or (not text.strip() and not has_media)):
        return None
    if allowed_chats:
        if chat.get("id") not in allowed_chats:
            return None
    elif chat.get("type") != "private":
        return None
    chat_id, message_id = chat.get("id"), message.get("message_id")
    if type(chat_id) is not int or type(message_id) is not int:
        return None
    thread_id = message.get("message_thread_id")
    session_key = f"telegram:{chat_id}"
    context = {"source": "telegram", "chat_id": chat_id, "message_id": message_id,
               "user_id": sender["id"]}
    if type(thread_id) is int:
        session_key += f":topic:{thread_id}"
        context["message_thread_id"] = thread_id
    return {"event_id": f"telegram:{update['update_id']}", "session_key": session_key,
            "prompt": text or "O usuário enviou um anexo. Analise-o se possível.", "context": context}


def receive_media(api, update, event, root, limit):
    message = update["message"]
    photos = message.get("photo", [])
    attachment = photos[-1] if photos else message.get("document")
    if not attachment:
        return
    if not isinstance(attachment, dict) or not isinstance(attachment.get("file_id"), str):
        raise WorkerError("Invalid Telegram attachment")
    try:
        item = media.download(api, attachment, update["update_id"], root, limit)
        event["context"]["attachments"] = [item]
        if item["mime_type"].startswith("image/"):
            event["images"] = [{"path": item["path"], "mime_type": item["mime_type"]}]
        LOG.info("Attachment downloaded update_id=%d bytes=%d image=%s",
                 update["update_id"], item["size"], bool(event.get("images")))
    except (media.MediaError, WorkerError):
        # Admit a useful turn explaining that an attachment could not be read,
        # without stalling all chats or exposing download URLs/filenames.
        event["context"]["attachment_error"] = "Attachment download rejected or failed (limit/network/metadata)"
        LOG.warning("Attachment unavailable update_id=%d", update["update_id"])



def load_offset(path):
    if not path.exists():
        return None
    try:
        offset = json.loads(path.read_text(encoding="utf-8"))["next_offset"]
        if type(offset) is not int or offset < 0:
            raise ValueError()
        return offset
    except (OSError, ValueError, KeyError, TypeError):
        raise WorkerError("Invalid Telegram cursor file; restore it or remove it explicitly") from None


def save_offset(path, offset):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as output:
            name = output.name
            json.dump({"next_offset": offset}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def run_worker(api, rpc, stop, state, users, chats, media_root="state/attachments", media_limit=media.MAX_DOWNLOAD):
    offset = load_offset(state)
    conversations = sessions.ConversationSessions(Path(state).with_name("conversations.json"))
    backoff = 1
    LOG.info("Polling started cursor=%s allowed_users=%d allowed_chats=%d private_only=%s",
             offset, -1 if users is None else len(users), len(chats), not chats)
    while not stop.is_set():
        params = {"timeout": POLL_SECONDS, "allowed_updates": ["message"], "limit": 25}
        if offset is not None:
            params["offset"] = offset
        try:
            updates = api.call("getUpdates", params)
            if not isinstance(updates, list):
                raise WorkerError("Invalid Telegram updates response")
            backoff = 1
            LOG.info("Poll complete updates=%d", len(updates))
        except WorkerError as exc:
            if isinstance(exc, APIError) and exc.code in (400, 401, 403, 404, 409):
                raise
            print(str(exc), file=sys.stderr)
            delay = max(backoff, min(300, getattr(exc, "retry_after", 0)))
            LOG.warning("Polling failed: %s; retry_seconds=%s", exc, delay)
            stop.wait(delay)
            backoff = min(30, backoff * 2)
            continue
        for update in updates:
            if stop.is_set():
                return
            if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                raise WorkerError("Invalid Telegram update ID")
            next_offset = update["update_id"] + 1
            if offset is not None and next_offset <= offset:
                continue
            event = wakeup_params(update, users, chats)
            if event is not None:
                # Persist selection before RPC: an ambiguous ACK/restart must
                # reuse this conversation key rather than rotating a second time.
                event["session_key"], rotated = conversations.select(event["session_key"])
                if rotated:
                    LOG.info("New conversation after 3h idle update_id=%d", update["update_id"])
                notify_typing(api, event["context"])
                receive_media(api, update, event, media_root, media_limit)
                if event.get("images") and not getattr(rpc, "image_paths", False):
                    event.pop("images")
                    event["context"]["image_delivery_error"] = "Client lacks image_paths support; rebuild/restart Loong for model vision"
                    LOG.warning("Client lacks image_paths support update_id=%d", update["update_id"])
                # Never retry a wakeup automatically: timeout can mean admitted.
                LOG.info("Wakeup submitting update_id=%d", update["update_id"])
                rpc.wakeup(event)
                LOG.info("Wakeup accepted update_id=%d (admission only, not completion)", update["update_id"])
            else:
                LOG.info("Update filtered update_id=%d (allowlist/chat/type/text)", update["update_id"])
            save_offset(state, next_offset)
            offset = next_offset
            LOG.info("Cursor saved next_offset=%d", offset)


def chunks(text, limit=4000):
    """Telegram measures message length in UTF-16 code units."""
    part, size = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            yield "".join(part)
            part, size = [], 0
        part.append(char)
        size += width
    if part:
        yield "".join(part)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", default=os.environ.get(
        "TELEGRAM_BOT_TOKEN_FILE", "bot-token.txt"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Run the stdio wakeup plugin (started by Loong)")
    typing = sub.add_parser("typing", help="Show typing for about five seconds; no automatic retries")
    typing.add_argument("--chat-id", type=int, required=True)
    typing.add_argument("--thread-id", type=int)
    send = sub.add_parser("send", help="Send a plain-text reply; no automatic retries")
    send.add_argument("--chat-id", type=int, required=True)
    send.add_argument("--reply-to", type=int)
    send.add_argument("--thread-id", type=int)
    send.add_argument("--text", required=True)
    for name in ("send-photo", "send-document"):
        transfer = sub.add_parser(name, help="Upload a local attachment; no automatic retry")
        transfer.add_argument("--chat-id", type=int, required=True)
        transfer.add_argument("--reply-to", type=int)
        transfer.add_argument("--thread-id", type=int)
        transfer.add_argument("--file", required=True)
        transfer.add_argument("--caption", default="")
    args = parser.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    handler = None
    try:
        # Only the long-lived worker rotates this file. Concurrent send helpers
        # must not race its rotation (especially on Windows).
        if args.command == "run":
            handler = configure_log(os.environ.get("TELEGRAM_LOG_FILE", "state/worker.log"))
            LOG.info("Worker starting; waiting for Loong initialize")
        if args.command == "typing":
            send_typing(Telegram(token_value(args.token_file)), args.chat_id, args.thread_id)
            print("Telegram typing action sent")
        elif args.command == "send":
            if not args.text.strip():
                raise WorkerError("Reply text cannot be empty")
            api = Telegram(token_value(args.token_file))
            for part in chunks(args.text):
                params = {"chat_id": args.chat_id, "text": part}
                if args.reply_to is not None:
                    params["reply_parameters"] = {"message_id": args.reply_to}
                if args.thread_id is not None:
                    params["message_thread_id"] = args.thread_id
                api.call("sendMessage", params)
            print("Telegram reply sent")
        elif args.command in ("send-photo", "send-document"):
            if len(args.caption.encode("utf-16-le")) // 2 > 1024:
                raise WorkerError("Attachment caption exceeds 1024 UTF-16 units")
            params = {"chat_id": args.chat_id, "caption": args.caption}
            if args.reply_to is not None:
                params["reply_parameters"] = {"message_id": args.reply_to}
            if args.thread_id is not None:
                params["message_thread_id"] = args.thread_id
            photo = args.command == "send-photo"
            media.upload(Telegram(token_value(args.token_file)), "sendPhoto" if photo else "sendDocument",
                         "photo" if photo else "document", args.file, params)
            print("Telegram attachment sent")
        else:
            rpc = RPC(sys.stdin.buffer, sys.stdout.buffer, stop)
            rpc.initialize()
            users = configured_users()
            chats = parse_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
            api = Telegram(token_value(args.token_file))
            run_worker(api, rpc, stop, Path(os.environ.get(
                "TELEGRAM_STATE_FILE", "state/cursor.json")), users, chats)
    except sessions.SessionStateError as exc:
        LOG.error("Worker failed: %s", exc)
        print(f"Telegram worker stopped: {exc}", file=sys.stderr)
        return 1
    except media.MediaError as exc:
        print(f"Telegram attachment failed: {exc}", file=sys.stderr)
        return 1
    except WorkerError as exc:
        # All WorkerError messages are constructed locally, never from HTTP URLs
        # or Telegram descriptions (either can contain credentials/user data).
        LOG.error("Worker failed: %s", exc)
        print(f"Telegram worker stopped: {exc}", file=sys.stderr)
        return 1
    except OSError:
        LOG.error("Worker failed: local I/O or protocol pipe error")
        print("Telegram worker stopped: local I/O or protocol pipe error", file=sys.stderr)
        return 1
    except Exception as exc:
        # Do not log exception text/tracebacks: third-party URLs or local data
        # may carry credentials. The exception type is enough to flag a bug.
        LOG.error("Unexpected worker failure type=%s", type(exc).__name__)
        print("Telegram worker stopped: unexpected internal error", file=sys.stderr)
        return 1
    finally:
        if handler is not None:
            LOG.info("Worker exiting")
            LOG.removeHandler(handler)
            handler.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
