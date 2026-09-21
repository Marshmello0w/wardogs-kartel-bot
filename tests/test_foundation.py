import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
from core import config
from core.permissions import is_admin, require_admin, valid_steam_id
from core.runtime import Health
from infrastructure import database


class PermissionsTests(unittest.TestCase):
    def test_empty_roles_is_not_public_access(self):
        member = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False), roles=[])
        with patch.object(config, "ADMIN_ROLE_IDS", []):
            self.assertFalse(is_admin(member))
            member.guild_permissions.administrator = True
            self.assertTrue(is_admin(member))
        self.assertFalse(is_admin(object()))

    def test_steam_id(self):
        self.assertTrue(valid_steam_id("76561190000000000"))
        self.assertFalse(valid_steam_id("a" * 17))
        self.assertFalse(valid_steam_id("١" * 17))


class GuildScopeTests(unittest.IsolatedAsyncioTestCase):
    def _interaction(self, guild_id):
        response = SimpleNamespace(is_done=Mock(return_value=False), send_message=AsyncMock())
        return SimpleNamespace(
            guild_id=guild_id,
            guild=SimpleNamespace(id=guild_id),
            user=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True), roles=[]),
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_admin_is_limited_to_configured_guild(self):
        with patch.object(config, 'GUILD_ID', '100'):
            self.assertTrue(await require_admin(self._interaction(100)))
            denied = self._interaction(200)
            self.assertFalse(await require_admin(denied))
            denied.response.send_message.assert_awaited_once()


class RconConfigurationTests(unittest.TestCase):
    def test_rcon_accepts_game_host_http_or_https(self):
        self.assertTrue(config.Server('server1', 'Server 1', 'https://rcon.example:20001', 'secret').enabled)
        self.assertTrue(config.Server('server1', 'Server 1', 'http://rcon.example:20001', 'secret').enabled)
        self.assertTrue(config.Server('server1', 'Server 1', 'http://127.0.0.1:20001', 'secret').enabled)

    def test_missing_guild_id_fails_startup_validation(self):
        with patch.object(config, 'DISCORD_BOT_TOKEN', 'test-token'), patch.object(config, 'GUILD_ID', ''):
            self.assertFalse(config.validate())


class HealthTests(unittest.TestCase):
    def test_repeated_identical_error_is_logged_once_until_interval(self):
        bot = Mock()
        health = Health(bot)
        error = SimpleNamespace(safe_message='RconError (HTTP 404)')
        with patch('core.runtime.time.monotonic', side_effect=(10, 20, 310)), \
             patch('core.runtime.logging.error') as log_error:
            health.error('Admin-Aufträge Server 1', error)
            health.error('Admin-Aufträge Server 1', error)
            health.error('Admin-Aufträge Server 1', error)
        self.assertEqual(log_error.call_count, 2)
        self.assertEqual(bot.dispatch.call_count, 1)


class FakePool:
    def __init__(self, broken=False):
        self.closed = False
        self.broken = broken

    def acquire(self):
        if self.broken:
            raise RuntimeError("connection lost")
        return self

    def cursor(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, *args):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        database._global_pool = None
        database._pool_lock = asyncio.Lock()

    async def asyncTearDown(self):
        await database.close_pool()

    async def test_reconnect_discards_closed_pool(self):
        old, new = FakePool(broken=True), FakePool()
        database._global_pool = old
        with patch.object(config, "DB_CONNECTION_URL", "mysql://test:test@localhost/test"), \
             patch.object(database.aiomysql, "create_pool", AsyncMock(return_value=new)), \
             patch.object(database, "init_db", AsyncMock()) as init:
            self.assertIs(await database.check_and_reconnect(old), new)
            self.assertTrue(old.closed)
            await database.check_and_reconnect(new)
            init.assert_awaited_once()

    async def test_concurrent_initialization_creates_one_pool(self):
        new = FakePool()
        with patch.object(config, "DB_CONNECTION_URL", "mysql://test:test@localhost/test"), \
             patch.object(database.aiomysql, "create_pool", AsyncMock(return_value=new)) as create, \
             patch.object(database, "init_db", AsyncMock()):
            pools = await asyncio.gather(*(database.get_db_pool() for _ in range(5)))
            self.assertTrue(all(p is new for p in pools))
            create.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
