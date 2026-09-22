"""Privacy-minimised, file-backed unique visitor counter."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile


class VisitorTracker:
    """Persist only keyed IP fingerprints, never raw network addresses."""

    def __init__(self, path: Path, secret: str):
        self.path = path
        self.secret = secret.encode('utf-8')
        self._fingerprints: set[str] | None = None
        self._lock = asyncio.Lock()

    def _load(self) -> set[str]:
        if self._fingerprints is not None:
            return self._fingerprints
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            values = payload.get('fingerprints', [])
            self._fingerprints = {value for value in values if isinstance(value, str) and len(value) == 64}
        except (OSError, ValueError, TypeError):
            self._fingerprints = set()
        return self._fingerprints

    def _save(self, fingerprints: set[str]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {'version': 1, 'unique_visitors': len(fingerprints),
                   'fingerprints': sorted(fingerprints)}
        temporary_path = None
        try:
            with NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                    prefix='.visitor-', suffix='.tmp', delete=False) as handle:
                json.dump(payload, handle, separators=(',', ':'))
                temporary_path = Path(handle.name)
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    async def record(self, address: str | None) -> int | None:
        """Record one valid IP and return the all-time unique visitor count."""
        try:
            normalized = ipaddress.ip_address(address or '').compressed
        except ValueError:
            return None
        fingerprint = hmac.new(self.secret, normalized.encode('ascii'), sha256).hexdigest()
        async with self._lock:
            fingerprints = self._load()
            if fingerprint not in fingerprints:
                fingerprints.add(fingerprint)
                self._save(fingerprints)
            return len(fingerprints)
