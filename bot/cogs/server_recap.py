"""Daily public server recap with a locally rendered player-count graph."""
from datetime import datetime, time as clock_time, timedelta, timezone
import math
import struct
from zoneinfo import ZoneInfo
import zlib

import discord
from discord.ext import commands, tasks

from core import config
from infrastructure import database, storage


BERLIN = ZoneInfo('Europe/Berlin')
GRAPH_WIDTH, GRAPH_HEIGHT = 1000, 420

# A deliberately small bitmap font keeps graph rendering dependency-free on AMP.
FONT = {
    '0': ('111', '101', '101', '101', '111'),
    '1': ('010', '110', '010', '010', '111'),
    '2': ('111', '001', '111', '100', '111'),
    '3': ('111', '001', '111', '001', '111'),
    '4': ('101', '101', '111', '001', '001'),
    '5': ('111', '100', '111', '001', '111'),
    '6': ('111', '100', '111', '101', '111'),
    '7': ('111', '001', '010', '010', '010'),
    '8': ('111', '101', '111', '101', '111'),
    '9': ('111', '101', '111', '001', '111'),
    ':': ('000', '010', '000', '010', '000'),
    '-': ('000', '000', '111', '000', '000'),
}


class Canvas:
    def __init__(self, width, height, background=(30, 31, 34)):
        self.width, self.height = width, height
        self.pixels = bytearray(background) * (width * height)

    def pixel(self, x, y, color):
        if 0 <= x < self.width and 0 <= y < self.height:
            index = (y * self.width + x) * 3
            self.pixels[index:index + 3] = bytes(color)

    def line(self, x1, y1, x2, y2, color):
        dx, dy = abs(x2 - x1), -abs(y2 - y1)
        sx, sy = (1 if x1 < x2 else -1), (1 if y1 < y2 else -1)
        error = dx + dy
        while True:
            self.pixel(x1, y1, color)
            if x1 == x2 and y1 == y2:
                return
            twice = 2 * error
            if twice >= dy:
                error += dy
                x1 += sx
            if twice <= dx:
                error += dx
                y1 += sy

    def text(self, x, y, value, color=(205, 208, 213), scale=2):
        for char in str(value):
            pattern = FONT.get(char)
            if pattern:
                for row, bits in enumerate(pattern):
                    for column, bit in enumerate(bits):
                        if bit == '1':
                            for offset_y in range(scale):
                                for offset_x in range(scale):
                                    self.pixel(x + column * scale + offset_x, y + row * scale + offset_y, color)
            x += 4 * scale

    def png(self):
        rows = b''.join(b'\x00' + self.pixels[row * self.width * 3:(row + 1) * self.width * 3]
                        for row in range(self.height))

        def chunk(kind, value):
            return (struct.pack('>I', len(value)) + kind + value +
                    struct.pack('>I', zlib.crc32(kind + value) & 0xffffffff))

        return (b'\x89PNG\r\n\x1a\n' +
                chunk(b'IHDR', struct.pack('>IIBBBBB', self.width, self.height, 8, 2, 0, 0, 0)) +
                chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b''))


def previous_day_window(now):
    """Return the preceding Berlin calendar day as local, timezone-aware bounds."""
    local_now = now.astimezone(BERLIN)
    end = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=1), end


def current_day_window(now):
    """Return today's Berlin day: query only through now, graph over all 24 hours."""
    local_now = now.astimezone(BERLIN)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, local_now, start + timedelta(days=1)


def _axis_limit(peak):
    return max(10, int(math.ceil(max(0, peak) / 10.0)) * 10)


