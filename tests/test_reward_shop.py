import os
from datetime import datetime
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from cogs.reward_shop import RewardShopCog, add_month, valid_faction
from services.rcon import RconError


class RewardShopRulesTests(unittest.TestCase):
    def test_only_real_factions_are_accepted(self):
        self.assertEqual(valid_faction('Valkyra'), 'Valkyra')
        self.assertIsNone(valid_faction('White'))
        self.assertIsNone(valid_faction('Unknown'))
        self.assertIsNone(valid_faction('valkyra'))

    def test_calendar_month_preserves_date_or_clamps_at_month_end(self):
        self.assertEqual(add_month(datetime(2026, 1, 31, 12, 30)), datetime(2026, 2, 28, 12, 30))
        self.assertEqual(add_month(datetime(2024, 1, 31, 12, 30)), datetime(2024, 2, 29, 12, 30))
        self.assertEqual(add_month(datetime(2026, 12, 15, 12, 30)), datetime(2027, 1, 15, 12, 30))


class FactionChargeOrderTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, players):
        cog = RewardShopCog.__new__(RewardShopCog)
        self.events = []

        async def reserve(*_args):
            self.events.append('reserve')
            return True

        async def mark(*_args):
            self.events.append('executing')
            return True

        async def request(*_args, **_kwargs):
            self.events.append('rcon-write')

        cog.bot = SimpleNamespace(
            rcon=SimpleNamespace(players=AsyncMock(side_effect=[players, [dict(steamId='76561190000000001', faction='Valkyra')]]),
                                 request=AsyncMock(side_effect=request)),
            dispatch=Mock(),
        )
        cog._reserve_faction_points = AsyncMock(side_effect=reserve)
        cog._mark_faction_executing = AsyncMock(side_effect=mark)
        cog._finish_request = AsyncMock()
        cog._refund_reserved_faction = AsyncMock()
        return cog

    async def test_points_are_reserved_before_any_faction_rcon_write(self):
        players = [
            dict(steamId='76561190000000001', faction='Lonestar'),
            dict(steamId='2', faction='Lonestar'), dict(steamId='3', faction='Valkyra'),
            dict(steamId='4', faction='Valkyra'), dict(steamId='5', faction='Manticore'),
            dict(steamId='6', faction='Manticore'),
        ]
        cog = self._cog(players)
        row = dict(id='request-1', steam_id='76561190000000001', server_id='server1', faction='Valkyra')
        await RewardShopCog._faction_request(cog, row)
        self.assertLess(self.events.index('reserve'), self.events.index('rcon-write'))
        self.assertEqual(cog.bot.rcon.request.await_count, 2)
        cog._finish_request.assert_awaited_once_with('request-1', 'success')

    async def test_failed_preflight_never_reserves_or_changes_faction(self):
        cog = self._cog([])
        cog.bot.rcon.players = AsyncMock(side_effect=RconError(503))
        row = dict(id='request-2', steam_id='76561190000000001', server_id='server1', faction='Valkyra')
        await RewardShopCog._faction_request(cog, row)
        cog._reserve_faction_points.assert_not_awaited()
        cog.bot.rcon.request.assert_not_awaited()
        cog._finish_request.assert_awaited_once_with('request-2', 'failed', 'RconError (HTTP 503)')

    async def test_ambiguous_write_is_held_for_reconciliation_not_refunded(self):
        players = [dict(steamId='76561190000000001', faction='Lonestar')]
        cog = self._cog(players)
        cog.bot.rcon.request = AsyncMock(side_effect=RconError(503, uncertain=True))
        row = dict(id='request-3', steam_id='76561190000000001', server_id='server1', faction='Valkyra')
        await RewardShopCog._faction_request(cog, row)
        cog._refund_reserved_faction.assert_not_awaited()
        cog._finish_request.assert_awaited_once_with('request-3', 'reconciliation_required', 'RconError (HTTP 503)')


if __name__ == '__main__':
    unittest.main()
