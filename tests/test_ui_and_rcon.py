import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import discord
from core import config
from cogs.admin_panel import AdminPanelCog
from cogs.leaderboard import PublicLeaderboardDropdown
from cogs.map_vote import MapVoteCog, _panel_changed
from cogs.round_tracker import RoundTracker
from cogs.player_lookup import PlayerLookupCog, unique_matches
from cogs.server_recap import current_day_window, player_graph, previous_day_window
from cogs.server_status import format_scores
from core.runtime import Health
from services.rcon import RconClient, RconError, Reply
from start import KartelBot


class RconTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_reads_are_singleflight(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def text(self):
                entered.set()
                await release.wait()
                return '{"players": []}'

        class Session:
            closed = False

            def __init__(self):
                self.calls = 0

            def request(self, *args, **kwargs):
                self.calls += 1
                return Response()

        client = RconClient()
        client.session = Session()
        server = SimpleNamespace(enabled=True, password='not-logged', url='https://rcon.invalid')
        with patch('services.rcon.config.server', return_value=server):
            first = asyncio.create_task(client.get('server1', '/v1/players'))
            await entered.wait()
            second = asyncio.create_task(client.get('server1', '/v1/players'))
            await asyncio.sleep(0)
            self.assertEqual(client.session.calls, 1)
            release.set()
            self.assertEqual(await asyncio.gather(first, second), [{'players': []}, {'players': []}])

    async def test_rcon_error_log_message_is_safe_and_specific(self):
        self.assertEqual(RconError(503).safe_message, 'RconError (HTTP 503)')
        self.assertEqual(RconError(reason='timeout').safe_message, 'RconError (timeout)')

    async def test_failed_ban_read_is_not_empty_list(self):
        client = RconClient()
        client.get = AsyncMock(side_effect=RconError(503))
        with self.assertRaises(RconError):
            await client.bans('server1')
        client.get = AsyncMock(return_value={})
        with self.assertRaises(RconError):
            await client.bans('server1')
        client.get = AsyncMock(return_value={'bans': []})
        self.assertEqual(await client.bans('server1'), set())

    async def test_config_conflict_refetches_revision_and_preserves_other_changes(self):
        client = RconClient()
        client.get = AsyncMock(side_effect=[
            dict(text='old',revision='1'),dict(text='old\nexternal',revision='2'),dict(text='new\nexternal',revision='3')])
        client.request = AsyncMock(side_effect=[RconError(412),Reply(200,{})])
        await client.edit_config('server1', lambda text: text.replace('old','new'))
        self.assertEqual(client.request.await_args_list[1].kwargs['revision'], '2')
        self.assertEqual(client.request.await_args_list[1].kwargs['text'], 'new\nexternal')

    async def test_ambiguous_config_write_is_verified_without_blind_repeat(self):
        client = RconClient()
        client.get = AsyncMock(side_effect=[dict(text='old',revision='1'),dict(text='new',revision='2')])
        client.request = AsyncMock(side_effect=RconError(uncertain=True))
        await client.edit_config('server1', lambda text: text.replace('old','new'))
        client.request.assert_awaited_once()


class DiscordTests(unittest.IsolatedAsyncioTestCase):
    def test_lookup_name_matches_are_deduplicated_and_limited(self):
        rows = [
            {'steam_id': '76561190000000001', 'name': 'Same', 'last_seen': datetime(2026, 1, 2)},
            {'steam_id': '76561190000000001', 'name': 'Older', 'last_seen': datetime(2026, 1, 1)},
            {'steam_id': '76561190000000002', 'name': 'Other', 'last_seen': datetime(2026, 1, 1)},
        ]
        matches = unique_matches(rows)
        self.assertEqual([row['steam_id'] for row in matches], ['76561190000000001', '76561190000000002'])
        self.assertEqual(matches[0]['name'], 'Same')

    def test_lookup_profile_pages_include_bans_and_jobs(self):
        cog = PlayerLookupCog.__new__(PlayerLookupCog)
        profile = dict(steam_id='76561190000000001', names=['Tester'], servers={}, target=None,
                       bans=[dict(issued_at=datetime(2026, 1, 1), status='active', admin_mention='Admin',
                                  duration_str='Permanent', expires_at=None, reason='Test')],
                       jobs=[dict(server_id='server1', action='ban', status='pending', attempts=2,
                                  next_attempt=datetime(2026, 1, 2), last_error='HTTP 503')])
        pages = PlayerLookupCog.pages(cog, profile)
        self.assertEqual(len(pages), 1)
        field_names = [field.name for field in pages[0].fields]
        self.assertIn('🛡️ Ban-Historie', field_names)
        self.assertIn('⚙️ Admin-Aufträge', field_names)

    def test_live_status_formats_team_scores(self):
        self.assertEqual(format_scores({'factionScores': [
            {'name': 'Valkyra', 'score': 87}, {'name': 'Chernaya', 'score': 81.5}]}),
            'Valkyra: 87 · Chernaya: 81.5')

    def test_daily_player_graph_is_png_with_berlin_day_bounds(self):
        now = datetime(2026, 9, 21, 0, 0, tzinfo=ZoneInfo('Europe/Berlin'))
        start_local, end_local = previous_day_window(now)
        start = start_local.astimezone(timezone.utc).replace(tzinfo=None)
        end = end_local.astimezone(timezone.utc).replace(tzinfo=None)
        image = player_graph([
            {'timestamp': start + timedelta(hours=1), 'player_count': 5},
            {'timestamp': start + timedelta(hours=2), 'player_count': 18}], start, end)
        self.assertTrue(image.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertEqual((start_local.hour, end_local.hour), (0, 0))

    def test_current_day_window_queries_until_now_and_graphs_full_day(self):
        now = datetime(2026, 9, 20, 14, 30, tzinfo=ZoneInfo('Europe/Berlin'))
        start, query_end, graph_end = current_day_window(now)
        self.assertEqual((start.hour, query_end.hour, graph_end.hour), (0, 14, 0))
        self.assertEqual((query_end - start).total_seconds(), 14.5 * 3600)
        self.assertEqual((graph_end - start).total_seconds(), 24 * 3600)

    async def test_fetched_component_ids_do_not_refresh_unchanged_voting_panel(self):
        embed = discord.Embed(title='Map Voting')
        planned = [{'type': 1, 'components': [
            {'type': 2, 'style': 1, 'label': 'Bakurani', 'custom_id': 'vote_Bakurani', 'disabled': True}]}]
        fetched = [{'type': 1, 'id': 0, 'components': [
            {'type': 2, 'id': 42, 'style': 1, 'label': 'Bakurani', 'custom_id': 'vote_Bakurani', 'disabled': True}]}]
        message = SimpleNamespace(embeds=[embed], components=[
            SimpleNamespace(to_dict=lambda payload=fetched[0]: payload)])
        view = SimpleNamespace(to_components=lambda: planned)
        self.assertFalse(_panel_changed(message, embed, view))

    async def test_leaderboard_defers_before_slow_query(self):
        response = SimpleNamespace(defer=AsyncMock())
        interaction = SimpleNamespace(response=response,followup=SimpleNamespace(send=AsyncMock()))
        async def slow_query(*args):
            response.defer.assert_awaited_once()
            return discord.Embed(title='Server 3')
        cog = SimpleNamespace(generate_embed=slow_query, bot=SimpleNamespace(health=Mock()))
        dropdown = PublicLeaderboardDropdown(cog, 'server3')
        dropdown._values = ['7d']
        await dropdown.callback(interaction)
        interaction.followup.send.assert_awaited_once()
        self.assertEqual(interaction.followup.send.await_args.kwargs['embed'].title, 'Server 3')

    async def test_admin_panel_transient_error_does_not_create_new_message(self):
        channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=RuntimeError('temporary error')), send=AsyncMock())
        bot = SimpleNamespace(get_channel=lambda _:channel, health=Mock())
        cog = AdminPanelCog.__new__(AdminPanelCog)
        cog.bot = bot
        with patch.object(config, 'ADMIN_PANEL_CHANNEL_ID', '123'), \
             patch('cogs.admin_panel.read_state', return_value={'message_id':456}):
            await AdminPanelCog.ensure_panel.coro(cog)
        channel.send.assert_not_awaited()

    async def test_unavailable_voting_status_preserves_persisted_votes(self):
        cog = MapVoteCog.__new__(MapVoteCog)
        from collections import defaultdict
        cog.locks = defaultdict(asyncio.Lock)
        cog.bot = SimpleNamespace(get_cog=lambda _:SimpleNamespace(current=lambda _:None))
        state = dict(enabled=True,locked=False,votes={'1':'Bakurani'},round_id='r1',msg_id=1)
        cog.load = AsyncMock(return_value=state)
        cog.panel = AsyncMock()
        await cog.tick_server(config.server('server2'))
        self.assertFalse(state['locked'])
        self.assertEqual(state['votes'], {'1':'Bakurani'})
        self.assertTrue(cog.panel.await_args.args[2]['locked'])

    async def test_all_cogs_load_and_close_without_network(self):
        bot = KartelBot()
        async with bot:
            for name in ('discord_logger','round_tracker','server_status','leaderboard','match_events',
                         'ban_tracker','map_vote','admin_panel','stats_tracker','server_recap','player_lookup',
                         'quest_tracker','low_population_guard','reward_shop','seed_tracker','region_guard'):
                await bot.load_extension('cogs.' + name)
            self.assertEqual(len(bot.cogs), 16)
            self.assertEqual({c.name for c in bot.tree.get_commands()},
                             {'voting','forcemap','adminpanel','ban_lookup','lookup','addvip','seed','regionguard'})
            self.assertEqual(len(bot.persistent_views), 7)
