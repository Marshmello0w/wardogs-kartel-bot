"""One HTTP session; explicit failures; serialized revision-aware config edits."""
import asyncio
from collections import defaultdict
from dataclasses import dataclass
import json
import time
import aiohttp
from core import config


class RconError(RuntimeError):
    def __init__(self, status=None, uncertain=False, reason=None):
        self.status, self.uncertain, self.reason = status, uncertain, reason
        super().__init__(f"RCON HTTP {status}" if status else "RCON unavailable or invalid response")

    @property
    def safe_message(self):
        """A diagnostic that is useful in logs but cannot disclose a connection string."""
        if self.reason:
            return f"RconError ({self.reason})"
        if self.status:
            return f"RconError (HTTP {self.status})"
        return "RconError"


@dataclass
class Reply:
    status: int
    data: object


class RconClient:
    def __init__(self):
        self.session = None
        self.request_locks = defaultdict(asyncio.Lock)
        self.write_locks = defaultdict(asyncio.Lock)
        self.config_locks = defaultdict(asyncio.Lock)
        self.read_failures = {}
        self.read_flights = {}

    async def close(self):
        if self.session is not None:
            await self.session.close()

    async def request(self, server_id, method, path, *, payload=None, text=None, revision=None, guard=None):
        srv = config.server(server_id)
        if not srv.enabled:
            raise RconError(reason='invalid configuration')
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=config.RCON_TIMEOUT_SECONDS))
        headers = {"Authorization": f"Bearer {srv.password}"}
        kwargs = {}
        if text is not None:
            headers["Content-Type"] = "text/plain; charset=utf-8"
            kwargs['data'] = text.encode('utf-8')
        elif payload is not None:
            kwargs['json'] = payload
        if revision is not None:
            headers['If-Match'] = f'"{revision}"'

        async def perform():
            # The gameserver RCON endpoint can transiently reject parallel reads.
            # Serialize all operations per server; writes retain their own ordering below.
            async with self.request_locks[server_id]:
                if guard is not None and not guard():
                    raise RconError(409)
                try:
                    async with self.session.request(method, srv.url + path, headers=headers, **kwargs) as response:
                        raw = await response.text()
                        try:
                            data = json.loads(raw) if raw else None
                        except ValueError:
                            data = None
                        if not 200 <= response.status < 300:
                            raise RconError(response.status, uncertain=method != 'GET' and response.status >= 500)
                        if isinstance(data, dict) and data.get('ok') is False:
                            raise RconError(response.status, reason='rejected response')
                        if method == 'GET' and not isinstance(data, dict):
                            raise RconError(response.status, reason='invalid response')
                        return Reply(response.status, data)
                except asyncio.TimeoutError:
                    raise RconError(uncertain=method != 'GET', reason='timeout') from None
                except aiohttp.ClientConnectorError:
                    raise RconError(uncertain=method != 'GET', reason='connection failed') from None
                except aiohttp.ClientError:
                    raise RconError(uncertain=method != 'GET', reason='transport error') from None

        if method == 'GET':
            key = (server_id, path)
            attempts, retry_at, last_status, last_reason = self.read_failures.get(key, (0, 0, None, None))
            if time.monotonic() < retry_at:
                raise RconError(last_status, reason=last_reason)

            async def read_once():
                try:
                    reply = await perform()
                except RconError as exc:
                    self.read_failures[key] = (attempts + 1, time.monotonic() + min(60, 5 * 2 ** min(attempts, 4)),
                                               exc.status, exc.reason)
                    raise
                self.read_failures.pop(key, None)
                return reply

            flight = self.read_flights.get(key)
            if flight is None:
                flight = asyncio.create_task(read_once())
                self.read_flights[key] = flight

                def clear_flight(done):
                    if self.read_flights.get(key) is done:
                        self.read_flights.pop(key, None)
                    if not done.cancelled():
                        done.exception()

                flight.add_done_callback(clear_flight)
            return await asyncio.shield(flight)
        async with self.write_locks[server_id]:
            return await perform()

    async def get(self, server_id, path):
        return (await self.request(server_id, 'GET', path)).data

    async def players(self, server_id):
        data = await self.get(server_id, '/v1/players')
        if not isinstance(data.get('players'), list):
            raise RconError()
        if not all(isinstance(p, dict) for p in data['players']):
            raise RconError()
        return data['players']

    async def bans(self, server_id):
        data = await self.get(server_id, '/v1/bans')
        if not isinstance(data.get('bans'), list) or not all(
                isinstance(b, dict) and 'steamId' in b for b in data['bans']):
            raise RconError()
        return {str(b['steamId']) for b in data['bans']}

    async def edit_config(self, server_id, transform):
        """Transform must be deterministic and must preserve unrelated configuration."""
        async with self.config_locks[server_id]:
            for attempt in range(3):
                current = await self.get(server_id, '/v1/config')
                if not isinstance(current.get('text'), str) or not current.get('revision') or current.get('writable') is False:
                    raise RconError()
                changed = transform(current['text'])
                if changed == current['text']:
                    return
                try:
                    await self.request(server_id, 'PUT', '/v1/config', text=changed, revision=current['revision'])
                except RconError as exc:
                    if exc.status == 412:
                        continue
                    if not exc.uncertain:
                        raise
                verified = await self.get(server_id, '/v1/config')
                if verified.get('text') == changed or transform(verified.get('text', '')) == verified.get('text'):
                    return
                raise RconError(uncertain=True)
            raise RconError(412)
