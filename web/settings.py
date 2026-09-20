from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
SERVERS = {f'server{i}': f'Server {i}' for i in range(1, 4)}


@dataclass(frozen=True)
class Settings:
    base_url: str = 'https://kartell.marshmello0w.de'
    db_url: str = field(default='', repr=False)
    steam_key: str = field(default='', repr=False)
    session_secret: str = field(default='', repr=False)

    def __post_init__(self):
        parsed = urlsplit(self.base_url)
        local = parsed.hostname in ('localhost', '127.0.0.1')
        if (parsed.scheme != 'https' and not (local and parsed.scheme == 'http')) or not parsed.hostname:
            raise ValueError('PUBLIC_BASE_URL requires HTTPS (HTTP only on localhost)')
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('PUBLIC_BASE_URL must be an origin without path or credentials')
        if len(self.session_secret) < 32:
            raise ValueError('WEB_SESSION_SECRET must contain at least 32 characters')
        object.__setattr__(self, 'base_url', self.base_url.rstrip('/'))

    @property
    def secure(self):
        return self.base_url.startswith('https://')

    @property
    def callback(self):
        return self.base_url + '/auth/steam/callback'

    @classmethod
    def from_env(cls):
        # Explicit path: never discover/import the bot's .env or configuration.
        values = {**dotenv_values(ROOT / '.env'), **os.environ}
        return cls(base_url=values.get('PUBLIC_BASE_URL', cls.base_url),
                   db_url=values.get('WEB_DB_CONNECTION_URL', ''),
                   steam_key=values.get('STEAM_WEB_API_KEY', ''),
                   session_secret=values.get('WEB_SESSION_SECRET', ''))