def player_graph(samples, start, end):
    """Build a PNG line graph from UTC-naive DB rows for one local-day window."""
    canvas = Canvas(GRAPH_WIDTH, GRAPH_HEIGHT)
    left, right, top, bottom = 62, 26, 22, 48
    plot_width = GRAPH_WIDTH - left - right
    plot_height = GRAPH_HEIGHT - top - bottom
    grid, axis, line = (66, 70, 77), (143, 148, 156), (87, 242, 135)
    peak = max((int(row['player_count']) for row in samples), default=0)
    limit = _axis_limit(peak)

    for level in range(5):
        y = top + round(plot_height * level / 4)
        canvas.line(left, y, left + plot_width, y, grid)
        value = limit - round(limit * level / 4)
        label = str(value)
        canvas.text(left - len(label) * 8 - 8, y - 5, label)
    canvas.line(left, top, left, top + plot_height, axis)
    canvas.line(left, top + plot_height, left + plot_width, top + plot_height, axis)

    local_start = start.replace(tzinfo=timezone.utc).astimezone(BERLIN)
    for hour in range(0, 25, 4):
        x = left + round(plot_width * hour / 24)
        canvas.line(x, top + plot_height, x, top + plot_height + 4, axis)
        label = f'{hour:02d}'
        canvas.text(x - 6, top + plot_height + 12, label)

    total_seconds = max(1, (end - start).total_seconds())
    previous = None
    for row in sorted(samples, key=lambda item: item['timestamp']):
        observed = row['timestamp']
        if observed.tzinfo is not None:
            observed = observed.astimezone(timezone.utc).replace(tzinfo=None)
        x = left + round(plot_width * (observed - start).total_seconds() / total_seconds)
        y = top + plot_height - round(plot_height * min(limit, max(0, int(row['player_count']))) / limit)
        point = (max(left, min(left + plot_width, x)), max(top, min(top + plot_height, y)), observed)
        if previous and (point[2] - previous[2]).total_seconds() <= 180:
            canvas.line(previous[0], previous[1], point[0], point[1], line)
        previous = point
    return canvas.png()


def duration_text(seconds):
    if not seconds:
        return '—'
    minutes, seconds = divmod(int(seconds), 60)
    return f'{minutes} Min. {seconds:02d} Sek.'


class ServerRecapCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sample_players.start()
        self.initial_recap.start()
        self.refresh_recap.start()
        self.daily_recap.start()

    def cog_unload(self):
        self.sample_players.cancel()
        self.initial_recap.cancel()
        self.refresh_recap.cancel()
        self.daily_recap.cancel()

    @tasks.loop(minutes=1)
    async def sample_players(self):
        tracker = self.bot.get_cog('RoundTracker')
        if tracker is None:
            return
        observed = storage.utcnow().replace(second=0, microsecond=0)
        samples = []
        for srv in config.servers():
            state = tracker.current(srv.id)
            data = state.get('snapshot') if state else None
            count = (data or {}).get('players', {}).get('current')
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                samples.append((srv.id, count))
        if not samples:
            return
        try:
            async with database.transaction() as cur:
                for server_id, count in samples:
                    await cur.execute("""INSERT INTO server_player_snapshots(server_id,player_count,timestamp)
                        SELECT %s,%s,%s WHERE NOT EXISTS (SELECT 1 FROM server_player_snapshots
                        WHERE server_id=%s AND timestamp=%s)""",
                                      (server_id, count, observed, server_id, observed))
            self.bot.health.ok('Spielerzahl-Erfassung')
        except Exception as exc:
            self.bot.health.error('Spielerzahl-Erfassung', exc)

    @sample_players.before_loop
    async def before_samples(self):
        await self.bot.wait_until_ready()

    async def recap_data(self, srv, start, end):
        availability_id = srv.uuid or srv.id
        async with database.transaction() as cur:
            await cur.execute("""SELECT COUNT(*) rounds, AVG(duration_seconds) average_duration
                FROM round_history WHERE server_id=%s AND ended_at>=%s AND ended_at<%s""", (srv.id, start, end))
            summary = await cur.fetchone()
            await cur.execute("""SELECT map_name,COUNT(*) rounds FROM round_history
                WHERE server_id=%s AND ended_at>=%s AND ended_at<%s AND map_name IS NOT NULL
                GROUP BY map_name ORDER BY rounds DESC,map_name ASC LIMIT 1""", (srv.id, start, end))
            top_map = await cur.fetchone()
            await cur.execute("""SELECT winner_faction,COUNT(*) wins FROM round_history
                WHERE server_id=%s AND ended_at>=%s AND ended_at<%s AND winner_faction IS NOT NULL
                GROUP BY winner_faction ORDER BY wins DESC,winner_faction ASC""", (srv.id, start, end))
            wins = await cur.fetchall()
            await cur.execute("""SELECT player_count,timestamp FROM server_player_snapshots
                WHERE server_id=%s AND timestamp>=%s AND timestamp<%s ORDER BY timestamp""", (srv.id, start, end))
            samples = await cur.fetchall()
            await cur.execute("""SELECT COUNT(*) total,SUM(status) online FROM server_uptime
                WHERE server_id=%s AND timestamp>=%s AND timestamp<%s""", (availability_id, start, end))
            availability = await cur.fetchone()
        total = int(availability['total'] or 0)
        online = int(availability['online'] or 0)
        return dict(summary=summary, top_map=top_map, wins=wins, samples=samples,
                    availability=(online / total * 100) if total else None)

    def embed(self, srv, data, start_local, query_end_local):
        summary, samples = data['summary'], data['samples']
        peak = max((int(row['player_count']) for row in samples), default=0)
        average_players = (sum(int(row['player_count']) for row in samples) / len(samples)) if samples else None
        embed = discord.Embed(
            title=f'📈 {srv.title} – Tagesrückblick',
            description=(f"{start_local:%d.%m.%Y} · 00:00–{query_end_local:%H:%M} Uhr (Europe/Berlin)\n"
                         'Spielerzahl im Tagesverlauf.'),
            color=discord.Color.blue())
        embed.add_field(name='Runden', value=str(int(summary['rounds'] or 0)), inline=True)
        embed.add_field(name='Ø Rundendauer', value=duration_text(summary['average_duration']), inline=True)
        embed.add_field(name='Server-Verfügbarkeit',
                        value=f"{data['availability']:.1f}%" if data['availability'] is not None else 'Keine Messwerte', inline=True)
        embed.add_field(name='Spielerzahl',
                        value=(f"Peak: {peak}\nØ: {average_players:.1f}" if average_players is not None else 'Keine Messwerte'),
                        inline=True)
        top_map = data['top_map']
        embed.add_field(name='Meistgespielte Karte',
                        value=(f"{top_map['map_name']} ({top_map['rounds']} Runde(n))" if top_map else 'Keine Runde beendet'),
                        inline=True)
        wins = data['wins']
        embed.add_field(name='Fraktionssiege',
                        value=(' · '.join(f"{row['winner_faction']}: {row['wins']}" for row in wins)[:1024] if wins else 'Keine gewerteten Siege'),
                        inline=False)
        filename = f'{srv.id}-players-{start_local:%Y-%m-%d}.png'
        embed.set_image(url=f'attachment://{filename}')
        embed.set_footer(text='Der Rückblick startet jeden Tag um 00:00 Uhr neu.')
        embed.timestamp = discord.utils.utcnow()
        return embed, filename

    async def publish_recap(self, log_title=None):
        start_local, query_end_local, graph_end_local = current_day_window(datetime.now(BERLIN))
        start = start_local.astimezone(timezone.utc).replace(tzinfo=None)
        query_end = query_end_local.astimezone(timezone.utc).replace(tzinfo=None)
        graph_end = graph_end_local.astimezone(timezone.utc).replace(tzinfo=None)
        for srv in config.servers():
            data = await self.recap_data(srv, start, query_end)
            embed, filename = self.embed(srv, data, start_local, query_end_local)
            self.bot.dispatch('server_recap', srv.id, embed, player_graph(data['samples'], start, graph_end), filename)
        if log_title:
            self.bot.dispatch('bot_log', log_title,
                              f'Tagesrückblick für {start_local:%d.%m.%Y} aktualisiert.', discord.Color.blue())
        self.bot.health.ok('Server-Rückblick')

    @tasks.loop(count=1)
    async def initial_recap(self):
        if config.SERVER_RECAP_CHANNEL_ID:
            try:
                await self.publish_recap('📈 Server-Rückblick initialisiert')
            except Exception as exc:
                self.bot.health.error('Server-Rückblick', exc)

    @initial_recap.before_loop
    async def before_initial_recap(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def refresh_recap(self):
        """Keep the existing daily messages current without producing log spam."""
        if not config.SERVER_RECAP_CHANNEL_ID:
            return
        try:
            await self.publish_recap()
        except Exception as exc:
            self.bot.health.error('Server-Rückblick', exc)

    @refresh_recap.before_loop
    async def before_refresh_recap(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=clock_time(hour=0, minute=0, tzinfo=BERLIN))
    async def daily_recap(self):
        if config.SERVER_RECAP_CHANNEL_ID:
            try:
                await self.publish_recap('📈 Server-Rückblick aktualisiert')
            except Exception as exc:
                self.bot.health.error('Server-Rückblick', exc)

    @daily_recap.before_loop
    async def before_recap(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ServerRecapCog(bot))
