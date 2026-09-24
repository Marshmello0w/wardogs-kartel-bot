"""Authenticated WarDogs feed ingress and durable provider forwarding.

No Steam session, RCON credentials, or bot database write account is used here.
The forwarding queue stores the original bytes, so delivery can be retried
without reconstructing the provider's event format.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import secrets
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import aiomysql
import httpx

logger = logging.getLogger('kartell.feed')
MAX_BODY_BYTES = 256_000
MAX_EVENTS = 200
PROVIDER_URL = 'https://pteroapi.pockethost.cloud/api/wardogs/feed/api/ingest/events'


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
                    # UNHEX preserves the original payload for exact forwarding.
                    await cur.execute('''INSERT INTO combat_feed_batches
                        (id,server_id,payload,received_at,next_forward_at)
                        VALUES (%s,%s,UNHEX(%s),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))''',
                                      (batch_id, server_id, raw.hex()))
        return batch_id

    async def pending(self, limit=20):
        pool = await self.connect()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute('''SELECT id,server_id,payload,forward_attempts,received_at
                    FROM combat_feed_batches WHERE forwarded_at IS NULL
                    AND next_forward_at<=UTC_TIMESTAMP(6)
                    ORDER BY received_at LIMIT %s''', (limit,))
                return await cur.fetchall()

    async def forwarded(self, batch_id):
        pool = await self.connect()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute('''UPDATE combat_feed_batches SET forwarded_at=UTC_TIMESTAMP(6),
                    last_forward_error=NULL WHERE id=%s AND forwarded_at IS NULL''', (batch_id,))

    async def failed(self, batch_id, attempts, reason):
        delay = min(300, 2 ** min(attempts, 8))
        pool = await self.connect()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute('''UPDATE combat_feed_batches SET forward_attempts=forward_attempts+1,
                    next_forward_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL %s SECOND),
                    last_forward_error=%s WHERE id=%s AND forwarded_at IS NULL''',
                                  (delay, reason[:100], batch_id))

    async def close(self):
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()


async def forward_once(db, tokens, client):
    """At-least-once delivery; never discard a failed batch."""
    rows = await db.pending()
    for row in rows:
        token = tokens[int(row['server_id'][-1]) - 1]
        try:
            response = await client.post(PROVIDER_URL, content=row['payload'],
                                         headers={'Authorization': f'Bearer {token}',
                                                  'Content-Type': 'application/json'})
            response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            # No URL/headers/body in logs: they may contain a bearer credential.
            await db.failed(row['id'], row['forward_attempts'], type(exc).__name__)
            age = datetime.now(timezone.utc) - row['received_at'].replace(tzinfo=timezone.utc)
            if age > timedelta(minutes=5):
                logger.error('Feed forwarding backlog: %s, age %d seconds',
                             row['server_id'], int(age.total_seconds()))
        else:
            await db.forwarded(row['id'])
    return len(rows)


async def forwarding_loop(db, tokens, client):
    while True:
        try:
            await forward_once(db, tokens, client)
        except (aiomysql.Error, OSError, RuntimeError) as exc:
            logger.error('Feed forwarding unavailable: %s', type(exc).__name__)
        await asyncio.sleep(2)
