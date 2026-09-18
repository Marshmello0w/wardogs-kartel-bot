import logging
import asyncio
import discord
from discord.ext import commands

import config

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user.name} ({bot.user.id})")

async def main():
    # Lade alle Erweiterungen (Cogs)
    await bot.load_extension("cogs.server_status")
    await bot.load_extension("cogs.leaderboard")
    await bot.load_extension("cogs.match_events")
    await bot.load_extension("cogs.ban_tracker")
    await bot.load_extension("cogs.discord_logger")
    
    # Starte den Bot
    if not config.DISCORD_BOT_TOKEN:
        logging.error("DISCORD_BOT_TOKEN is missing in .env")
        return
        
    await bot.start(config.DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
