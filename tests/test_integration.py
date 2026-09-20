"""Opt-in tests against a disposable LOCAL MariaDB, never the project's .env."""
import asyncio
from collections import defaultdict
from datetime import timedelta
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4
import tempfile
import json
from contextlib import redirect_stdout
from io import StringIO

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import pymysql
from core import config
from core.runtime import Health
from domain import stats
from domain.rounds import advance
from infrastructure import database, storage
from services.ban_service import BanService
from cogs.round_tracker import RoundTracker
from cogs.map_vote import MapVoteCog
from services.rcon import RconError, Reply

STEAM = '76561190000000001'
OTHER = '76561190000000002'


class FakeRcon:
    def __init__(self):
        self.list = defaultdict(set)
        self.calls = []

    async def bans(self, srv):
        return set(self.list[srv])

    async def request(self, srv, method, path, payload=None, guard=None):
        if guard is not None and not guard():
            raise RconError(409)
        self.calls.append((srv, method, path))
        if method == 'POST':
            self.list[srv].add(payload['steamId'])
        elif method == 'DELETE':
            self.list[srv].discard(path.rsplit('/', 1)[-1])
        return Reply(200, {})

    async def get(self, srv, path):
        return {'text': '', 'revision': '1'}


@unittest.skipUnless(os.getenv('KARTEL_TEST_DB_PORT'), 'Set KARTEL_TEST_DB_PORT for isolated local MariaDB tests')
class MysqlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 2
        database._global_pool = None
        database._pool_lock = asyncio.Lock()
        self.port = int(os.environ['KARTEL_TEST_DB_PORT'])
        self.dbname = 'kartelbot_test_' + uuid4().hex
        self.admin = pymysql.connect(host='127.0.0.1', port=self.port, user='root', password='local-test-only', autocommit=True)
        with self.admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE `{self.dbname}` CHARACTER SET utf8mb4')
        self.url_patch = patch.object(config, 'DB_CONNECTION_URL', f'mysql://root:local-test-only@127.0.0.1:{self.port}/{self.dbname}')
        self.url_patch.start()
        self.pool = await database.get_db_pool()
        self.assertIsNotNone(self.pool)
        self.bot = SimpleNamespace(rcon=FakeRcon(), dispatch=Mock())
        self.bot.health = Health(self.bot)
        self.legacy_patch = patch('services.ban_service.read_state', return_value=[])
        self.legacy_patch.start()
        self.service = BanService(self.bot)

    async def asyncTearDown(self):
        await database.close_pool()
        self.url_patch.stop()
        self.legacy_patch.stop()
        with self.admin.cursor() as cur:
            cur.execute(f'DROP DATABASE `{self.dbname}`')
        self.admin.close()

    async def rows(self, sql, params=()):
        async with database.transaction() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def test_migration_repeated_preserves_history(self):
        await self.service.decide(STEAM, 'ban', 'reason', 'admin', 24, '24 Stunden')
        await database.init_db(self.pool)
        self.assertEqual(len(await self.rows('SELECT * FROM global_bans')), 1)
        self.assertEqual(len(await self.rows('SELECT * FROM schema_migrations')), 1)

    async def test_expired_offline_ban_never_posts(self):
        await self.service.decide(STEAM, 'ban', 'reason', 'admin', 24, '24 Stunden')
        await self.rows('UPDATE admin_targets SET expires_at=%s WHERE steam_id=%s', (storage.utcnow()-timedelta(seconds=1), STEAM))
        await self.service.tick()
        self.assertFalse(any(method == 'POST' for _,method,_ in self.bot.rcon.calls))
        jobs = await self.rows('SELECT action,status FROM admin_jobs')
        self.assertEqual(sum(j['action'] == 'ban' and j['status'] == 'superseded' for j in jobs), 3)
        self.assertEqual(sum(j['action'] == 'unban' and j['status'] == 'done' for j in jobs), 3)

    async def test_unban_supersedes_old_ban_and_restart_preserves_jobs(self):
        await self.service.decide(STEAM, 'ban', 'reason', 'admin')
        await self.service.decide(STEAM, 'unban', '', 'admin')
        restarted = BanService(self.bot)
        await restarted.tick()
        self.assertFalse(any(method == 'POST' for _,method,_ in self.bot.rcon.calls))
        self.assertEqual(len(await self.rows("SELECT * FROM admin_jobs WHERE status='done'")), 3)

    async def test_new_action_during_network_wait_is_retained(self):
        await self.service.decide(STEAM, 'ban', 'reason', 'admin')
        jobs = await self.rows('SELECT * FROM admin_jobs ORDER BY id')
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.service.apply

        async def waiting(job):
            entered.set()
            await release.wait()
            return await original(job)

        with patch.object(self.service, 'apply', waiting):
            task = asyncio.create_task(self.service.process_job(jobs[0]))
            await entered.wait()
            await self.service.decide(OTHER, 'ban', 'new', 'admin')
            unban = asyncio.create_task(self.service.decide(STEAM, 'unban', '', 'admin'))
            release.set()
            await asyncio.gather(task, unban)
        self.assertEqual(len(await self.rows("SELECT * FROM admin_jobs WHERE steam_id=%s AND status='pending'", (OTHER,))), 3)
        await self.service.tick()
        self.assertNotIn(STEAM, self.bot.rcon.list['server1'])

    async def test_write_failure_never_calls_server(self):
        await self.service.initialize()
        with patch.object(self.service, '_job', AsyncMock(side_effect=RuntimeError('failed commit'))):
            with self.assertRaises(RuntimeError):
                await self.service.decide(STEAM, 'ban', 'reason', 'admin')
        self.assertFalse(self.bot.rcon.calls)
        self.assertFalse(await self.rows('SELECT * FROM global_bans'))

    async def test_partial_server_failure_keeps_one_pending_job(self):
        await self.service.decide(STEAM, 'ban', 'reason', 'admin')
        original = self.service.apply

        async def failure(job):
            if job['server_id'] == 'server2':
                raise RconError(503)
            return await original(job)

        with patch.object(self.service, 'apply', failure):
            await self.service.tick()
        pending = await self.rows("SELECT * FROM admin_jobs WHERE status='pending'")
        self.assertEqual([j['server_id'] for j in pending], ['server2'])

    async def test_legacy_unlinked_ban_requires_review(self):
        action = dict(action='ban', server_id='server1', steam_id=STEAM, reason='unknown')
        with patch('services.ban_service.read_state', return_value=[action]):
            await self.service.initialize()
            await BanService(self.bot).initialize()
        self.assertEqual(len(await self.rows('SELECT * FROM legacy_admin_import')), 1)
        self.assertEqual((await self.rows('SELECT status FROM legacy_admin_import'))[0]['status'], 'needs_review')
        self.assertFalse(await self.rows('SELECT * FROM admin_jobs'))

    async def test_round_counter_and_matching_rankings(self):
        partial = dict(round_id='r0', started_at=None, quality='partial')
        observed = dict(round_id='r1', started_at='2026-09-20T10:00:00', quality='observed')
        player = dict(steamId=STEAM, name='Player', kills=10, deaths=5, cash=100)
        await stats.update_players('server1', partial, [player])
        await stats.update_players('server1', observed, [dict(player, kills=12,deaths=6,cash=120)])
        public = await stats.ranking('server1')
        personal = await stats.ranking('server1', steam_id=STEAM)
        self.assertEqual(public[0], personal)
        self.assertEqual(personal['kills'], 12)
        self.assertEqual((await stats.ranking('server1','7d',steam_id=STEAM))['kills'], 12)
        await self.rows('INSERT INTO banned_players VALUES (%s,%s,TRUE)', ('server1',STEAM))
        self.assertFalse(await stats.ranking('server1'))
        self.assertIsNone(await stats.ranking('server1',steam_id=STEAM))

    async def test_failed_daily_insert_rolls_back_lifetime_and_baseline(self):
        state = dict(round_id='r1', started_at='2026-09-20T10:00:00', quality='observed')
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""CREATE TRIGGER fail_daily BEFORE INSERT ON player_daily_stats
                    FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='simulated failure'""")
        with self.assertRaises(Exception):
            await stats.update_players('server1', state, [dict(steamId=STEAM,name='x',kills=1,deaths=1,cash=1)])
        self.assertFalse(await self.rows('SELECT * FROM leaderboard'))
        self.assertFalse(await self.rows('SELECT * FROM player_counter_state'))

    async def test_seven_calendar_days_excludes_eighth_day(self):
        await self.rows('INSERT INTO leaderboard(server_id,steam_id,name) VALUES (%s,%s,%s)', ('server1',STEAM,'Player'))
        await self.rows("""INSERT INTO player_daily_stats(server_id,steam_id,name,date,kills)
            VALUES (%s,%s,'Player',DATE_SUB(UTC_DATE(),INTERVAL 7 DAY),100),
            (%s,%s,'Player',DATE_SUB(UTC_DATE(),INTERVAL 6 DAY),10)""", ('server1',STEAM,'server1',STEAM))
        self.assertEqual((await stats.ranking('server1','7d',steam_id=STEAM))['kills'], 10)

    async def test_ban_expires_during_preflight_read(self):
        await self.service.decide(STEAM, 'ban', 'reason', 'admin', 24, '24 Stunden')
        job = (await self.rows("""SELECT j.*, t.expires_at,t.reason FROM admin_jobs j
            JOIN admin_targets t ON t.steam_id=j.steam_id LIMIT 1"""))[0]
        async def preflight(srv):
            job['expires_at'] = storage.utcnow() - timedelta(seconds=1)
            return set()
        self.bot.rcon.bans = preflight
        self.assertFalse(await self.service.apply(job))
        self.assertFalse(self.bot.rcon.calls)

    async def test_playtime_uses_elapsed_time_and_skips_gap(self):
        from cogs.stats_tracker import StatsTracker
        cog = StatsTracker.__new__(StatsTracker)
        cog.bot, cog.last_online, cog.last_observed = self.bot, {}, {}
        self.bot.rcon.players = AsyncMock(return_value=[dict(steamId=STEAM,name='Player',faction='A',pingMs=20)])
        with patch('cogs.stats_tracker.monotonic', return_value=100):
            await cog.sample(config.server('server1'))
        with patch('cogs.stats_tracker.monotonic', return_value=162):
            await cog.sample(config.server('server1'))
        with patch('cogs.stats_tracker.monotonic', return_value=362):
            await cog.sample(config.server('server1'))
        row = (await self.rows('SELECT * FROM player_playtime'))[0]
        self.assertEqual(row['playtime_seconds'], 62)

    async def test_round_events_and_results_survive_reload_without_duplicates(self):
        tracker = RoundTracker.__new__(RoundTracker)
        tracker.bot, tracker.states, tracker.locks = self.bot, {}, defaultdict(asyncio.Lock)
        raw = dict(map='Bakurani',experiences=['KOTH'],factionScores=[dict(name='A',score=100)])
        self.bot.rcon.get = AsyncMock(return_value=raw)
        await tracker.observe('server1')
        tracker.states = {}
        await tracker.observe('server1')
        self.assertEqual(len(await self.rows('SELECT * FROM round_history')), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM round_events WHERE kind='round_ended'")), 1)

    async def test_round_change_during_player_fetch_discards_sample(self):
        tracker = RoundTracker.__new__(RoundTracker)
        tracker.bot = self.bot
        tracker.observe = AsyncMock(side_effect=[dict(round_id='a', uncertain=False), dict(round_id='b', uncertain=False)])
        self.bot.rcon.players = AsyncMock(return_value=[dict(steamId=STEAM)])
        self.assertIsNone(await tracker.sample_players('server1'))

    async def test_voting_retry_retains_applied_change_until_cleanup_succeeds(self):
        cog = MapVoteCog.__new__(MapVoteCog)
        cog.bot, cog.states = self.bot, {}
        state = dict(change=dict(round_id='old',before=['A'],after=['X','A'],status='applied'),winner='Bakurani')
        current = dict(round_id='new',snapshot=dict(highest=1))
        self.bot.rcon.edit_config = AsyncMock(side_effect=RconError(503))
        with self.assertRaises(RconError):
            await cog.cleanup(config.server('server2'), state, current)
        self.assertIsNotNone(state['change'])
        self.bot.rcon.edit_config = AsyncMock()
        await cog.cleanup(config.server('server2'), state, current)
        self.assertIsNone((await storage.get_state('voting','server2'))['change'])

    async def test_winning_vote_retries_then_restores_original_rotation(self):
        from domain.rotation import entries
        text = '[Other]\nX=1\n[/Script/WDGame.WDServerMapRotationSettings]\n!RotationEntries=ClearArray\n.RotationEntries=Old\n'
        document = {'text': text, 'revision': '1'}
        self.bot.rcon.get = AsyncMock(side_effect=lambda *args: dict(document))
        fail = True
        async def edit(server_id, transform):
            nonlocal fail
            if fail:
                fail = False
                raise RconError(503)
            document['text'] = transform(document['text'])
        self.bot.rcon.edit_config = edit
        current = dict(round_id='r1', voting_closed=True, ended=False, snapshot=dict(highest=95, rotation=dict(nextIndex=0)))
        self.bot.get_cog = lambda _: SimpleNamespace(current=lambda _: current)
        self.bot.add_view = Mock()
        cog = MapVoteCog.__new__(MapVoteCog)
        cog.bot, cog.states, cog.locks = self.bot, {}, defaultdict(asyncio.Lock)
        cog.panel = AsyncMock()
        await storage.save_state('voting','server2', dict(enabled=True,msg_id=123,
            votes={str(i):'Bakurani' for i in range(5)},locked=False,round_id='r1',change=None,winner=None,outcome=None))
        with self.assertRaises(RconError):
            await cog.tick_server(config.server('server2'))
        pending = await storage.get_state('voting','server2')
        self.assertEqual(pending['change']['status'], 'pending')
        self.assertEqual(pending['winner'], 'Bakurani')
        await cog.tick_server(config.server('server2'))
        applied = await storage.get_state('voting','server2')
        self.assertEqual(applied['change']['status'], 'applied')
        self.assertEqual(len(entries(document['text'])), 2)
        current = dict(round_id='r2',voting_closed=False,ended=False,snapshot=dict(highest=1))
        await cog.tick_server(config.server('server2'))
        restored = await storage.get_state('voting','server2')
        self.assertIsNone(restored['change'])
        self.assertEqual(restored['votes'], {})
        self.assertEqual(entries(document['text']), ['.RotationEntries=Old'])

    async def test_external_ban_removal_and_reban_are_distinguishable(self):
        from cogs.ban_tracker import BanTracker
        cog = BanTracker.__new__(BanTracker)
        cog.bot = self.bot
        srv = config.server('server1')
        await cog.poll_server(srv)
        self.bot.rcon.list[srv.id].add(STEAM)
        self.bot.rcon.request = AsyncMock(return_value=Reply(200,{}))
        await cog.poll_server(srv)
        self.assertEqual(self.bot.rcon.request.await_count, 1)
        self.bot.rcon.list[srv.id].clear()
        await cog.poll_server(srv)
        self.bot.rcon.list[srv.id].add(STEAM)
        await cog.poll_server(srv)
        self.assertEqual(self.bot.rcon.request.await_count, 2)
        history = await self.rows('SELECT status FROM global_bans ORDER BY id')
        self.assertEqual([r['status'] for r in history], ['external_removed','external'])
        await self.service.initialize()
        self.assertFalse(await self.rows('SELECT * FROM admin_targets'))

    async def test_maintenance_export_uses_current_jobs_and_preserves_history(self):
        import maintenance
        await self.service.decide(STEAM, 'ban', 'old', 'admin')
        await self.service.decide(STEAM, 'unban', '', 'admin')
        with tempfile.TemporaryDirectory(prefix='kartelbot-export-test-') as folder:
            target = Path(folder) / 'export'
            with redirect_stdout(StringIO()):
                await maintenance.run(SimpleNamespace(command='export-state', output=str(target)))
            pending = json.loads((target / 'pending_admin_actions.json').read_text(encoding='utf-8'))
            self.assertEqual(len(pending), 3)
            self.assertTrue(all(action['action'] == 'unban' for action in pending))
            self.assertTrue((target / 'round_state.json').exists())
        self.assertEqual(len(await self.rows('SELECT * FROM global_bans')), 1)
