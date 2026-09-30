import asyncio
import copy
from contextlib import asynccontextmanager
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
from cogs.leaderboard import Leaderboard, PublicLeaderboardDropdown, following_sort
from cogs.map_vote import MapVoteCog, _panel_changed
from cogs.round_tracker import RoundTracker
from cogs.player_lookup import PlayerLookupCog, headshot_summary, unique_matches
from cogs.server_recap import current_day_window, player_graph, previous_day_window
from cogs.server_status import ServerStatus, format_scores
from cogs.stats_tracker import daily_playtime_slices
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
    async def test_status_join_ids_match_servers_including_offline_and_missing_id(self):
        servers = [SimpleNamespace(id=f'server{i}', title=f'Server {i}', enabled=True,
                                   uuid=f'join-code-{i}' if i < 3 else '') for i in (1, 2, 3)]
        snapshots = {srv.id: dict(serverName=srv.title, map='Bakurani', experiences=['KOTH'],
                                 serverId=f'changing-instance-{srv.id}',
                                 players={'current': 10, 'max': 100}, factionScores=[]) for srv in servers}
        tracker = SimpleNamespace(
            current=lambda sid: {'snapshot': snapshots[sid]} if sid == 'server1' else None,
            states={sid: {'snapshot': data} for sid, data in snapshots.items()})
        message = SimpleNamespace(edit=AsyncMock())
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message), send=AsyncMock())
        cog = ServerStatus.__new__(ServerStatus)
        cog.bot = SimpleNamespace(get_cog=lambda _: tracker, get_channel=lambda _: channel, health=Mock())
        cog.region = AsyncMock(return_value='EU-CENTRAL')
        loop_globals = ServerStatus.update_status_embed.coro.__globals__
        with (patch.object(config, 'SERVER_STATUS_CHANNEL_ID', '123'),
              patch('cogs.server_status.config.servers', return_value=servers),
              patch('cogs.server_status.database.check_and_reconnect', new=AsyncMock(return_value=None)),
              patch.dict(loop_globals, read_state=lambda path, default: {'message_id': 12}, write_state=Mock())):
            await ServerStatus.update_status_embed.coro(cog)
        cog.bot.health.error.assert_not_called()
        channel.send.assert_not_awaited()
        fields = message.edit.await_args.kwargs['embed'].fields
        self.assertIn('**Join-ID:** `join-code-1`', fields[0].value)
        self.assertIn('**Join-ID:** `join-code-2`', fields[1].value)
        self.assertIn('Status veraltet', fields[1].value)
        self.assertIn('**Join-ID:** nicht konfiguriert', fields[2].value)
        self.assertNotIn('changing-instance', '\n'.join(field.value for field in fields))
        self.assertTrue(all(len(field.value) <= 1024 for field in fields))

    async def test_leaderboard_embed_has_no_intro_notes(self):
        row = {'player_rank': 1, 'name': 'Spieler', 'kills': 10, 'deaths': 5,
               'kd': 2.0, 'cash': 1000, 'playtime_seconds': 3600,
               'legacy_seconds': 3600}
        cog = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: None))
        with patch('cogs.leaderboard.stats.ranking', new=AsyncMock(return_value=[row])), \
             patch('cogs.leaderboard.config.server', return_value=SimpleNamespace(title='Server 1')):
            embed = await Leaderboard.generate_embed(cog, 'server1')
        self.assertIsNone(embed.description)
        self.assertIn('Spielzeit: 1 Std.', embed.fields[0].value)

    def test_daily_playtime_splits_at_utc_midnight(self):
        end = datetime(2026, 9, 23, 0, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(list(daily_playtime_slices(end, 60)),
                         [(datetime(2026, 9, 22).date(), 30),
                          (datetime(2026, 9, 23).date(), 30)])

    async def test_public_leaderboards_rotate_independently_and_retry_failures(self):
        self.assertEqual([following_sort(x) for x in ('kd', 'cash', 'playtime')],
                         ['cash', 'playtime', 'kd'])
        servers = [SimpleNamespace(id=f'server{i}', title=f'Server {i}', enabled=True) for i in (1, 2)]
        state = {'server1': {'msg_id': 101}}
        messages = {101: SimpleNamespace(id=101, embeds=[], edit=AsyncMock())}
        next_id = 102

        async def send(*, embed, view):
            nonlocal next_id
            message = SimpleNamespace(id=next_id, embeds=[embed], edit=AsyncMock())
            async def edit_sent(**kwargs):
                await edit(message, **kwargs)
            message.edit.side_effect = edit_sent
            messages[next_id] = message
            next_id += 1
            return message

        async def edit(message, *, embed, view):
            message.embeds = [embed]

        async def edit_first(**kwargs):
            await edit(messages[101], **kwargs)

        messages[101].edit.side_effect = edit_first
        channel = SimpleNamespace(send=AsyncMock(side_effect=send),
                                  fetch_message=AsyncMock(side_effect=lambda mid: messages[mid]))
        bot = SimpleNamespace(get_channel=lambda _: channel, health=Mock())
        cog = Leaderboard.__new__(Leaderboard)
        cog.bot, cog.state_file = bot, 'test-state.json'
        calls = []
        fail_server1 = False

        async def generate(server_id, timeframe='7d', sort_by='kd'):
            calls.append((server_id, sort_by))
            if fail_server1 and server_id == 'server1':
                raise RuntimeError('temporary database error')
            return discord.Embed(title=f'{server_id}-{sort_by}-{len(calls)}')

        cog.generate_embed = generate

        def save(path, value):
            state.clear()
            state.update(copy.deepcopy(value))

        loop_globals = Leaderboard.update_leaderboard_ui.coro.__globals__
        with patch.object(config, 'LEADERBOARD_CHANNEL_ID', '123'), \
             patch('cogs.leaderboard.config.servers', return_value=servers), \
             patch.dict(loop_globals, read_state=lambda path, default: copy.deepcopy(state), write_state=save):
            for _ in range(3):
                await Leaderboard.update_leaderboard_ui.coro(cog)
            self.assertEqual(calls, [(server, sort) for sort in ('kd', 'cash', 'playtime')
                                     for server in ('server1', 'server2')], (state, bot.health.error.call_args_list))
            self.assertEqual((state['server1']['next_sort'], state['server2']['next_sort']), ('kd', 'kd'),
                             bot.health.error.call_args_list)
            fail_server1 = True
            await Leaderboard.update_leaderboard_ui.coro(cog)
            self.assertEqual((state['server1']['next_sort'], state['server2']['next_sort']), ('kd', 'cash'))

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
                                  next_attempt=datetime(2026, 1, 2), last_error='HTTP 503')],
                       points=42, vip_memberships=[dict(server_id='server2', status='active',
                                                         expires_at=datetime(2026, 1, 10), duration_kind='week', source='shop')])
        pages = PlayerLookupCog.pages(cog, profile)
        self.assertEqual(len(pages), 1)
        field_names = [field.name for field in pages[0].fields]
        self.assertIn('🛡️ Ban-Historie', field_names)
        self.assertIn('⚙️ Admin-Aufträge', field_names)
        self.assertIn('⭐ VIP', field_names)

        ledger_pages = PlayerLookupCog.point_pages(cog, dict(profile, point_ledger=[
            dict(amount=5, kind='seed_join', reason='Server 1', admin_mention=None, created_at=datetime(2026, 1, 2)),
            dict(amount=-150, kind='vip_purchase', reason='VIP week', admin_mention=None, created_at=datetime(2026, 1, 3)),
        ]))
        self.assertIn('+5 Punkte · Seed-Start', ledger_pages[0].fields[0].value)
        self.assertIn('-150 Punkte · VIP-Einlösung', ledger_pages[0].fields[0].value)

    def test_lookup_headshot_rate_uses_feed_kills_and_handles_no_kills(self):
        cog = PlayerLookupCog.__new__(PlayerLookupCog)
        profile = dict(steam_id='76561190000000001', names=['Tester'], target=None,
                       bans=[], jobs=[], points=0, vip_memberships=[], feed_kills=30,
                       feed_headshots=8, servers={
                           'server1': {'leaderboard': {'lifetime_kills': 100},
                                       'combat': {'kills': 20, 'headshots': 5}},
                           'server2': {'combat': {'kills': 10, 'headshots': 3}},
                       })
        page = PlayerLookupCog.pages(cog, profile)[0]
        self.assertIn('8/30 Feed-Kills · 26.7 %', page.description)
        server_fields = [field.value for field in page.fields if field.name.startswith('📊')]
        self.assertIn('5/20 Feed-Kills · 25.0 %', server_fields[0])
        self.assertIn('3/10 Feed-Kills · 30.0 %', server_fields[1])
        self.assertEqual(headshot_summary(0, 0), '— (keine Feed-Kills)')

    async def test_lookup_profile_reads_headshots_for_all_servers(self):
        class Cursor:
            sql = ''

            async def execute(self, sql, args=()):
                self.sql = sql

            async def fetchall(self):
                if 'FROM combat_player_stats' in self.sql:
                    return [{'server_id': 'server1', 'kills': 20, 'headshots': 5},
                            {'server_id': 'server2', 'kills': 10, 'headshots': 3}]
                return []

            async def fetchone(self):
                return None

        @asynccontextmanager
        async def transaction():
            yield Cursor()

        with patch('cogs.player_lookup.database.transaction', transaction):
            profile = await PlayerLookupCog.profile(PlayerLookupCog.__new__(PlayerLookupCog),
                                                    '76561190000000001')
        self.assertEqual((profile['feed_kills'], profile['feed_headshots']), (30, 8))
        self.assertEqual(profile['servers']['server1']['combat']['headshots'], 5)
        self.assertTrue(profile['exists'])

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
                         'quest_tracker','challenge_quests','low_population_guard','reward_shop','seed_tracker','region_guard'):
                await bot.load_extension('cogs.' + name)
            self.assertEqual(len(bot.cogs), 17)
            self.assertEqual({c.name for c in bot.tree.get_commands()},
                             {'voting','forcemap','adminpanel','ban_lookup','lookup','addvip','seed','regionguard','regionguardstart'})
            self.assertEqual(len(bot.persistent_views), 7)
