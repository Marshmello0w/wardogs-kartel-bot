import asyncio
import logging
from io import BytesIO
import discord
from discord.ext import commands

from core import config
from core.runtime import read_state, write_state

class DiscordLogger(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._recap_lock = asyncio.Lock()

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
    async def on_server_recap(self, server_id: str, embed: discord.Embed, image: bytes, filename: str):
        """Create each public recap once, then edit that same Discord message."""
        if not config.SERVER_RECAP_CHANNEL_ID:
            return
        try:
            async with self._recap_lock:
                channel = self.bot.get_channel(int(config.SERVER_RECAP_CHANNEL_ID))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(config.SERVER_RECAP_CHANNEL_ID))
                message_ids = read_state(config.SERVER_RECAP_MESSAGE_IDS_FILE, {})
                message_id = message_ids.get(server_id)
                if message_id:
                    try:
                        message = await channel.fetch_message(int(message_id))
                        await message.edit(embed=embed, attachments=[discord.File(BytesIO(image), filename=filename)])
                        return
                    except discord.NotFound:
                        message_ids.pop(server_id, None)
                message = await channel.send(embed=embed, file=discord.File(BytesIO(image), filename=filename))
                message_ids[server_id] = message.id
                write_state(config.SERVER_RECAP_MESSAGE_IDS_FILE, message_ids)
        except Exception as exc:
            logging.error("Konnte Server-Rückblick nicht in Discord senden: %s", type(exc).__name__)


async def setup(bot):
    await bot.add_cog(DiscordLogger(bot))
