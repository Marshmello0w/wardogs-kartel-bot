import asyncio
import time
import discord
from discord.ext import commands, tasks
from core import config
from core.permissions import valid_steam_id
from core.runtime import read_state, write_state
from domain import stats

SORT_ORDER = ('kd', 'cash', 'playtime')


def following_sort(sort_by):
    return SORT_ORDER[(SORT_ORDER.index(sort_by) + 1) % len(SORT_ORDER)] if sort_by in SORT_ORDER else 'kd'


def playtime_text(seconds):
    hours, minutes = divmod(max(0, int(seconds or 0)) // 60, 60)
    return f'{hours} Std. {minutes:02d} Min.'


class EphemeralTimeframeDropdown(discord.ui.Select):
    def __init__(self, current_tf):
        super().__init__(placeholder='Zeitraum wählen', options=[
            discord.SelectOption(label=label, value=value, default=value == current_tf)
            for value,label in [('7d','Letzte 7 Tage'),('30d','Letzte 30 Tage'),('all','All-Time')]])

    async def callback(self, interaction):
        self.view.current_tf = self.values[0]
        await self.view.update_message(interaction)


class EphemeralSortDropdown(discord.ui.Select):
    def __init__(self, current_sort):
        super().__init__(placeholder='Sortierung', options=[
            discord.SelectOption(label=label, value=value, default=value == current_sort)
            for value,label in [('kd','Nach K/D'),('cash','Nach Cash'),('playtime','Nach Spielzeit')]])

    async def callback(self, interaction):
        self.view.current_sort = self.values[0]
        await self.view.update_message(interaction)


class EphemeralLeaderboardView(discord.ui.View):
    def __init__(self, cog, server_id, current_tf='7d', current_sort='kd'):
        super().__init__(timeout=300)
        self.cog, self.server_id = cog, server_id
        self.current_tf, self.current_sort = current_tf, current_sort
        self.add_item(EphemeralTimeframeDropdown(current_tf))
        self.add_item(EphemeralSortDropdown(current_sort))

    async def update_message(self, interaction):
        await interaction.response.defer()
        try:
            embed = await self.cog.generate_embed(self.server_id, self.current_tf, self.current_sort)
            view = EphemeralLeaderboardView(self.cog, self.server_id, self.current_tf, self.current_sort)
            await interaction.edit_original_response(embed=embed, view=view)
        except Exception as exc:
            self.cog.bot.health.error('Leaderboard-Menü', exc)
            await interaction.followup.send('Statistiken sind momentan nicht verfügbar.', ephemeral=True)


class PublicLeaderboardDropdown(discord.ui.Select):
    def __init__(self, cog, server_id):
        self.cog, self.server_id = cog, server_id
        super().__init__(placeholder='Auswahl / Menü öffnen …', custom_id=f'pub_lb_{server_id}',
            options=[discord.SelectOption(label=label, value=value) for value,label in
                     [('7d','Letzte 7 Tage'),('30d','Letzte 30 Tage'),('all','All-Time')]])

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            embed = await self.cog.generate_embed(self.server_id, self.values[0])
            await interaction.followup.send(embed=embed,
                view=EphemeralLeaderboardView(self.cog, self.server_id, self.values[0]), ephemeral=True)
        except Exception as exc:
            self.cog.bot.health.error('Leaderboard-Menü', exc)
            await interaction.followup.send('Statistiken sind momentan nicht verfügbar.', ephemeral=True)


class PlayerRankModal(discord.ui.Modal, title='Eigenen Platz im Leaderboard finden'):
    steam_id = discord.ui.TextInput(label='Steam64 ID', min_length=17, max_length=17)

    def __init__(self, cog, server_id):
        super().__init__()
        self.cog, self.server_id = cog, server_id

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        steam_id = self.steam_id.value.strip()
        if not valid_steam_id(steam_id):
            await interaction.followup.send('Steam64 ID muss aus 17 Ziffern bestehen.', ephemeral=True)
            return
        try:
            embed = discord.Embed(title=f'📊 Deine Statistiken – {config.server(self.server_id).title}', color=discord.Color.gold())
            for key,title in [('all','All-Time'),('30d','Letzte 30 Tage'),('7d','Letzte 7 Tage')]:
                row = await stats.ranking(self.server_id, key, steam_id=steam_id)
                if row:
                    embed.add_field(name=f"{title} (Platz #{row['player_rank']})",
                        value=f"Kills: {row['kills']} | Deaths: {row['deaths']}\nK/D: {row['kd']:.2f} | Cash: {row['cash']} USD", inline=False)
                else:
                    embed.add_field(name=title, value='Keine gewerteten Daten vorhanden (oder Spieler ausgeschlossen).', inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            self.cog.bot.health.error('Persönlicher Rang', exc)
            await interaction.followup.send('Statistiken sind momentan nicht verfügbar.', ephemeral=True)


class PublicLeaderboardView(discord.ui.View):
    def __init__(self, cog, server_id):
        super().__init__(timeout=None)
        self.cog, self.server_id = cog, server_id
        self.add_item(PublicLeaderboardDropdown(cog, server_id))
        button = discord.ui.Button(label='Eigenen Platz finden', custom_id=f'lb_rank_btn_{server_id}',
                                   style=discord.ButtonStyle.secondary, row=1)
        button.callback = self.find_rank_callback
        self.add_item(button)

    async def find_rank_callback(self, interaction):
        await interaction.response.send_modal(PlayerRankModal(self.cog, self.server_id))


class Leaderboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_sample = {}
        self.state_file = 'leaderboard_state.json'
        for srv in config.servers():
            bot.add_view(PublicLeaderboardView(self, srv.id))
        self.update_leaderboard_data.start()
        self.update_leaderboard_ui.start()

    def cog_unload(self):
        self.update_leaderboard_data.cancel()
        self.update_leaderboard_ui.cancel()

    async def generate_embed(self, server_id, timeframe='7d', sort_by='kd'):
        rows = await stats.ranking(server_id, timeframe, sort_by)
        labels = {'7d':'Letzte 7 Tage','30d':'Letzte 30 Tage','all':'All-Time'}
        sort_labels = {'kd':'K/D','cash':'Cash','playtime':'Spielzeit'}
        embed = discord.Embed(title=f"🏆 {config.server(server_id).title} – {labels[timeframe]} · {sort_labels[sort_by]}", color=discord.Color.gold())
        for row in rows:
            embed.add_field(name=f"{row['player_rank']}. {row['name']}"[:256],
                value=(f"Kills: {row['kills']} | Deaths: {row['deaths']} | K/D: {row['kd']:.2f} | Cash: {row['cash']} USD\n"
                       f"Spielzeit: {playtime_text(row['playtime_seconds'])}"), inline=False)
        if not rows:
            embed.description = 'Noch keine gewerteten Spielerdaten vorhanden.'
        tracker = self.bot.get_cog('RoundTracker')
        updated_at = discord.utils.utcnow()
        if not tracker or not tracker.current(server_id):
            embed.add_field(name='⚠️ Erfassung unterbrochen',
                            value=f'Letzte Aktualisierung: <t:{int(updated_at.timestamp())}:R>', inline=False)
        else:
            # Footer-Texte unterstützen Discords dynamische Zeitstempel nicht.
            # In Embed-Feldern wird <t:...:R> hingegen als „vor X Sekunden“ gerendert.
            embed.add_field(name='Letzte Aktualisierung',
                            value=f'<t:{int(updated_at.timestamp())}:R>', inline=False)
        return embed

    async def sample(self, srv):
        tracker = self.bot.get_cog('RoundTracker')
        if not tracker:
            return
        state = tracker.current(srv.id)
        interval = 5 if state and state['near_end'] else 15
        if time.monotonic() - self.last_sample.get(srv.id, 0) < interval:
            return
        try:
            result = await tracker.sample_players(srv.id)
            if result is not None:
                state, players = result
                await stats.update_players(srv.id, state, players)
                challenges = self.bot.get_cog('ChallengeQuests')
                if challenges is not None:
                    try:
                        await challenges.process_sample(srv.id, state, players)
                    except Exception as exc:
                        self.bot.health.error(challenges.health_key, exc)
                self.bot.dispatch('player_sampled', srv.id, state, players)
                self.last_sample[srv.id] = time.monotonic()
                self.bot.health.ok(f'Leaderboard-Erfassung {srv.title}')
        except Exception as exc:
            self.bot.health.error(f'Leaderboard-Erfassung {srv.title}', exc)

    @tasks.loop(seconds=5)
    async def update_leaderboard_data(self):
        await asyncio.gather(*(self.sample(s) for s in config.servers() if s.enabled))

    @tasks.loop(seconds=30)
    async def update_leaderboard_ui(self):
        if not config.LEADERBOARD_CHANNEL_ID:
            return
        try:
            channel = self.bot.get_channel(int(config.LEADERBOARD_CHANNEL_ID)) or await self.bot.fetch_channel(int(config.LEADERBOARD_CHANNEL_ID))
            saved = read_state(self.state_file, {})
            for srv in config.servers():
                if not srv.enabled:
                    continue
                try:
                    entry = saved.get(srv.id, {})
                    sort_by = entry.get('next_sort', 'kd')
                    if sort_by not in SORT_ORDER:
                        sort_by = 'kd'
                    embed = await self.generate_embed(srv.id, sort_by=sort_by)
                    view = PublicLeaderboardView(self, srv.id)
                    message = None
                    if entry.get('msg_id'):
                        try:
                            message = await channel.fetch_message(entry['msg_id'])
                        except discord.NotFound:
                            pass
                    if message is None:
                        message = await channel.send(embed=embed, view=view)
                    elif not message.embeds or message.embeds[0].to_dict() != embed.to_dict():
                        await message.edit(embed=embed, view=view)
                    else:
                        continue
                    saved[srv.id] = {'msg_id': message.id, 'next_sort': following_sort(sort_by)}
                    write_state(self.state_file, saved)
                    self.bot.health.ok(f'Leaderboard-Panel {srv.title}')
                except Exception as exc:
                    self.bot.health.error(f'Leaderboard-Panel {srv.title}', exc)
            self.bot.health.ok('Leaderboard-Panels')
        except Exception as exc:
            self.bot.health.error('Leaderboard-Panels', exc)

    @update_leaderboard_data.before_loop
    @update_leaderboard_ui.before_loop
    async def ready(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Leaderboard(bot))
