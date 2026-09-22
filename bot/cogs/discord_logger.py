import asyncio
import logging
from collections import defaultdict
from io import BytesIO
from time import monotonic
import discord
from discord.ext import commands

from core import config
from core.runtime import read_state, write_state

class DiscordLogger(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._recap_lock = asyncio.Lock()
        self._log_locks = defaultdict(asyncio.Lock)
        self._last_log_sent_at = {}

    @commands.Cog.listener()
    async def on_bot_log(self, title: str, description: str, color: discord.Color = discord.Color.blue(),
                         channel_id: str | None = None):
        """
        Custom event listener. 
        Kann im gesamten Bot aufgerufen werden per:
        self.bot.dispatch("bot_log", "Titel", "Beschreibung", discord.Color.red())
        """
        target_channel_id = channel_id or config.DISCORD_LOG_CHANNEL_ID
        if not target_channel_id:
            return

        embed = discord.Embed(
            title=title[:256],
            description=description[:4096],
            color=color
        )
        embed.set_footer(text="Event Timestamp")
        embed.timestamp = discord.utils.utcnow()

        try:
            # Serialise messages per channel and leave a safety margin below
            # Discord's channel rate limit. Important bursts are queued, never
            # dropped, and ordinary bot features keep their own channels.
            async with self._log_locks[str(target_channel_id)]:
                elapsed = monotonic() - self._last_log_sent_at.get(str(target_channel_id), 0)
                if elapsed < config.DISCORD_LOG_MIN_INTERVAL_SECONDS:
                    await asyncio.sleep(config.DISCORD_LOG_MIN_INTERVAL_SECONDS - elapsed)
                channel = self.bot.get_channel(int(target_channel_id))
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(int(target_channel_id))
                    except Exception as e:
                        logging.error(f"Konnte Log-Channel nicht finden: {e}")
                        return
                await channel.send(embed=embed)
                self._last_log_sent_at[str(target_channel_id)] = monotonic()
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

    @commands.Cog.listener()
    async def on_public_lookup(self, channel_id: int, embed: discord.Embed, view: discord.ui.View):
        """Central sender for admin-authorized public player lookup reports."""
        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(channel_id))
            await channel.send(embed=embed, view=view)
        except Exception as exc:
            logging.error("Konnte öffentlichen Spieler-Lookup nicht senden: %s", type(exc).__name__)

    async def _vip_channel(self):
        if not config.VIP_CHANNEL_ID:
            return None
        channel = self.bot.get_channel(int(config.VIP_CHANNEL_ID))
        return channel if channel is not None else await self.bot.fetch_channel(int(config.VIP_CHANNEL_ID))

    @commands.Cog.listener()
    async def on_vip_order(self, membership_id: int):
        """Central sender for new VIP administration posts."""
        try:
            shop = self.bot.get_cog('RewardShopCog')
            row = await shop.membership(membership_id) if shop else None
            if not row or row.get('discord_message_id'):
                return
            channel = await self._vip_channel()
            if channel is None:
                return
            view = None
            if row['status'] == 'pending_activation':
                from cogs.reward_shop import VipPendingView
                view = VipPendingView(shop)
            message = await channel.send(embed=shop.vip_embed(row), view=view)
            await shop.delivered(row['id'], message.id)
        except Exception as exc:
            logging.error('Konnte VIP-Auftrag nicht in Discord senden: %s', type(exc).__name__)

    @commands.Cog.listener()
    async def on_vip_update(self, membership_id: int):
        try:
            shop = self.bot.get_cog('RewardShopCog')
            row = await shop.membership(membership_id) if shop else None
            if not row or not row.get('discord_message_id'):
                return
            channel = await self._vip_channel()
            message = await channel.fetch_message(int(row['discord_message_id']))
            await message.edit(embed=shop.vip_embed(row), view=None)
        except Exception as exc:
            logging.error('Konnte VIP-Auftrag nicht aktualisieren: %s', type(exc).__name__)

    @commands.Cog.listener()
    async def on_vip_expired(self, membership_id: int):
        try:
            shop = self.bot.get_cog('RewardShopCog')
            row = await shop.membership(membership_id) if shop else None
            if not row or row.get('expiry_message_id'):
                return
            channel = await self._vip_channel()
            if channel is None:
                return
            from cogs.reward_shop import VipExpiryView
            embed = discord.Embed(title='⚠️ VIP abgelaufen – Entfernung erforderlich', color=discord.Color.orange(),
                                  description=f"**Spieler:** {row.get('player_name') or 'Unbekannt'}\n**Steam64-ID:** `{row['steam_id']}`\n**Server:** {config.server(row['server_id']).title}\nVIP muss jetzt extern entfernt werden.")
            embed.set_footer(text=f'VIP-Auftrag {row["id"]}')
            message = await channel.send(embed=embed, view=VipExpiryView(shop))
            await shop.delivered(row['id'], message.id, expiry=True)
        except Exception as exc:
            logging.error('Konnte VIP-Ablauf nicht in Discord senden: %s', type(exc).__name__)

    @commands.Cog.listener()
    async def on_vip_expiry_update(self, membership_id: int):
        try:
            shop = self.bot.get_cog('RewardShopCog')
            row = await shop.membership(membership_id) if shop else None
            if not row or not row.get('expiry_message_id'):
                return
            channel = await self._vip_channel()
            message = await channel.fetch_message(int(row['expiry_message_id']))
            embed = discord.Embed(title='✅ VIP extern entfernt', color=discord.Color.green(),
                                  description=f"**Spieler:** {row.get('player_name') or 'Unbekannt'}\n**Steam64-ID:** `{row['steam_id']}`\n**Server:** {config.server(row['server_id']).title}\nDer VIP-Slot ist wieder frei.")
            await message.edit(embed=embed, view=None)
        except Exception as exc:
            logging.error('Konnte VIP-Entfernung nicht aktualisieren: %s', type(exc).__name__)


async def setup(bot):
    await bot.add_cog(DiscordLogger(bot))
