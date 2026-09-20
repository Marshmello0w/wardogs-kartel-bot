import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
import discord
import config
from cogs.admin_panel import AdminPanelCog
from cogs.leaderboard import PublicLeaderboardDropdown
from cogs.map_vote import MapVoteCog
from cogs.round_tracker import RoundTracker
from rcon import RconClient, RconError, Reply
from runtime import Health
from start import KartelBot


class RconTests(unittest.IsolatedAsyncioTestCase):
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
                         'ban_tracker','map_vote','admin_panel','stats_tracker'):
                await bot.load_extension('cogs.' + name)
            self.assertEqual(len(bot.cogs), 9)
            self.assertEqual({c.name for c in bot.tree.get_commands()}, {'voting','forcemap','adminpanel','ban_lookup'})
            self.assertEqual(len(bot.persistent_views), 4)
