"""Durable desired-state bans. No RCON change precedes a committed admin action."""
import asyncio
from collections import defaultdict
from datetime import timedelta
import hashlib
import json
import re
import discord
import config
import database
from permissions import valid_steam_id
from rcon import RconError
from runtime import read_state
from storage import utcnow


def config_banned_ids(text):
    return set(re.findall(r'^\s*[.+]?DefaultBannedPlayerIds\s*=\s*"?([0-9]{17})"?\s*$', text, re.MULTILINE))


def remove_config_ban(text, steam_id):
    pattern = re.compile(r'^\s*[.+]?DefaultBannedPlayerIds\s*=\s*"?' + re.escape(steam_id) + r'"?\s*$')
    return ''.join(line for line in text.splitlines(keepends=True) if not pattern.fullmatch(line.rstrip('\r\n')))


class BanService:
    def __init__(self, bot):
        self.bot = bot
        self.locks = defaultdict(asyncio.Lock)
        self.worker_lock = asyncio.Lock()
        self.init_lock = asyncio.Lock()
        self.initialized = False

    async def initialize(self):
        async with self.init_lock:
            await self._initialize()

    async def _initialize(self):
        if self.initialized:
            return
        async with database.transaction() as cur:
            # Import only the most recent legacy decision. Never manufacture new bans
            # from a temporarily stale gameserver list.
            await cur.execute("""SELECT b.* FROM global_bans b JOIN
                (SELECT steam_id, MAX(id) id FROM global_bans GROUP BY steam_id) latest ON b.id=latest.id""")
            for row in await cur.fetchall():
                await cur.execute("""INSERT IGNORE INTO admin_targets
                    (steam_id,version,desired,reason,admin_mention,expires_at,ban_id)
                    VALUES (%s,1,%s,%s,%s,%s,%s)""",
                    (row['steam_id'], 'ban' if row['status'] == 'active' else 'unban',
                     row['reason'], row['admin_mention'], row['expires_at'], row['id']))
        actions = read_state('pending_admin_actions.json', [])
        if not isinstance(actions, list):
            raise ValueError('Invalid legacy queue')
        for action in actions:
            fingerprint = hashlib.sha256(json.dumps(action, sort_keys=True).encode()).hexdigest()
            async with database.transaction() as cur:
                await cur.execute("SELECT fingerprint FROM legacy_admin_import WHERE fingerprint=%s", (fingerprint,))
                if await cur.fetchone():
                    continue
                await cur.execute("SELECT * FROM admin_targets WHERE steam_id=%s FOR UPDATE", (action.get('steam_id'),))
                target = await cur.fetchone()
                status = 'needs_review'
                if target and action.get('action') == target['desired']:
                    if target['desired'] == 'ban' and target['expires_at'] and target['expires_at'] <= utcnow():
                        status = 'expired'
                    elif action.get('server_id') in {s.id for s in config.servers()}:
                        await self._job(cur, target, action['server_id'])
                        status = 'imported'
                elif target:
                    status = 'superseded'
                elif action.get('action') == 'unban' and valid_steam_id(str(action.get('steam_id', ''))):
                    target = dict(steam_id=action['steam_id'], version=1, desired='unban')
                    await cur.execute("""INSERT IGNORE INTO admin_targets
                        (steam_id,version,desired,reason,admin_mention) VALUES (%s,1,'unban','',%s)""",
                        (action['steam_id'], str(action.get('admin_mention', 'Legacy'))[:100]))
                    if action.get('server_id') in {s.id for s in config.servers()}:
                        await self._job(cur, target, action['server_id'])
                        status = 'imported'
                await cur.execute("INSERT INTO legacy_admin_import VALUES (%s,%s,%s)",
                                  (fingerprint, json.dumps(action), status))
            if status == 'needs_review':
                self.bot.dispatch('bot_log', '⚠️ Alter Ban-Auftrag prüfen',
                                  f"Import zurückgestellt: {fingerprint[:12]}", discord.Color.orange())
        self.initialized = True

    async def _job(self, cur, target, server_id):
        await cur.execute("""INSERT IGNORE INTO admin_jobs(steam_id,server_id,version,action)
            VALUES (%s,%s,%s,%s)""", (target['steam_id'], server_id, target['version'], target['desired']))

    async def decide(self, steam_id, action, reason, admin, hours=0, label='Permanent'):
        if not valid_steam_id(steam_id) or action not in ('ban', 'unban'):
            raise ValueError('Invalid admin action')
        await self.initialize()
        async with self.locks[steam_id]:
            async with database.transaction() as cur:
                await cur.execute("""INSERT IGNORE INTO admin_targets
                    (steam_id,version,desired,reason,admin_mention) VALUES (%s,0,'unban','',%s)""", (steam_id, admin))
                await cur.execute('SELECT * FROM admin_targets WHERE steam_id=%s FOR UPDATE', (steam_id,))
                target = await cur.fetchone()
                version = target['version'] + 1
                expires = utcnow() + timedelta(hours=hours) if action == 'ban' and hours > 0 else None
                await cur.execute("UPDATE global_bans SET status='revoked' WHERE steam_id=%s AND status='active'", (steam_id,))
                ban_id = None
                if action == 'ban':
                    await cur.execute("""INSERT INTO global_bans
                        (steam_id,reason,admin_mention,duration_str,expires_at) VALUES (%s,%s,%s,%s,%s)""",
                        (steam_id, reason, admin, label, expires))
                    ban_id = cur.lastrowid
                await cur.execute("""UPDATE admin_targets SET version=%s,desired=%s,reason=%s,
                    admin_mention=%s,expires_at=%s,ban_id=%s,updated_at=UTC_TIMESTAMP() WHERE steam_id=%s""",
                    (version, action, reason, admin, expires, ban_id, steam_id))
                await cur.execute("UPDATE admin_jobs SET status='superseded' WHERE steam_id=%s AND status='pending'", (steam_id,))
                for srv in config.servers():
                    await self._job(cur, dict(steam_id=steam_id, version=version, desired=action), srv.id)
        self.bot.dispatch('bot_log', '🛡️ Admin-Aktion gespeichert',
                          f"**Admin:** {admin}\n**SteamID:** `{steam_id}`\n**Aktion:** {action}\n"
                          f"**Dauer:** {label if action == 'ban' else '—'}\n**Grund:** {reason[:1500]}", discord.Color.blue())
        return version

    async def expire(self):
        async with database.transaction() as cur:
            await cur.execute("SELECT steam_id FROM admin_targets WHERE desired='ban' AND expires_at<=UTC_TIMESTAMP()")
            ids = [r['steam_id'] for r in await cur.fetchall()]
        for steam_id in ids:
            async with self.locks[steam_id]:
                async with database.transaction() as cur:
                    await cur.execute('SELECT * FROM admin_targets WHERE steam_id=%s FOR UPDATE', (steam_id,))
                    target = await cur.fetchone()
                    if target['desired'] != 'ban' or not target['expires_at'] or target['expires_at'] > utcnow():
                        continue
                    target.update(version=target['version'] + 1, desired='unban')
                    await cur.execute("UPDATE admin_targets SET version=%s,desired='unban',updated_at=UTC_TIMESTAMP() WHERE steam_id=%s",
                                      (target['version'], steam_id))
                    await cur.execute("UPDATE global_bans SET status='expired' WHERE id=%s AND status='active'", (target['ban_id'],))
                    await cur.execute("UPDATE admin_jobs SET status='superseded' WHERE steam_id=%s AND status='pending'", (steam_id,))
                    for srv in config.servers():
                        await self._job(cur, target, srv.id)
                self.bot.dispatch('bot_log', '⏳ Ban abgelaufen',
                                  f"SteamID `{steam_id}`: Entbannung auf allen Servern vorgemerkt.", discord.Color.blue())

    async def apply(self, job):
        server_id, steam_id = job['server_id'], job['steam_id']
        bans = await self.bot.rcon.bans(server_id)
        if job['action'] == 'ban':
            if job['expires_at'] and job['expires_at'] <= utcnow():
                return False
            if steam_id in bans:
                return True
            try:
                await self.bot.rcon.request(server_id, 'POST', '/v1/bans',
                                            payload=dict(steamId=steam_id, reason=job['reason']),
                                            guard=lambda: not job['expires_at'] or job['expires_at'] > utcnow())
            except RconError as exc:
                if not exc.uncertain:
                    raise
            return steam_id in await self.bot.rcon.bans(server_id)
        # Config-only bans need the same path for manual, automatic and retried unbans.
        try:
            await self.bot.rcon.request(server_id, 'DELETE', f'/v1/bans/{steam_id}')
        except RconError as exc:
            if exc.status != 404 and not exc.uncertain:
                raise
        document = await self.bot.rcon.get(server_id, '/v1/config')
        if not isinstance(document.get('text'), str):
            raise RconError()
        if steam_id in config_banned_ids(document['text']):
            await self.bot.rcon.edit_config(server_id, lambda text: remove_config_ban(text, steam_id))
        return steam_id not in await self.bot.rcon.bans(server_id)

    async def process_job(self, queued):
        async with self.locks[queued['steam_id']]:
            async with database.transaction() as cur:
                await cur.execute("""SELECT j.*,t.reason,t.expires_at,t.version target_version,t.desired
                    FROM admin_jobs j JOIN admin_targets t ON t.steam_id=j.steam_id WHERE j.id=%s""", (queued['id'],))
                job = await cur.fetchone()
            if not job or job['status'] != 'pending':
                return
            if job['version'] != job['target_version'] or job['action'] != job['desired']:
                return
            if job['action'] == 'ban' and job['expires_at'] and job['expires_at'] <= utcnow():
                return
            key = f"Admin-Aufträge {config.server(job['server_id']).title}"
            error = None
            try:
                success = await self.apply(job)
                if not success:
                    raise RconError(uncertain=True)
                self.bot.health.ok(key)
            except Exception as exc:
                error = f'HTTP {exc.status}' if isinstance(exc, RconError) and exc.status else type(exc).__name__
                self.bot.health.error(key, exc)
            async with database.transaction() as cur:
                await cur.execute("""UPDATE admin_jobs SET status=%s,attempts=attempts+1,
                    next_attempt=%s,last_error=%s WHERE id=%s AND status='pending'""",
                    ('pending' if error else 'done', utcnow() + timedelta(seconds=min(300, 5 * 2 ** min(job['attempts'], 6))), error, job['id']))
            if not error:
                self.bot.dispatch('bot_log', '✅ Admin-Auftrag ausgeführt',
                                  f"{config.server(job['server_id']).title}: {job['action']} für `{job['steam_id']}`",
                                  discord.Color.blue())

    async def tick(self):
        async with self.worker_lock:
            await self.initialize()
            await self.expire()
            async with database.transaction() as cur:
                await cur.execute("""SELECT id,steam_id,server_id FROM admin_jobs WHERE status='pending'
                    AND next_attempt<=UTC_TIMESTAMP() ORDER BY id LIMIT 30""")
                jobs = await cur.fetchall()
            # Bound concurrency and preserve order for a player's successive decisions.
            for offset in range(0, len(jobs), 3):
                results = await asyncio.gather(*(self.process_job(j) for j in jobs[offset:offset+3]), return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        self.bot.health.error('Admin-Auftragsverarbeitung', result)
