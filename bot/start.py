import logging
import asyncio
import discord
from discord.ext import commands

import config

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

async def load_cogs():
    await bot.load_extension("cogs.server_status")
    await bot.load_extension("cogs.leaderboard")
    await bot.load_extension("cogs.match_events")
    await bot.load_extension("cogs.ban_tracker")
    await bot.load_extension("cogs.discord_logger")
    await bot.load_extension("cogs.map_vote")
    await bot.load_extension("cogs.admin_panel")
    await bot.load_extension("cogs.playtime_tracker")

bot.setup_hook = load_cogs

# Füge den View zur setup_hook hinzu, damit die Buttons nach Neustart funktionieren
async def setup_persistent_views():
    from cogs.admin_panel import AdminPanelView
    bot.add_view(AdminPanelView(bot))

original_load_cogs = bot.setup_hook
async def new_setup_hook():
    await original_load_cogs()
    await setup_persistent_views()
bot.setup_hook = new_setup_hook

@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user.name} ({bot.user.id})")
    try:
        if config.GUILD_ID:
            guild = discord.Object(id=int(config.GUILD_ID))
            
            # 1. Copy global commands (from our Cogs) to the specific guild
            bot.tree.copy_global_to(guild=guild)
            
            # 2. Sync the guild commands to Discord API
            synced = await bot.tree.sync(guild=guild)
            logging.info(f"Synced {len(synced)} command(s) to guild {config.GUILD_ID}")
            
            # 3. Wipe the global commands from the bot's memory and sync to delete them globally from Discord
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync(guild=None)
        else:
            synced = await bot.tree.sync()
            logging.info(f"Synced {len(synced)} command(s) globally")
    except Exception as e:
        logging.error(f"Failed to sync commands: {e}")

async def main():
    if not config.DISCORD_BOT_TOKEN:
        logging.error("DISCORD_BOT_TOKEN is missing in .env")
        return
        
    async with bot:
        await bot.start(config.DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
