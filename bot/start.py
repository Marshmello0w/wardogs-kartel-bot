import asyncio
import logging
import discord
from discord.ext import commands
from core import config
from core.runtime import Health
from infrastructure import database
from services.rcon import RconClient

logging.basicConfig(level=logging.INFO)


class KartelBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions.none())
        self.rcon = RconClient()
        self.health = Health(self)

    async def setup_hook(self):
        for name in ('discord_logger', 'round_tracker', 'server_status', 'leaderboard',
                     'match_events', 'ban_tracker', 'map_vote', 'admin_panel', 'stats_tracker',
                     'server_recap', 'quest_tracker', 'challenge_quests', 'player_lookup', 'low_population_guard', 'reward_shop',
                     'seed_tracker', 'region_guard', 'combat_feed', 'kill_feed',
                     'ingame_notifications'):
            await self.load_extension(f'cogs.{name}')
        try:
            # ``config.validate`` requires this identifier before login. Never
            # fall back to global command registration for privileged controls.
            guild = discord.Object(id=int(config.GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        except (discord.HTTPException, ValueError) as exc:
            logging.error('Command sync failed: %s', type(exc).__name__)

    async def on_ready(self):
        logging.info('Logged in as %s', self.user)

    async def close(self):
        for name in list(self.extensions):
            await self.unload_extension(name)
        await super().close()
        await self.rcon.close()
        await database.close_pool()


async def main():
    if not config.validate():
        logging.error('DISCORD_BOT_TOKEN or GUILD_ID is missing or invalid')
        return
    async with KartelBot() as bot:
        await bot.start(config.DISCORD_BOT_TOKEN)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # AMP stops the process with SIGINT. asyncio turns that into a cancelled
        # WebSocket receive followed by KeyboardInterrupt; neither is a bot error.
        logging.info('Bot wurde beendet.')
