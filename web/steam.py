"""Steam OpenID 2.0 relying party; the provider and identity origin are fixed."""
import asyncio
from datetime import datetime, timezone
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

OPENID_URL = 'https://steamcommunity.com/openid/login'
OPENID_NS = 'http://specs.openid.net/auth/2.0'
IDENTIFIER = 'http://specs.openid.net/auth/2.0/identifier_select'
CLAIM = re.compile(r'https://steamcommunity\.com/openid/id/(7656119[0-9]{10})\Z')
AVATAR_HOSTS = {'avatars.steamstatic.com', 'avatars.akamai.steamstatic.com',
                'steamcdn-a.akamaihd.net', 'cdn.akamai.steamstatic.com'}


class LoginError(ValueError):
    pass


class SteamLogin:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.pending = {}
        self.lock = asyncio.Lock()

    async def begin(self):
        async with self.lock:
            now = time.monotonic()
            self.pending = {k: v for k, v in self.pending.items() if v > now}
            if len(self.pending) >= 4096:
                raise LoginError('Too many pending logins')
            state = secrets.token_urlsafe(32)
            self.pending[state] = now + 600
        return_to = self.settings.callback + '?' + urlencode({'state': state})
        params = {'openid.ns': OPENID_NS, 'openid.mode': 'checkid_setup',
                  'openid.return_to': return_to, 'openid.realm': self.settings.base_url + '/',
                  'openid.identity': IDENTIFIER, 'openid.claimed_id': IDENTIFIER}
        return state, OPENID_URL + '?' + urlencode(params)

    async def verify(self, pairs, expected_state):
        params = dict(pairs)
        if len(params) != len(pairs) or len(params) > 32 or any(len(v) > 4096 for v in params.values()):
            raise LoginError('Invalid parameters')
        state = params.get('state', '')
        if not expected_state or not secrets.compare_digest(state, expected_state):
            raise LoginError('Invalid login state')
        # Consume BEFORE network I/O: concurrent callbacks cannot replay a login.
        async with self.lock:
            expires = self.pending.pop(state, 0)
        if expires < time.monotonic():
            raise LoginError('Expired or replayed login')
        expected_return = self.settings.callback + '?' + urlencode({'state': state})
        if (params.get('openid.ns') != OPENID_NS or params.get('openid.mode') != 'id_res'
                or params.get('openid.op_endpoint') != OPENID_URL
                or params.get('openid.return_to') != expected_return):
            raise LoginError('Invalid provider response')
        claim = params.get('openid.claimed_id', '')
        match = CLAIM.fullmatch(claim)
        if not match or params.get('openid.identity') != claim:
            raise LoginError('Invalid Steam identity')
        required = {'op_endpoint', 'claimed_id', 'identity', 'return_to', 'response_nonce', 'assoc_handle'}
        if not required.issubset(set(params.get('openid.signed', '').split(','))):
            raise LoginError('Unsigned identity fields')
        nonce = params.get('openid.response_nonce', '')
        try:
            stamp = datetime.strptime(nonce[:20], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise LoginError('Invalid nonce') from exc
        if len(nonce) <= 20 or not -60 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 600:
            raise LoginError('Expired nonce')
        verification = {k: v for k, v in params.items() if k.startswith('openid.')}
        verification['openid.mode'] = 'check_authentication'
        response = await self.client.post(OPENID_URL, data=verification)
        response.raise_for_status()
        lines = dict(line.split(':', 1) for line in response.text.splitlines() if ':' in line)
        if lines.get('is_valid') != 'true':
            raise LoginError('Steam rejected the signature')
        return match.group(1)

    async def summary(self, steam_id):
        fallback = {'name': '', 'avatar': ''}
        if not self.settings.steam_key:
            return fallback
        response = await self.client.get('https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/',
                                         params={'key': self.settings.steam_key, 'steamids': steam_id})
        response.raise_for_status()
        for player in response.json().get('response', {}).get('players', []):
            if player.get('steamid') == steam_id:
                avatar = str(player.get('avatarfull', ''))
                parsed = urlsplit(avatar)
                if parsed.scheme != 'https' or parsed.hostname not in AVATAR_HOSTS or parsed.username:
                    avatar = ''
                return {'name': str(player.get('personaname', ''))[:255], 'avatar': avatar}
        return fallback
