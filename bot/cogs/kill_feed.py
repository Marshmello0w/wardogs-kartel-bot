"""Best-effort public kill feed, batched per server for Discord rate limits."""
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from core import config

MAX_BUFFER = 600
BATCH_SIZE = 20
MAX_MESSAGE_CHARS = 3800
MAX_AGE = timedelta(minutes=2)


def recent_batch(received_at, now=None):
    """Do not flood Discord with batches accumulated while the bot was offline."""
    now = now or datetime.now(timezone.utc)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    age = now - received_at
    return timedelta(0) <= age <= MAX_AGE


def clean(value, fallback='Unbekannt', limit=64):
    value = ' '.join(str(value or '').split())[:limit] or fallback
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))


def kill_line(event):
    """Names come from the game server and must not control Discord markup."""
    killer = clean(event.get('killer_name'), event.get('killer') or 'Unbekannt')
    victim = clean(event.get('victim_name'), event.get('victim') or 'Unbekannt')
    details = []
    if event.get('cause'):
        details.append(clean(event['cause'], limit=80))
    if event.get('distance_m') is not None:
        details.append(f"{event['distance_m']:.0f} m")
    if event.get('headshot'):
        details.append('Headshot')
    return f'**{killer}** → **{victim}**' + (f" · {' · '.join(details)}" if details else '')


class KillFeed(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pending = defaultdict(deque)
        self.dropped = defaultdict(int)
        if config.COMBAT_FEED_ENABLED:
            self.flush_loop.start()

    def cog_unload(self):
        if self.flush_loop.is_running():
            self.flush_loop.cancel()

    @commands.Cog.listener()
    async def on_combat_kill_batch(self, server_id, events, received_at):
        if server_id not in config.KILL_FEED_CHANNEL_IDS or not recent_batch(received_at):
            return
        queue = self.pending[server_id]
        for event in events:
            if not event.get('killer') or not event.get('victim') or event.get('suicide'):
                continue
            if len(queue) >= MAX_BUFFER:
                self.dropped[server_id] += 1
                continue
            queue.append(kill_line(event))

    async def flush(self):
        for server_id, channel_id in config.KILL_FEED_CHANNEL_IDS.items():
            queue = self.pending[server_id]
            if (not queue and not self.dropped[server_id]) or not channel_id:
                continue
            lines = []
            length = 0
            while queue and len(lines) < BATCH_SIZE:
                line = queue[0]
                if lines and length + len(line) + 1 > MAX_MESSAGE_CHARS:
                    break
                queue.popleft()
                lines.append(line)
                length += len(line) + 1
            dropped = self.dropped.pop(server_id, 0)
            if dropped:
                note = f'{dropped} weitere Ereignisse wegen hoher Last ausgelassen.'
                if length + len(note) + 1 <= MAX_MESSAGE_CHARS:
                    lines.append(note)
                else:
                    self.dropped[server_id] = dropped
            self.bot.dispatch('bot_log', f'Kill-Feed · {config.server(server_id).title}',
                              '\n'.join(lines), discord.Color.orange(), channel_id)

    @tasks.loop(seconds=10)
    async def flush_loop(self):
        await self.flush()

    @flush_loop.before_loop
    async def before_flush(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(KillFeed(bot))
