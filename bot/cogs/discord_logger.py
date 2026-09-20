import logging
from io import BytesIO
import discord
from discord.ext import commands

from core import config

class DiscordLogger(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_bot_log(self, title: str, description: str, color: discord.Color = discord.Color.blue()):
        """
        Custom event listener. 
        Kann im gesamten Bot aufgerufen werden per:
        self.bot.dispatch("bot_log", "Titel", "Beschreibung", discord.Color.red())
        """
        if not config.DISCORD_LOG_CHANNEL_ID:
            return

        channel = self.bot.get_channel(int(config.DISCORD_LOG_CHANNEL_ID))
        if not channel:
            try:
                channel = await self.bot.fetch_channel(int(config.DISCORD_LOG_CHANNEL_ID))
            except Exception as e:
                logging.error(f"Konnte Log-Channel nicht finden: {e}")
                return

        embed = discord.Embed(
            title=title[:256],
            description=description[:4096],
            color=color
        )
        embed.set_footer(text="Event Timestamp")
        embed.timestamp = discord.utils.utcnow()

        try:
            await channel.send(embed=embed)
        except Exception as e:
            logging.error(f"Konnte Bot-Log nicht in Discord senden: {e}")

    @commands.Cog.listener()
    async def on_server_recap(self, embed: discord.Embed, image: bytes, filename: str):
        """Central delivery point for the public daily server recap."""
        if not config.SERVER_RECAP_CHANNEL_ID:
            return
        try:
            channel = self.bot.get_channel(int(config.SERVER_RECAP_CHANNEL_ID))
            if channel is None:
                channel = await self.bot.fetch_channel(int(config.SERVER_RECAP_CHANNEL_ID))
            await channel.send(embed=embed, file=discord.File(BytesIO(image), filename=filename))
        except Exception as exc:
            logging.error("Konnte Server-Rückblick nicht in Discord senden: %s", type(exc).__name__)


async def setup(bot):
    await bot.add_cog(DiscordLogger(bot))
