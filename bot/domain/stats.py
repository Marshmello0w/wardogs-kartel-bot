"""Transactional round-aware counters and a single ranking definition."""
import asyncio
from collections import defaultdict
from core.permissions import valid_steam_id
from domain.rounds import counter_delta
from infrastructure import database

_locks = defaultdict(asyncio.Lock)


async def update_players(server_id, state, players):
    async with _locks[server_id]:
        # The complete batch is committed together. A failed sample cannot leave the
        # lifetime counters ahead of the daily counters or their measurement baseline.
        async with database.transaction() as cur:
            for player in players:
                steam_id = str(player.get('steamId', ''))
                if not valid_steam_id(steam_id):
                    continue
                values = tuple(player.get(k) for k in ('kills', 'deaths', 'cash'))
                if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
                    continue
                name = str(player.get('name', 'Unknown'))[:255]
                await cur.execute('SELECT * FROM player_counter_state WHERE server_id=%s AND steam_id=%s FOR UPDATE', (server_id, steam_id))
                previous = await cur.fetchone()
                added, quality = counter_delta(previous, values, state['round_id'],
                                                state.get('started_at') is not None and state['quality'] == 'observed')
                await cur.execute("""INSERT INTO leaderboard
                    (server_id,steam_id,name,lifetime_kills,lifetime_deaths,lifetime_cash,
                     current_match_kills,current_match_deaths,current_match_cash)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                    name=VALUES(name), lifetime_kills=lifetime_kills+VALUES(lifetime_kills),
                    lifetime_deaths=lifetime_deaths+VALUES(lifetime_deaths),
                    lifetime_cash=lifetime_cash+VALUES(lifetime_cash), current_match_kills=VALUES(current_match_kills),
                    current_match_deaths=VALUES(current_match_deaths),current_match_cash=VALUES(current_match_cash),
                    last_seen=UTC_TIMESTAMP()""", (server_id, steam_id, name, *added, *values))
                if any(added):
                    await cur.execute("""INSERT INTO player_daily_stats(server_id,steam_id,date,name,kills,deaths,cash)
                        VALUES (%s,%s,UTC_DATE(),%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                        name=VALUES(name),kills=kills+VALUES(kills),deaths=deaths+VALUES(deaths),cash=cash+VALUES(cash)""",
                        (server_id, steam_id, name, *added))
                await cur.execute("""INSERT INTO player_counter_state(server_id,steam_id,round_id,kills,deaths,cash,quality)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                    round_id=VALUES(round_id),kills=VALUES(kills),deaths=VALUES(deaths),cash=VALUES(cash),quality=VALUES(quality)""",
                    (server_id, steam_id, state['round_id'], *values, quality))


def ranking_query(server_id, timeframe='all', sort_by='kd'):
    if timeframe not in ('all', '7d', '30d') or sort_by not in ('kd', 'cash', 'playtime'):
        raise ValueError('Invalid ranking')
    exclusion = """AND NOT EXISTS (SELECT 1 FROM banned_players b WHERE b.server_id=l.server_id AND b.steam_id=l.steam_id)
        AND NOT EXISTS (SELECT 1 FROM admin_targets t WHERE t.steam_id=l.steam_id AND t.desired='ban'
        AND (t.expires_at IS NULL OR t.expires_at>UTC_TIMESTAMP()))"""
    qualification = ''
    if sort_by == 'kd':
        # Cash and playtime remain available to every tracked player.
        qualification = 'AND totals.deaths>=5 AND totals.qualifying_playtime_seconds>=1800'
    if timeframe == 'all':
        base = f"""SELECT l.steam_id,l.name,l.lifetime_kills kills,l.lifetime_deaths deaths,l.lifetime_cash cash,
            p.playtime_seconds,p.playtime_seconds qualifying_playtime_seconds,0 legacy_seconds
            FROM leaderboard l JOIN player_playtime p
            ON p.server_id=l.server_id AND p.steam_id=l.steam_id
            WHERE l.server_id=%s {exclusion}"""
    else:
        days = 6 if timeframe == '7d' else 29
        base = f"""SELECT l.steam_id,l.name,COALESCE(d.kills,0) kills,COALESCE(d.deaths,0) deaths,
            COALESCE(d.cash,0) cash,COALESCE(dp.playtime_seconds,0) playtime_seconds,
            p.playtime_seconds qualifying_playtime_seconds,COALESCE(dp.legacy_seconds,0) legacy_seconds
            FROM leaderboard l JOIN player_playtime p
            ON p.server_id=l.server_id AND p.steam_id=l.steam_id
            LEFT JOIN (SELECT server_id,steam_id,SUM(kills) kills,SUM(deaths) deaths,SUM(cash) cash
                FROM player_daily_stats WHERE date BETWEEN DATE_SUB(UTC_DATE(),INTERVAL {days} DAY)
                AND UTC_DATE() GROUP BY server_id,steam_id) d
            ON d.server_id=l.server_id AND d.steam_id=l.steam_id
            LEFT JOIN (SELECT server_id,steam_id,SUM(playtime_seconds) playtime_seconds,
                SUM(legacy_seconds) legacy_seconds FROM player_daily_playtime
                WHERE date BETWEEN DATE_SUB(UTC_DATE(),INTERVAL {days} DAY) AND UTC_DATE()
                GROUP BY server_id,steam_id) dp
            ON dp.server_id=l.server_id AND dp.steam_id=l.steam_id
            WHERE l.server_id=%s {exclusion} AND (d.steam_id IS NOT NULL OR dp.steam_id IS NOT NULL)"""
    order = {'kd':'kd DESC,kills DESC,steam_id ASC',
             'cash':'cash DESC,kills DESC,steam_id ASC',
             'playtime':'playtime_seconds DESC,kills DESC,steam_id ASC'}[sort_by]
    return f"""SELECT scored.*,ROW_NUMBER() OVER (ORDER BY {order}) AS player_rank
        FROM (SELECT totals.*,kills / IF(deaths=0,1,deaths) AS kd FROM ({base}) totals
        WHERE 1=1 {qualification}) scored""", (server_id,)


async def ranking(server_id, timeframe='all', sort_by='kd', steam_id=None):
    sql, args = ranking_query(server_id, timeframe, sort_by)
    async with database.transaction() as cur:
        if steam_id:
            await cur.execute(f'SELECT * FROM ({sql}) ranked WHERE steam_id=%s', (*args, steam_id))
            return await cur.fetchone()
        await cur.execute(f'SELECT * FROM ({sql}) ranked ORDER BY player_rank LIMIT 10', args)
        return await cur.fetchall()
