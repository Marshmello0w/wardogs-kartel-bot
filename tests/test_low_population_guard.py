import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from cogs.low_population_guard import LowPopulationGuardCog
from core import config
from services.rcon import RconError


class FakeRcon:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    async def request(self, server_id, method, path, **kwargs):
        self.calls.append((server_id, method, path, kwargs.get('payload')))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return SimpleNamespace(status=200)


class FakeHealth:
    def __init__(self):
        self.errors = []

    def error(self, key, exc):
        self.errors.append((key, exc))

    def ok(self, key):
        pass


class FakeTracker:
    def __init__(self, state):
        self.state = state

    def current(self, server_id):
        return self.state


class FakeBot:
    def __init__(self, state, results=None):
        self.tracker = FakeTracker(state)
        self.rcon = FakeRcon(results)
        self.health = FakeHealth()
        self.logs = []

    def get_cog(self, name):
        return self.tracker if name == 'RoundTracker' else None

    def dispatch(self, event, *args):
        self.logs.append((event, args))


def round_state(players=19, score=1, round_id='round-1', ended=False):
    return dict(round_id=round_id, ended=ended,
                snapshot=dict(players=dict(current=players), highest=score))


class LowPopulationGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = SimpleNamespace(id='server1', title='Server 1', enabled=True)

    def cog(self, state, results=None):
        cog = LowPopulationGuardCog.__new__(LowPopulationGuardCog)
        cog.bot = FakeBot(state, results)
        cog.states = {}
        cog.followup_tasks = set()
        return cog

    async def test_warns_after_five_minutes_and_sends_english_after_ten_seconds(self):
        cog = self.cog(round_state())
        with patch.object(config, 'LOW_POPULATION_ENGLISH_DELAY_SECONDS', 0):
            await cog.tick_server(self.server, now=0)
            await cog.tick_server(self.server, now=299)
            self.assertEqual(cog.bot.rcon.calls, [])
            await cog.tick_server(self.server, now=300)
            state = cog.states['server1']
            await state.english_task
        messages = [call[3]['message'] for call in cog.bot.rcon.calls]
        self.assertEqual(messages, [config.LOW_POPULATION_WARNING_DE, config.LOW_POPULATION_WARNING_EN])
        self.assertEqual(state.deadline, 420)

    async def test_zero_score_and_twenty_players_never_start_a_timer(self):
        zero_score = self.cog(round_state(players=19, score=0))
        await zero_score.tick_server(self.server, now=0)
        self.assertEqual(zero_score.states, {})
        enough_players = self.cog(round_state(players=20, score=1))
        await enough_players.tick_server(self.server, now=0)
        self.assertEqual(enough_players.states, {})

    async def test_recovery_cancels_the_english_warning_and_countdown(self):
        cog = self.cog(round_state())
        with patch.object(config, 'LOW_POPULATION_ENGLISH_DELAY_SECONDS', 60):
            await cog.tick_server(self.server, now=0)
            await cog.tick_server(self.server, now=300)
            english_task = cog.states['server1'].english_task
            cog.bot.tracker.state = round_state(players=20)
            await cog.tick_server(self.server, now=301)
            await asyncio.sleep(0)
        self.assertNotIn('server1', cog.states)
        self.assertTrue(english_task.cancelled())
        messages = [call[3]['message'] for call in cog.bot.rcon.calls]
        self.assertEqual(messages, [config.LOW_POPULATION_WARNING_DE, config.LOW_POPULATION_RECOVERED_DE])

    async def test_recovery_sends_an_english_followup(self):
        cog = self.cog(round_state())
        with patch.object(config, 'LOW_POPULATION_ENGLISH_DELAY_SECONDS', 0):
            await cog.tick_server(self.server, now=0)
            await cog.tick_server(self.server, now=300)
            cog.bot.tracker.state = round_state(players=20)
            await cog.tick_server(self.server, now=301)
            recovery_task = next(iter(cog.followup_tasks))
            await recovery_task
        messages = [call[3]['message'] for call in cog.bot.rcon.calls]
        self.assertEqual(messages[-2:], [config.LOW_POPULATION_RECOVERED_DE, config.LOW_POPULATION_RECOVERED_EN])

    async def test_force_end_after_the_countdown(self):
        cog = self.cog(round_state())
        with patch.object(config, 'LOW_POPULATION_ENGLISH_DELAY_SECONDS', 0):
            await cog.tick_server(self.server, now=0)
            await cog.tick_server(self.server, now=300)
            await cog.states['server1'].english_task
            await cog.tick_server(self.server, now=420)
        self.assertEqual(cog.bot.rcon.calls[-2][3]['message'], config.LOW_POPULATION_ENDING_DE)
        self.assertEqual(cog.bot.rcon.calls[-1][1:3], ('POST', '/v1/match/end'))
        self.assertTrue(any(args[0] == '⏹️ Runde automatisch beendet' for event, args in cog.bot.logs if event == 'bot_log'))

    async def test_round_change_clears_countdown_without_ending(self):
        cog = self.cog(round_state())
        with patch.object(config, 'LOW_POPULATION_ENGLISH_DELAY_SECONDS', 60):
            await cog.tick_server(self.server, now=0)
            await cog.tick_server(self.server, now=300)
            cog.bot.tracker.state = round_state(round_id='round-2')
            await cog.tick_server(self.server, now=301)
            await asyncio.sleep(0)
        self.assertEqual(cog.states['server1'].round_id, 'round-2')
        self.assertFalse(cog.states['server1'].warning_sent)
        self.assertEqual(cog.states['server1'].low_since, 301)
        self.assertFalse(any(call[2] == '/v1/match/end' for call in cog.bot.rcon.calls))

    async def test_uncertain_force_end_is_not_repeated(self):
        cog = self.cog(round_state(), results=[SimpleNamespace(status=200), SimpleNamespace(status=200),
                                                RconError(202, uncertain=True)])
        with patch.object(config, 'LOW_POPULATION_ENGLISH_DELAY_SECONDS', 60):
            await cog.tick_server(self.server, now=0)
            await cog.tick_server(self.server, now=300)
            await cog.tick_server(self.server, now=420)
            await cog.tick_server(self.server, now=425)
        end_calls = [call for call in cog.bot.rcon.calls if call[2] == '/v1/match/end']
        self.assertEqual(len(end_calls), 1)
