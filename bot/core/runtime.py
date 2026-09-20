"""Atomic local state and transition-only operational reporting."""
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import discord


def read_state(path, default):
    path = Path(path)
    if not path.exists():
        return default
    # Corrupt state must not silently create duplicate panels or discard votes.
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(path, value):
    path = Path(path).resolve()
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Health:
    def __init__(self, bot):
        self.bot = bot
        self.failed = set()
        self.last_error = {}
        self.last_log_at = {}

    def error(self, key, exc):
        # Exception bodies may contain remote responses or connection strings.
        message = getattr(exc, 'safe_message', type(exc).__name__)
        now = time.monotonic()
        # Polling must keep retrying, but a temporarily unavailable RCON endpoint
        # should not write an identical error to the console every few seconds.
        if (self.last_error.get(key) != message or
                now - self.last_log_at.get(key, 0) >= 300):
            logging.error("%s: %s", key, message)
            self.last_log_at[key] = now
        self.last_error[key] = message
        if key not in self.failed:
            self.failed.add(key)
            self.bot.dispatch("bot_log", "⚠️ Funktion gestört", key, discord.Color.orange())

    def ok(self, key):
        if key in self.failed:
            self.failed.remove(key)
            self.last_error.pop(key, None)
            self.last_log_at.pop(key, None)
            self.bot.dispatch("bot_log", "✅ Funktion wieder verfügbar", key, discord.Color.green())
