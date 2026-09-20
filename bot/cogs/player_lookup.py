"""Admin-only player lookup backed exclusively by persisted statistics."""
from datetime import datetime
import re

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.permissions import require_admin, valid_steam_id
from infrastructure import database


FOOTER_RE = re.compile(r"Steam64: ([0-9]{17}) · Seite ([0-9]+)/([0-9]+)")


def duration(seconds):
    seconds = int(seconds or 0)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours} Std. {minutes:02d} Min.' if hours else f'{minutes} Min. {seconds:02d} Sek.'


def when(value):
    return value.strftime('%d.%m.%Y %H:%M UTC') if isinstance(value, datetime) else '—'


def title_for(server_id):
    try:
        return config.server(server_id).title
    except KeyError:
        return str(server_id)


def unique_matches(rows):
    """Keep the newest occurrence of a Steam ID, retaining at most Discord's select limit."""
    matches = {}
    for row in rows:
        steam_id = str(row['steam_id'])
        if steam_id not in matches:
            matches[steam_id] = dict(row)
    return list(matches.values())[:25]


def chunks(values, size):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def text_fields(title, entries):
    """Combine list entries into as few Discord fields as their limits permit."""
    fields, current = [], []
    current_length = 0
    for entry in entries:
        entry = entry[:1000]
        extra = len(entry) + (2 if current else 0)
        if current and current_length + extra > 1024:
            fields.append((title if not fields else f'{title} (Fortsetzung)', '\n\n'.join(current)))
            current, current_length = [], 0
        current.append(entry)
        current_length += len(entry) + (2 if len(current) > 1 else 0)
    if current:
        fields.append((title if not fields else f'{title} (Fortsetzung)', '\n\n'.join(current)))
    return fields


def embed_size(embed):
    return len(embed.title or '') + len(embed.description or '') + sum(
        len(field.name) + len(field.value) for field in embed.fields)


class PlayerLookupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Fixed custom IDs let public reports keep working after a bot restart.
        bot.add_view(PublicLookupView(self))

    async def search(self, query):
        async with database.transaction() as cur:
            await cur.execute("""SELECT steam_id,name,last_seen FROM (
                SELECT steam_id,name,last_seen FROM leaderboard WHERE LOWER(name) LIKE LOWER(%s)
                UNION ALL
                SELECT steam_id,name,last_seen FROM player_playtime WHERE LOWER(name) LIKE LOWER(%s)
                ) names ORDER BY last_seen DESC LIMIT 100""", (f'%{query}%', f'%{query}%'))
            return unique_matches(await cur.fetchall())

    async def profile(self, steam_id):
        """Read every persisted player-related record without touching RCON."""
        async with database.transaction() as cur:
            await cur.execute('SELECT * FROM leaderboard WHERE steam_id=%s ORDER BY server_id', (steam_id,))
            leaderboard = await cur.fetchall()
            await cur.execute('SELECT * FROM player_playtime WHERE steam_id=%s ORDER BY server_id', (steam_id,))
            playtime = await cur.fetchall()
            await cur.execute("""SELECT server_id,
                SUM(CASE WHEN date >= DATE_SUB(UTC_DATE(),INTERVAL 6 DAY) THEN kills ELSE 0 END) kills_7d,
                SUM(CASE WHEN date >= DATE_SUB(UTC_DATE(),INTERVAL 6 DAY) THEN deaths ELSE 0 END) deaths_7d,
                SUM(CASE WHEN date >= DATE_SUB(UTC_DATE(),INTERVAL 6 DAY) THEN cash ELSE 0 END) cash_7d,
                SUM(CASE WHEN date >= DATE_SUB(UTC_DATE(),INTERVAL 29 DAY) THEN kills ELSE 0 END) kills_30d,
                SUM(CASE WHEN date >= DATE_SUB(UTC_DATE(),INTERVAL 29 DAY) THEN deaths ELSE 0 END) deaths_30d,
                SUM(CASE WHEN date >= DATE_SUB(UTC_DATE(),INTERVAL 29 DAY) THEN cash ELSE 0 END) cash_30d
                FROM player_daily_stats WHERE steam_id=%s GROUP BY server_id""", (steam_id,))
            daily = await cur.fetchall()
            await cur.execute('SELECT * FROM player_counter_state WHERE steam_id=%s ORDER BY server_id', (steam_id,))
            counters = await cur.fetchall()
            await cur.execute('SELECT * FROM player_faction_stats WHERE steam_id=%s ORDER BY server_id,faction', (steam_id,))
            factions = await cur.fetchall()
            await cur.execute('SELECT server_id,total_ping,ping_samples FROM player_ping_stats WHERE steam_id=%s ORDER BY server_id', (steam_id,))
            pings = await cur.fetchall()
            await cur.execute('SELECT * FROM global_bans WHERE steam_id=%s ORDER BY issued_at DESC,id DESC', (steam_id,))
            bans = await cur.fetchall()
            await cur.execute('SELECT * FROM banned_players WHERE steam_id=%s ORDER BY server_id', (steam_id,))
            observed_bans = await cur.fetchall()
            await cur.execute('SELECT * FROM admin_targets WHERE steam_id=%s', (steam_id,))
            target = await cur.fetchone()
            await cur.execute('SELECT * FROM admin_jobs WHERE steam_id=%s ORDER BY id DESC', (steam_id,))
            jobs = await cur.fetchall()

        servers = {}
        names = set()
        for row in leaderboard:
            servers.setdefault(row['server_id'], {}).update(leaderboard=row)
            names.add(row['name'])
        for row in playtime:
            servers.setdefault(row['server_id'], {}).update(playtime=row)
            names.add(row['name'])
        for row in daily:
            servers.setdefault(row['server_id'], {}).update(daily=row)
        for row in counters:
            servers.setdefault(row['server_id'], {}).update(counter=row)
        for row in factions:
            servers.setdefault(row['server_id'], {}).setdefault('factions', []).append(row)
        for row in pings:
            servers.setdefault(row['server_id'], {}).update(ping=row)
        for row in observed_bans:
            servers.setdefault(row['server_id'], {}).update(observed_ban=row)
        return dict(steam_id=steam_id, names=sorted(names), servers=servers, bans=bans,
                    target=target, jobs=jobs,
                    exists=bool(servers or bans or target or jobs))

    def pages(self, profile):
        steam_id, servers = profile['steam_id'], profile['servers']
        name = profile['names'][0] if profile['names'] else 'Unbekannter Spieler'
        overview = discord.Embed(title=f'🔍 Spielerprofil: {name}', color=discord.Color.blue())
        all_names = ' · '.join(profile['names']) or '—'
        shown_names = all_names if len(all_names) <= 1000 else all_names[:990] + ' …'
        overview.description = f'**Steam64-ID:** `{steam_id}`\n**Bekannte Namen:** {shown_names}'
        if profile['target']:
            target = profile['target']
            overview.add_field(name='Aktuelles Admin-Ziel', value=(
                f"**Aktion:** {target['desired']}\n**Version:** {target['version']}\n"
                f"**Bis:** {when(target['expires_at'])}\n**Admin:** {target['admin_mention']}\n"
                f"**Grund:** {target['reason'][:700]}"), inline=False)
        else:
            overview.add_field(name='Aktuelles Admin-Ziel', value='Keines gespeichert.', inline=False)
        overview.add_field(name='Gespeicherte Einträge', value=(
            f"Server: {len(servers)} · Ban-Historie: {len(profile['bans'])} · "
            f"Admin-Aufträge: {len(profile['jobs'])}"), inline=False)

        detail_fields = []
        if len(all_names) > 1000:
            detail_fields.extend(text_fields('🪪 Bekannte Namen', profile['names']))

        for server_id, values in sorted(servers.items()):
            board, play, daily = values.get('leaderboard', {}), values.get('playtime', {}), values.get('daily', {})
            current, ping = values.get('counter', {}), values.get('ping', {})
            kills, deaths, cash = board.get('lifetime_kills', 0), board.get('lifetime_deaths', 0), board.get('lifetime_cash', 0)
            average_ping = (int(ping['total_ping']) / int(ping['ping_samples'])) if ping.get('ping_samples') else None
            factions = values.get('factions', [])
            faction_text = ' · '.join(f"{item['faction']}: {item['times_seen']} Beitritte" for item in factions) or 'Keine Daten'
            faction_summary = faction_text if len(faction_text) <= 280 else faction_text[:270] + ' … (vollständig auf Folgeseite)'
            ping_text = f'{average_ping:.0f} ms' if average_ping is not None else '—'
            server_text = (
                f"**Spielzeit:** {duration(play.get('playtime_seconds'))} · **Zuletzt:** {when(play.get('last_seen') or board.get('last_seen'))}\n"
                f"**All-Time:** K {kills} · T {deaths} · K/D {kills / max(1, deaths):.2f} · Cash {cash}\n"
                f"**Runde:** K {current.get('kills', board.get('current_match_kills', 0))} · "
                f"T {current.get('deaths', board.get('current_match_deaths', 0))} · Cash {current.get('cash', board.get('current_match_cash', 0))} · "
                f"Qualität {current.get('quality', '—')}\n"
                f"**7/30 Tage:** K {daily.get('kills_7d', 0)}/{daily.get('kills_30d', 0)} · "
                f"T {daily.get('deaths_7d', 0)}/{daily.get('deaths_30d', 0)} · Cash {daily.get('cash_7d', 0)}/{daily.get('cash_30d', 0)}\n"
                f"**Ping:** {ping_text} · **Server-Ban:** {'ja' if values.get('observed_ban') else 'nein'}\n"
                f"**Fraktionsbeitritte:** {faction_summary}")
            overview.add_field(name=f'📊 {title_for(server_id)}', value=server_text, inline=False)
            if len(faction_text) > 280:
                detail_fields.extend(text_fields(f'🏴 Fraktionsbeitritte – {title_for(server_id)}',
                                                 [f"{item['faction']}: {item['times_seen']} Beitritte" for item in factions]))

        ban_entries = [
            f"**{when(ban['issued_at'])} · {ban['status']}**\nAdmin: {ban['admin_mention']} · Dauer: {ban['duration_str']} · Bis: {when(ban['expires_at'])}\nGrund: {ban['reason'][:650]}"
            for ban in profile['bans']]
        job_entries = [
            f"**{title_for(job['server_id'])} · {job['action']} · {job['status']}**\n"
            f"Versuche: {job['attempts']} · Nächster Versuch: {when(job['next_attempt'])}\n"
            f"Letzter Fehler: {job['last_error'] or '—'}"
            for job in profile['jobs']]
        detail_fields.extend(text_fields('🛡️ Ban-Historie', ban_entries or ['Keine Einträge.']))
        detail_fields.extend(text_fields('⚙️ Admin-Aufträge', job_entries or ['Keine Einträge.']))

        # Start with the overview and only create another page once Discord's field or
        # 6,000-character embed limits are reached.
        pages, current = [overview], overview
        for field_name, field_value in detail_fields:
            if len(current.fields) >= 25 or embed_size(current) + len(field_name) + len(field_value) > 5700:
                current = discord.Embed(title=f'📚 Weitere Profildaten: {name}', color=discord.Color.blue())
                pages.append(current)
            current.add_field(name=field_name, value=field_value, inline=False)
        for index, embed in enumerate(pages, start=1):
            embed.set_footer(text=f'Steam64: {steam_id} · Seite {index}/{len(pages)}')
            embed.timestamp = discord.utils.utcnow()
        return pages

    async def send_private_profile(self, interaction, steam_id):
        profile = await self.profile(steam_id)
        if not profile['exists']:
            await interaction.followup.send('Kein gespeicherter Spieler zu dieser Steam64-ID gefunden.', ephemeral=True)
            return
        pages = self.pages(profile)
        await interaction.followup.send(embed=pages[0], view=PrivateLookupView(self, interaction.user.id, profile, pages), ephemeral=True)

    @app_commands.command(name='lookup', description='Zeigt alle gespeicherten Daten eines Spielers.')
    @app_commands.describe(query='Spielername oder 17-stellige Steam64-ID')
    async def lookup(self, interaction: discord.Interaction, query: str):
        if not await require_admin(interaction):
            return
        query = query.strip()
        await interaction.response.defer(ephemeral=True)
        try:
            if valid_steam_id(query):
                await self.send_private_profile(interaction, query)
                return
            if not query:
                await interaction.followup.send('Bitte Name oder Steam64-ID angeben.', ephemeral=True)
                return
            matches = await self.search(query)
            if not matches:
                await interaction.followup.send('Kein passender gespeicherter Spieler gefunden.', ephemeral=True)
            elif len(matches) == 1:
                await self.send_private_profile(interaction, str(matches[0]['steam_id']))
            else:
                await interaction.followup.send('Mehrere Spieler gefunden – bitte auswählen:',
                                                  view=LookupMatchView(self, interaction.user.id, matches), ephemeral=True)
        except Exception as exc:
            self.bot.health.error('Spieler-Lookup', exc)
            await interaction.followup.send('Die Spielerdatenbank ist momentan nicht verfügbar.', ephemeral=True)


