"""Read-only queries. No imports from the bot, no migrations, no RCON access."""
import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import json
import math
import time
from urllib.parse import unquote, urlsplit

import aiomysql

from .settings import SERVERS


class DataUnavailable(RuntimeError):
    pass


class ReadDatabase:
    def __init__(self, url):
        self.url, self.pool = url, None
        self.lock = asyncio.Lock()

    async def connect(self):
        async with self.lock:
            if self.pool is None or self.pool.closed:
                parsed = urlsplit(self.url)
                if parsed.scheme != 'mysql' or not parsed.hostname or not parsed.path.strip('/'):
                    raise DataUnavailable('Missing web database configuration')
                self.pool = await aiomysql.create_pool(
                    host=parsed.hostname, port=parsed.port or 3306,
                    user=unquote(parsed.username or ''), password=unquote(parsed.password or ''),
                    db=unquote(parsed.path.lstrip('/')), charset='utf8mb4', autocommit=True,
                    minsize=1, maxsize=4, connect_timeout=5, pool_recycle=120,
                    init_command="SET time_zone = '+00:00'")
        return self.pool

    async def query(self, sql, args=()):
        try:
            async with asyncio.timeout(10):
                pool = await self.connect()
                async with pool.acquire() as conn:
                    async with conn.cursor(aiomysql.DictCursor) as cur:
                        await cur.execute('SET SESSION TRANSACTION READ ONLY')
                        await cur.execute(sql, args)
                        return await cur.fetchall()
        except (TimeoutError, aiomysql.Error, ValueError, OSError) as exc:
            raise DataUnavailable('Database query failed') from exc

    async def close(self):
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()


def ranking_query(server_id, timeframe='all', sort_by='kd'):
    """Matches bot/domain/stats.py; a parity test detects any future divergence."""
    if server_id not in SERVERS or timeframe not in ('all', '7d', '30d') or sort_by not in ('kd', 'cash'):
        raise ValueError('Invalid ranking')
    exclusion = """AND NOT EXISTS (SELECT 1 FROM banned_players b WHERE b.server_id=l.server_id AND b.steam_id=l.steam_id)
        AND NOT EXISTS (SELECT 1 FROM admin_targets t WHERE t.steam_id=l.steam_id AND t.desired='ban'
        AND (t.expires_at IS NULL OR t.expires_at>UTC_TIMESTAMP()))"""
    if timeframe == 'all':
        base = f"""SELECT l.steam_id,l.name,l.lifetime_kills kills,l.lifetime_deaths deaths,l.lifetime_cash cash
            FROM leaderboard l WHERE l.server_id=%s {exclusion}"""
    else:
        days = 6 if timeframe == '7d' else 29
        base = f"""SELECT l.steam_id,l.name,SUM(d.kills) kills,SUM(d.deaths) deaths,SUM(d.cash) cash
            FROM leaderboard l JOIN player_daily_stats d ON d.server_id=l.server_id AND d.steam_id=l.steam_id
            WHERE l.server_id=%s AND d.date BETWEEN DATE_SUB(UTC_DATE(),INTERVAL {days} DAY) AND UTC_DATE()
            {exclusion} GROUP BY l.steam_id,l.name"""
    order = 'cash DESC,kills DESC,steam_id ASC' if sort_by == 'cash' else 'kd DESC,kills DESC,steam_id ASC'
    return f"""SELECT scored.*,ROW_NUMBER() OVER (ORDER BY {order}) AS player_rank
        FROM (SELECT totals.*,kills / IF(deaths=0,1,deaths) AS kd FROM ({base}) totals) scored""", (server_id,)


def parse_time(value):
    if not value:
        return None
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def server_status(server_id, payload, now=None):
    result = {'id': server_id, 'title': SERVERS[server_id], 'fresh': False, 'observed_at': None,
              'players': None, 'capacity': None, 'map': 'Noch keine Daten', 'mode': '—',
              'scores': [], 'label': 'Keine Messung', 'ended': False}
    try:
        state = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(state, dict):
            return result
        snap = state['snapshot']
        if not isinstance(snap.get('map'), str) or not snap['map']:
            return result
        if not isinstance(snap.get('experiences', []), list) or not all(isinstance(x, str) for x in snap.get('experiences', [])):
            return result
        scores = snap.get('factionScores')
        if not isinstance(scores, list) or not scores or not all(
                isinstance(s, dict) and isinstance(s.get('name'), str)
                and type(s.get('score')) in (int, float) and math.isfinite(s['score']) and s['score'] >= 0
                for s in scores):
            return result
        stamp = parse_time(state['observed_at'])
        age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
        players = snap.get('players', {})
        count = players.get('current') if isinstance(players, dict) else None
        capacity = players.get('max') if isinstance(players, dict) else None
        valid_count = type(count) is int and count >= 0
        fresh = 0 <= age <= 30 and not state.get('uncertain') and valid_count
        result.update(observed_at=stamp, fresh=fresh, players=count if valid_count else None,
                      capacity=capacity if type(capacity) is int and capacity > 0 else None,
                      map=str(snap.get('map') or 'Unbekannt'),
                      mode=', '.join(snap.get('experiences', [])) or 'Unbekannt',
                      scores=[{'name': s['name'], 'score': s['score']} for s in scores],
                      label='Online' if fresh else 'Letzte bekannte Daten', ended=bool(state.get('ended')))
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return result


