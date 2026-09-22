import asyncio
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from cogs.region_guard import (RegionGuardCog, berlin_day, in_restart_window, restart_allowed,
                               initial_state, response_is_new_enough)


UTC = timezone.utc


def snapshot(players):
    return {'snapshot': {'players': {'current': players}}}


class FakeTracker:
    def __init__(self, value):
        self.value = value

    def current(self, _server_id):
        return self.value


class FakeBot:
    def __init__(self, players=0):
        self.tracker = FakeTracker(snapshot(players))
        self.logs = []
        self.health = SimpleNamespace(ok=lambda *_: None, error=lambda *_: None)

    def get_cog(self, name):
        return self.tracker if name == 'RoundTracker' else None

    def dispatch(self, event, *args):
        self.logs.append((event, args))


class RegionGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = SimpleNamespace(id='server1', title='Server 1', uuid='join-code', enabled=True)
        # 04:00 in Berlin during CEST.
        self.now = datetime(2026, 9, 22, 2, 0, tzinfo=UTC)
        self.memory = {}

    def cog(self, players=0):
        cog = RegionGuardCog.__new__(RegionGuardCog)
        cog.bot = FakeBot(players)
        cog.locks = defaultdict(asyncio.Lock)
        cog.states = {}
        cog.panel_ids = {}
        cog.panel_checked_at = None
        cog.ptero_session = None
        cog.public_session = None
        return cog

    async def _get(self, _namespace, key):
        return self.memory.get(key)

    async def _save(self, _namespace, key, value):
        self.memory[key] = dict(value)

    async def test_window_uses_berlin_time_and_excludes_ten(self):
        self.assertTrue(in_restart_window(self.now))
        self.assertFalse(in_restart_window(datetime(2026, 9, 22, 8, 0, tzinfo=UTC)))
        self.assertEqual(berlin_day(self.now), '2026-09-22')

    async def test_empty_fresh_rcon_starts_only_after_five_minutes(self):
        cog = self.cog(0)
        restarts = []

        async def restart(_srv, state, now):
            restarts.append(now)
            state.update(phase='awaiting_api', restart_at=now.isoformat())
            return True

        cog._restart = restart
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, self.now)
            await cog.tick_server(self.server, self.now + timedelta(seconds=299))
            await cog.tick_server(self.server, self.now + timedelta(seconds=300))
        self.assertEqual(restarts, [self.now + timedelta(seconds=300)])
        self.assertEqual(self.memory['server1']['phase'], 'awaiting_api')

    async def test_player_join_pauses_and_later_empty_resumes(self):
        cog = self.cog(3)
        state = initial_state(berlin_day(self.now))
        state.update(phase='awaiting_api', restart_at=(self.now - timedelta(minutes=1)).isoformat())
        self.memory['server1'] = state
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, self.now)
            self.assertEqual(self.memory['server1']['phase'], 'waiting_empty')
            cog.bot.tracker.value = snapshot(0)
            cog._restart = AsyncMock(return_value=True)
            await cog.tick_server(self.server, self.now + timedelta(minutes=5, seconds=15))
            await cog.tick_server(self.server, self.now + timedelta(minutes=10, seconds=15))
        cog._restart.assert_awaited_once()

    async def test_early_release_still_needs_five_minutes_empty(self):
        early = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)  # 02:00 Europe/Berlin
        cog = self.cog(0)
        state = initial_state(berlin_day(early))
        state['early_enabled'] = True
        self.memory['server1'] = state
        cog._restart = AsyncMock(return_value=True)
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, early)
            await cog.tick_server(self.server, early + timedelta(minutes=5))
        cog._restart.assert_awaited_once()
        self.assertTrue(restart_allowed(self.memory['server1'], early + timedelta(minutes=5)))

    async def test_old_api_timestamp_is_never_processed(self):
        cog = self.cog(0)
        restart_at = self.now
        state = initial_state(berlin_day(restart_at))
        state.update(phase='awaiting_api', restart_at=restart_at.isoformat())
        self.memory['server1'] = state
        cog._server_api = AsyncMock(return_value=(
            {'updated_at': restart_at + timedelta(minutes=3, seconds=59), 'region': 'eu-west'}, None))
        cog._restart = AsyncMock(return_value=True)
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, restart_at + timedelta(minutes=4))
        cog._restart.assert_not_awaited()
        self.assertIn('Zeitstempel', self.memory['server1']['last_error'])

    async def test_fresh_target_region_completes_day(self):
        cog = self.cog(0)
        restart_at = self.now
        state = initial_state(berlin_day(restart_at))
        state.update(phase='awaiting_api', restart_at=restart_at.isoformat())
        self.memory['server1'] = state
        cog._server_api = AsyncMock(return_value=(
            {'updated_at': restart_at + timedelta(minutes=4), 'region': 'eu-central'}, None))
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, restart_at + timedelta(minutes=4))
        self.assertTrue(self.memory['server1']['completed'])
        self.assertEqual(self.memory['server1']['phase'], 'complete')

    async def test_fresh_wrong_region_restarts_when_still_empty(self):
        cog = self.cog(0)
        restart_at = self.now
        state = initial_state(berlin_day(restart_at))
        state.update(phase='awaiting_api', restart_at=restart_at.isoformat())
        self.memory['server1'] = state
        cog._server_api = AsyncMock(return_value=(
            {'updated_at': restart_at + timedelta(minutes=4), 'region': 'eu-west'}, None))
        cog._restart = AsyncMock(return_value=True)
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            await cog.tick_server(self.server, restart_at + timedelta(minutes=4))
        cog._restart.assert_awaited_once()

    async def test_pause_persists_when_a_new_day_starts(self):
        cog = self.cog(0)
        state = initial_state('2026-09-21')
        state.update(paused=True, completed=True, link_key='stable-key')
        self.memory['server1'] = state
        with patch('cogs.region_guard.storage.get_state', self._get), \
             patch('cogs.region_guard.storage.save_state', self._save):
            loaded = await cog.load('server1', self.now)
        self.assertTrue(loaded['paused'])
        self.assertFalse(loaded['completed'])
        self.assertEqual(loaded['link_key'], 'stable-key')

    def test_timestamp_requires_full_four_minutes(self):
        self.assertFalse(response_is_new_enough(self.now + timedelta(seconds=239), self.now))
        self.assertTrue(response_is_new_enough(self.now + timedelta(seconds=240), self.now))


if __name__ == '__main__':
    unittest.main()
