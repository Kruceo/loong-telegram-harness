"""Worker-local Telegram conversation keys; no Loong runtime configuration."""

import json
import math
from pathlib import Path
import os
import tempfile
import time
import uuid

IDLE_SECONDS = 3 * 60 * 60


class SessionStateError(Exception):
    pass


class ConversationSessions:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.conversations = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError()
                for base, entry in data.items():
                    if (not isinstance(base, str) or not isinstance(entry, dict)
                            or not isinstance(entry.get("key"), str) or not entry["key"]
                            or type(entry.get("last_received")) not in (int, float)
                            or not math.isfinite(entry["last_received"]) or entry["last_received"] < 0):
                        raise ValueError()
                self.conversations = data
            except (OSError, ValueError, TypeError):
                raise SessionStateError("Invalid Telegram conversation state; restore or remove it explicitly") from None

    def select(self, base):
        now = self.clock()
        previous = self.conversations.get(base)
        # First use keeps the legacy key so an active conversation survives the
        # upgrade. No historical receive timestamp exists for pre-upgrade chats.
        rotated = previous is not None and now - previous["last_received"] >= IDLE_SECONDS
        key = (base + ":conversation:" + uuid.uuid4().hex if rotated
               else previous["key"] if previous is not None else base)
        entry = {"key": key, "last_received": now}
        self.conversations[base] = entry
        try:
            self._save()
        except OSError:
            if previous is None:
                self.conversations.pop(base)
            else:
                self.conversations[base] = previous
            raise SessionStateError("Cannot persist Telegram conversation state") from None
        return key, rotated

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             delete=False) as output:
                name = output.name
                json.dump(self.conversations, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)
