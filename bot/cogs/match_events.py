from datetime import datetime
import asyncio
import random
import discord
from discord.ext import commands, tasks
from core import config
from infrastructure import database, storage
from services.rcon import RconError


class MatchEvents(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.followup_tasks = set()
        self.check_match_events.start()

    def cog_unload(self):
        self.check_match_events.cancel()
        for task in self.followup_tasks:
            task.cancel()

    def schedule_english_followup(self, server_id):
        task = asyncio.create_task(self.send_english_followup(server_id))
        self.followup_tasks.add(task)
        task.add_done_callback(self.followup_tasks.discard)

    async def send_english_followup(self, server_id):
        """Send one translated round-end notice after the prior broadcast has faded."""
        try:
            await asyncio.sleep(config.BROADCAST_FOLLOWUP_DELAY_SECONDS)
            message = random.choice(config.ENGLISH_BROADCAST_MESSAGES)
            reply = await self.bot.rcon.request(server_id, 'POST', '/v1/broadcast', payload={'message': message})
            if reply.status == 202:
                raise RconError(202, uncertain=True)
            self.bot.dispatch('bot_log', '🌐 Englischer Runden-Broadcast',
                              f"{config.server(server_id).title}: {message}", discord.Color.blue())
            self.bot.health.ok(f'Englischer Runden-Broadcast {server_id}')
        except asyncio.CancelledError:
            raise
        except RconError as exc:
            # A delayed POST can be uncertain; never repeat it blindly.
            self.bot.health.error(f'Englischer Runden-Broadcast {server_id}', exc)
        except Exception as exc:
            self.bot.health.error(f'Englischer Runden-Broadcast {server_id}', exc)

    @tasks.loop(seconds=5)
    async def check_match_events(self):
        try:
            events = await storage.pending_events('broadcast', ('round_ended',))
            for event in events:
                data = event['data']
                age = (storage.utcnow() - datetime.fromisoformat(data['ended_at'])).total_seconds()
                if not data.get('winner') or age > 90:
                    await storage.acknowledge(event['id'], 'broadcast', 'skipped')
                    continue
                # Reserve before POST: a crash/timeout must not cause a blind repeat.
                await storage.acknowledge(event['id'], 'broadcast', 'uncertain')
                message = random.choice(config.BROADCAST_MESSAGES)
                try:
                    reply = await self.bot.rcon.request(event['server_id'], 'POST', '/v1/broadcast', payload={'message': message})
                    if reply.status == 202:
                        raise RconError(202, uncertain=True)
                except RconError as exc:
                    if not exc.uncertain:
                        async with database.transaction() as cur:
                            await cur.execute("DELETE FROM event_deliveries WHERE event_id=%s AND consumer='broadcast'", (event['id'],))
                    self.bot.health.error(f"Runden-Broadcast {event['server_id']}", exc)
                    continue
                await storage.acknowledge(event['id'], 'broadcast')
                self.bot.dispatch('bot_log', '🏁 Runden-Ende Broadcast',
                                  f"{config.server(event['server_id']).title}: {message}", discord.Color.blue())
                self.schedule_english_followup(event['server_id'])
                self.bot.health.ok(f"Runden-Broadcast {event['server_id']}")
        except Exception as exc:
            self.bot.health.error('Runden-Broadcast-Verarbeitung', exc)

    @check_match_events.before_loop
    async def ready(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(MatchEvents(bot))
