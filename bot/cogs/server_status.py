import time
import discord
from discord.ext import commands, tasks
from core import config
from core.runtime import read_state, write_state
from infrastructure import database


def format_scores(data):
    scores = data.get('factionScores', []) if isinstance(data, dict) else []
    values = []
    for item in scores:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
            continue
        score = item.get('score')
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            values.append(f"{item['name']}: {int(score) if float(score).is_integer() else score}")
    return ' · '.join(values)


class ServerStatus(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.regions = {}
        self.region_at = {}
        self.update_status_embed.start()

    def cog_unload(self):
        self.update_status_embed.cancel()

    async def region(self, srv):
        session = self.bot.rcon.session
        if srv.uuid and session and time.monotonic() - self.region_at.get(srv.id, 0) > 3600:
            try:
                async with session.get('https://wardogserverlist.com/api/server',
                                       params={'key': 'id|' + srv.uuid}) as response:
                    if response.status == 200:
                        data = await response.json()
                        self.regions[srv.id] = str(data.get('server', {}).get('region', 'Unknown')).upper()
                        self.region_at[srv.id] = time.monotonic()
            except Exception:
                pass
        return self.regions.get(srv.id, 'Unknown')

    @tasks.loop(seconds=60)
    async def update_status_embed(self):
        if not config.SERVER_STATUS_CHANNEL_ID:
            return
        try:
            tracker = self.bot.get_cog('RoundTracker')
            if not tracker:
                return
            embed = discord.Embed(title='Das Kartell Server Status', color=discord.Color.green())
            for srv in config.servers():
                if not srv.enabled:
                    continue
                state = tracker.current(srv.id)
                last = tracker.states.get(srv.id)
                data = state['snapshot'] if state else (last or {}).get('snapshot')
                uptime = 'N/A'
                try:
                    pool = await database.check_and_reconnect(None)
                    if pool:
                        await database.log_uptime(pool, srv.uuid or srv.id, state is not None)
                        values = await database.get_uptime_stats(pool, srv.uuid or srv.id)
                        uptime = ' | '.join(f'{key}: {value}' for key,value in values.items())
                except Exception as exc:
                    self.bot.health.error('Status-Datenbank', exc)
                text = '**Status:** 🟢 Online' if state else '**Status:** 🟠 Status veraltet / nicht erreichbar'
                if data:
                    counts = data.get('players', {})
                    modes = ', '.join(data['experiences'])
                    text += (f"\n**Spieler:** {counts.get('current','?')}/{counts.get('max','?')}"
                             f"\n**Karte:** {data['map']}\n**Modus:** {modes[:150]}")
                    scores = format_scores(data)
                    if scores:
                        text += f"\n**Teamstände:** {scores[:300]}"
                region = await self.region(srv)
                text += f"\n**Region:** {region}\n**Server-Verfügbarkeit:** {uptime}"
                embed.add_field(name=str((data or {}).get('serverName', srv.title))[:256], value=text[:1024], inline=False)
            embed.set_footer(text='Server-Verfügbarkeit wird über die RCON-Erreichbarkeit gemessen.')
            embed.timestamp = discord.utils.utcnow()
            channel = self.bot.get_channel(int(config.SERVER_STATUS_CHANNEL_ID)) or await self.bot.fetch_channel(int(config.SERVER_STATUS_CHANNEL_ID))
            saved = read_state(config.MESSAGE_ID_FILE, {})
            message = None
            if saved.get('message_id'):
                try:
                    message = await channel.fetch_message(saved['message_id'])
                except discord.NotFound:
                    pass
            if message is None:
                message = await channel.send(embed=embed)
                write_state(config.MESSAGE_ID_FILE, {'message_id': message.id})
            else:
                await message.edit(embed=embed)
            self.bot.health.ok('Serverstatus-Panel')
        except Exception as exc:
            self.bot.health.error('Serverstatus-Panel', exc)

    @update_status_embed.before_loop
    async def ready(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ServerStatus(bot))