class Repository:
    def __init__(self, db):
        self.db = db
        self.cache = OrderedDict()
        self.cache_lock = asyncio.Lock()

    async def cached(self, key, loader, ttl=20):
        async with self.cache_lock:
            cached = self.cache.get(key)
            if cached and cached[0] > time.monotonic():
                self.cache.move_to_end(key)
                return cached[1]
            value = await loader()
            self.cache[key] = (time.monotonic() + ttl, value)
            self.cache.move_to_end(key)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
            return value

    async def statuses(self):
        async def load():
            return await self.db.query("SELECT item_key,payload FROM durable_state WHERE namespace='round' AND item_key IN ('server1','server2','server3')")
        rows = {row['item_key']: row['payload'] for row in await self.cached(('status',), load, 5)}
        # Recompute age for every response, including cache hits.
        return [server_status(key, rows.get(key)) for key in SERVERS]

    async def leaderboard(self, server, period, sort, page):
        async def load():
            sql, args = ranking_query(server, period, sort)
            return await self.db.query(f'SELECT * FROM ({sql}) ranked ORDER BY player_rank LIMIT 51 OFFSET %s',
                                       (*args, (page - 1) * 50))
        return await self.cached(('ranking', server, period, sort, page), load)

    async def profile(self, steam_id):
        args = (steam_id,)
        stats = await self.db.query('''SELECT server_id,name,lifetime_kills,lifetime_deaths,lifetime_cash,
            current_match_kills,current_match_deaths,current_match_cash,last_seen
            FROM leaderboard WHERE steam_id=%s ORDER BY server_id''', args)
        playtime = await self.db.query('SELECT server_id,name,playtime_seconds,last_seen FROM player_playtime WHERE steam_id=%s', args)
        daily = await self.db.query('''SELECT server_id,
            SUM(CASE WHEN date>=DATE_SUB(UTC_DATE(),INTERVAL 6 DAY) THEN kills ELSE 0 END) kills_7d,
            SUM(CASE WHEN date>=DATE_SUB(UTC_DATE(),INTERVAL 6 DAY) THEN deaths ELSE 0 END) deaths_7d,
            SUM(CASE WHEN date>=DATE_SUB(UTC_DATE(),INTERVAL 6 DAY) THEN cash ELSE 0 END) cash_7d,
            SUM(kills) kills_30d,SUM(deaths) deaths_30d,SUM(cash) cash_30d
            FROM player_daily_stats WHERE steam_id=%s
            AND date BETWEEN DATE_SUB(UTC_DATE(),INTERVAL 29 DAY) AND UTC_DATE() GROUP BY server_id''', args)
        names = await self.db.query('SELECT DISTINCT name FROM player_daily_stats WHERE steam_id=%s ORDER BY name', args)
        factions = await self.db.query('SELECT server_id,faction,times_seen FROM player_faction_stats WHERE steam_id=%s ORDER BY times_seen DESC,faction', args)
        pings = await self.db.query('SELECT server_id,total_ping,ping_samples FROM player_ping_stats WHERE steam_id=%s', args)
        bans = await self.db.query('''SELECT reason,duration_str,issued_at,expires_at,status
            FROM global_bans WHERE steam_id=%s ORDER BY issued_at DESC,id DESC''', args)
        servers = {key: {'id': key, 'title': title, 'factions': [], 'ping': None,
                         'playtime_seconds': 0, 'last_seen': None, 'has_data': False} for key, title in SERVERS.items()}
        known_names = {r['name'] for r in names if r['name']}
        for row in [*stats, *playtime, *daily, *pings]:
            key = row['server_id']
            if key not in servers:
                continue
            target = servers[key]
            old_seen = target.get('last_seen')
            target.update(row, has_data=True)
            if row.get('name'):
                known_names.add(row['name'])
            seen = [t for t in (parse_time(old_seen), parse_time(row.get('last_seen'))) if t]
            target['last_seen'] = max(seen) if seen else None
            if row.get('ping_samples'):
                target['ping'] = row['total_ping'] / row['ping_samples']
        for row in factions:
            if row['server_id'] in servers:
                servers[row['server_id']]['factions'].append(row)
                servers[row['server_id']]['has_data'] = True
        totals = {k: sum((s.get(k) or 0) for s in servers.values())
                  for k in ('lifetime_kills', 'lifetime_deaths', 'lifetime_cash', 'playtime_seconds')}
        totals['kd'] = totals['lifetime_kills'] / (totals['lifetime_deaths'] or 1)
        return {'steam_id': steam_id, 'names': sorted(known_names, key=str.casefold),
                'servers': list(servers.values()), 'totals': totals, 'bans': bans,
                'has_data': any(s['has_data'] for s in servers.values()) or bool(bans)}