class LookupMatchSelect(discord.ui.Select):
    def __init__(self, matches):
        options = [discord.SelectOption(label=str(row['name'])[:100], value=str(row['steam_id']),
                   description=f"{row['steam_id']} · zuletzt {when(row['last_seen'])}"[:100]) for row in matches]
        super().__init__(placeholder='Spieler auswählen …', options=options)

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await self.view.cog.send_private_profile(interaction, self.values[0])


class LookupMatchView(discord.ui.View):
    def __init__(self, cog, owner, matches):
        super().__init__(timeout=300)
        self.cog, self.owner = cog, owner
        self.add_item(LookupMatchSelect(matches))

    async def interaction_check(self, interaction):
        return interaction.user.id == self.owner and await require_admin(interaction)


class PrivateLookupView(discord.ui.View):
    def __init__(self, cog, owner, profile, pages, page=0):
        super().__init__(timeout=300)
        self.cog, self.owner, self.profile, self.pages, self.page = cog, owner, profile, pages, page
        self.previous.disabled = page == 0
        self.next.disabled = page >= len(pages) - 1

    async def interaction_check(self, interaction):
        return interaction.user.id == self.owner and await require_admin(interaction)

    @discord.ui.button(label='Zurück', style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page -= 1
        self.previous.disabled = self.page == 0
        self.next.disabled = False
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    @discord.ui.button(label='Weiter', style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page += 1
        self.previous.disabled = False
        self.next.disabled = self.page >= len(self.pages) - 1
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    @discord.ui.button(label='Öffentlich machen', style=discord.ButtonStyle.primary, emoji='📢')
    async def publish(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        view = PublicLookupView(self.cog, self.profile['steam_id'])
        self.cog.bot.dispatch('public_lookup', interaction.channel_id, self.pages[0], view)
        self.cog.bot.dispatch('bot_log', '📢 Spieler-Lookup veröffentlicht',
                              f"{interaction.user.mention} veröffentlichte `{self.profile['steam_id']}`.", discord.Color.blue())
        await interaction.followup.send('Der vollständige Bericht wurde in diesem Channel veröffentlicht.', ephemeral=True)


class PublicLookupView(discord.ui.View):
    def __init__(self, cog, steam_id=None, page=0):
        super().__init__(timeout=None)
        self.cog, self.steam_id, self.page = cog, steam_id, page
        self.previous.disabled = page == 0

    async def source(self, interaction):
        footer = interaction.message.embeds[0].footer.text if interaction.message.embeds else ''
        match = FOOTER_RE.fullmatch(footer or '')
        if not match:
            return None, None
        profile = await self.cog.profile(match.group(1))
        return profile, max(0, int(match.group(2)) - 1)

    async def interaction_check(self, interaction):
        return await require_admin(interaction)

    async def change_page(self, interaction, delta):
        try:
            profile, current = await self.source(interaction)
            if not profile or not profile['exists']:
                await interaction.response.send_message('Dieser Bericht enthält keine abrufbaren Daten mehr.', ephemeral=True)
                return
            pages = self.cog.pages(profile)
            page = max(0, min(len(pages) - 1, current + delta))
            await interaction.response.edit_message(embed=pages[page], view=PublicLookupView(self.cog, profile['steam_id'], page))
        except Exception as exc:
            self.cog.bot.health.error('Öffentlicher Spieler-Lookup', exc)
            await interaction.response.send_message('Der Bericht konnte nicht aktualisiert werden.', ephemeral=True)

    @discord.ui.button(label='Zurück', style=discord.ButtonStyle.secondary, custom_id='lookup_public_previous')
    async def previous(self, interaction, button):
        await self.change_page(interaction, -1)

    @discord.ui.button(label='Weiter', style=discord.ButtonStyle.secondary, custom_id='lookup_public_next')
    async def next(self, interaction, button):
        await self.change_page(interaction, 1)

    @discord.ui.button(label='Löschen', style=discord.ButtonStyle.danger, custom_id='lookup_public_delete', emoji='🗑️')
    async def delete(self, interaction, button):
        await interaction.response.defer()
        await interaction.message.delete()
        self.cog.bot.dispatch('bot_log', '🗑️ Spieler-Lookup gelöscht',
                              f"{interaction.user.mention} löschte einen veröffentlichten Lookup.", discord.Color.blue())


async def setup(bot):
    await bot.add_cog(PlayerLookupCog(bot))
