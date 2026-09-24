"""Authenticated WarDogs feed ingress for the local combat tracker.

No Steam session, RCON credentials, or bot database write account is used here.
"""
import asyncio
import json
import secrets
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import aiomysql

MAX_BODY_BYTES = 256_000
MAX_EVENTS = 200


def token_server(authorization, tokens):
    """Constant-time comparison; the changing feed serverId is not identity."""
    candidate = authorization[7:] if authorization.startswith('Bearer ') else ''
    if not candidate.isascii() or len(candidate) > 512:
        return None
    matches = [secrets.compare_digest(candidate, token) if token else False for token in tokens]
    return next((f'server{i}' for i, matched in enumerate(matches, 1) if matched), None)


def reject_nonfinite(_):
    raise ValueError('non-finite JSON number')


def validate_batch(raw):
    if not raw or len(raw) > MAX_BODY_BYTES:
        raise ValueError('size')
    try:
        batch = json.loads(raw, parse_constant=reject_nonfinite)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ValueError('json') from exc
    if not isinstance(batch, dict) or not isinstance(batch.get('events'), list):
        raise ValueError('events')
    if not 1 <= len(batch['events']) <= MAX_EVENTS:
        raise ValueError('count')
    if not isinstance(batch.get('serverId'), str) or not 1 <= len(batch['serverId']) <= 100:
        raise ValueError('serverId')
    seen = set()
    for event in batch['events']:
        if not isinstance(event, dict) or not isinstance(event.get('eventId'), str):
            raise ValueError('eventId')
        event_id = event['eventId']
        if not 1 <= len(event_id) <= 100 or event_id in seen:
            raise ValueError('eventId')
        seen.add(event_id)
    return batch


class FeedIngressDatabase:
    """Restricted writer: only the feed queue, never player/quest tables."""

    def __init__(self, url):
        self.url, self.pool = url, None
        self.lock = asyncio.Lock()

    async def connect(self):
        async with self.lock:
            if self.pool is None or self.pool.closed:
                parsed = urlsplit(self.url)
                if parsed.scheme != 'mysql' or not parsed.hostname or not parsed.path.strip('/'):
                    raise RuntimeError('Feed database not configured')
                self.pool = await aiomysql.create_pool(
                    host=parsed.hostname, port=parsed.port or 3306,
                    user=unquote(parsed.username or ''), password=unquote(parsed.password or ''),
                    db=unquote(parsed.path.lstrip('/')), charset='utf8mb4', autocommit=True,
                    minsize=1, maxsize=2, connect_timeout=5, pool_recycle=120,
                    init_command="SET time_zone = '+00:00'")
        return self.pool

    async def receive(self, server_id, raw):
        batch_id = str(uuid4())
        async with asyncio.timeout(8):
            pool = await self.connect()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # aiomysql/PyMySQL on the AMP runtime cannot bind bytes here;
                    # UNHEX preserves the original payload for local processing.
                    await cur.execute('''INSERT INTO combat_feed_batches
                        (id,server_id,payload,received_at,next_forward_at)
                        VALUES (%s,%s,UNHEX(%s),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))''',
                                      (batch_id, server_id, raw.hex()))
        return batch_id

    async def close(self):
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
