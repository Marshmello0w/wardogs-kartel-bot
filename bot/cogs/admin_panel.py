import discord
from discord.ext import commands, tasks
from discord import app_commands
from core import config
from core.permissions import require_admin, valid_steam_id
from core.runtime import read_state, write_state
from infrastructure import database
from services.ban_service import BanService


class HistoryView(discord.ui.View):
    def __init__(self, rows, steam_id, owner):
        super().__init__(timeout=300)
        self.rows, self.steam_id, self.owner, self.page = rows, steam_id, owner, 0

    def embed(self):
        embed = discord.Embed(title=f'🔍 Ban-Historie: {self.steam_id}', color=discord.Color.blue())
        for row in self.rows[self.page * 4:(self.page + 1) * 4]:
            expiry = str(row['expires_at']) + ' UTC' if row['expires_at'] else 'Nie'
            if row['status'].startswith('external'):
                expiry = 'Unbekannt (externer Server-Ban)'
            text = (f"**Admin:** {row['admin_mention']}\n**Dauer:** {row['duration_str']}\n"
                    f"**Bis:** {expiry}\n**Status:** {row['status']}\n**Grund:** {row['reason'][:600]}")
            embed.add_field(name=f"Ban am {row['issued_at']} UTC", value=text[:1024], inline=False)
        embed.set_footer(text=f'Seite {self.page + 1}/{max(1, (len(self.rows) + 3) // 4)}')
        return embed

    async def interaction_check(self, interaction):
        return interaction.user.id == self.owner and await require_admin(interaction)

    @discord.ui.button(label='Zurück', style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label='Weiter', style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page = min(max(0, (len(self.rows) - 1) // 4), self.page + 1)
        await interaction.response.edit_message(embed=self.embed(), view=self)


async def show_history(interaction, steam_id):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    if not valid_steam_id(steam_id):
        await interaction.followup.send('Steam64 ID muss aus 17 Ziffern bestehen.', ephemeral=True)
        return
    try:
        async with database.transaction() as cur:
            await cur.execute('SELECT * FROM global_bans WHERE steam_id=%s ORDER BY issued_at DESC,id DESC', (steam_id,))
            rows = await cur.fetchall()
        if not rows:
            await interaction.followup.send('Keine Ban-Einträge vorhanden.', ephemeral=True)
            return
        view = HistoryView(rows, steam_id, interaction.user.id)
        await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)
    except Exception as exc:
        interaction.client.health.error('Ban-Historie', exc)
        await interaction.followup.send('Die Datenbank ist momentan nicht verfügbar.', ephemeral=True)


class BanModal(discord.ui.Modal, title='Spieler Global Bannen'):
    steam_id = discord.ui.TextInput(label='Steam64 ID', min_length=17, max_length=17)
    reason = discord.ui.TextInput(label='Ban Grund', style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, bot, duration_value, duration_label):
        super().__init__()
        self.bot, self.duration_value, self.duration_label = bot, duration_value, duration_label

    async def on_submit(self, interaction):
        await submit_action(interaction, self.steam_id.value.strip(), 'ban', self.reason.value.strip(),
                            self.duration_value, self.duration_label)


async def submit_action(interaction, steam_id, action, reason='', hours=0, label='Permanent'):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    if not valid_steam_id(steam_id):
        await interaction.followup.send('Steam64 ID muss aus 17 Ziffern bestehen.', ephemeral=True)
        return
    try:
        service = interaction.client.get_cog('AdminPanelCog').service
        await service.decide(steam_id, action, reason, interaction.user.mention, hours, label)
        await interaction.followup.send(
            '✅ Auftrag dauerhaft gespeichert. Die Ausführung auf den drei Servern wird zentral protokolliert. '
            'Bei Nichterreichbarkeit wird automatisch erneut versucht.', ephemeral=True)
    except Exception as exc:
        interaction.client.health.error('Admin-Aktion speichern', exc)
        await interaction.followup.send('❌ Auftrag konnte nicht gespeichert werden. Es wurde keine neue Serveraktion ausgelöst.', ephemeral=True)


class BanDurationSelect(discord.ui.Select):
    def __init__(self, bot):
        self.bot = bot
        choices = [('24 Stunden', 24), ('3 Tage', 72), ('7 Tage', 168), ('2 Wochen', 336), ('1 Monat', 720), ('Permanent', 0)]
        super().__init__(placeholder='Bann-Dauer ab jetzt …', options=[discord.SelectOption(label=k, value=str(v)) for k,v in choices])

    async def callback(self, interaction):
        if await require_admin(interaction):
            label = next(o.label for o in self.options if o.value == self.values[0])
            await interaction.response.send_modal(BanModal(self.bot, int(self.values[0]), label))


class UnbanModal(discord.ui.Modal, title='Spieler Global Entbannen'):
    steam_id = discord.ui.TextInput(label='Steam64 ID', min_length=17, max_length=17)

    async def on_submit(self, interaction):
        await submit_action(interaction, self.steam_id.value.strip(), 'unban')


class LookupModal(discord.ui.Modal, title='Spieler Ban-Historie'):
    steam_id = discord.ui.TextInput(label='Steam64 ID', min_length=17, max_length=17)

    async def on_submit(self, interaction):
        await show_history(interaction, self.steam_id.value.strip())


class AdminPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def interaction_check(self, interaction):
        return await require_admin(interaction)

    @discord.ui.button(label='Spieler Bannen', style=discord.ButtonStyle.danger, custom_id='admin_panel_ban', emoji='🔨')
    async def ban_button(self, interaction, button):
        view = discord.ui.View(timeout=300)
        view.add_item(BanDurationSelect(self.bot))
        await interaction.response.send_message('Wie lange soll der Ban ab jetzt gelten?', view=view, ephemeral=True)

    @discord.ui.button(label='Spieler Entbannen', style=discord.ButtonStyle.success, custom_id='admin_panel_unban', emoji='🕊️')
    async def unban_button(self, interaction, button):
        await interaction.response.send_modal(UnbanModal())

    @discord.ui.button(label='Ban Historie', style=discord.ButtonStyle.secondary, custom_id='admin_panel_lookup', emoji='🔍')
    async def lookup_button(self, interaction, button):
        await interaction.response.send_modal(LookupModal())


class AdminPanelCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.service = BanService(bot)
        self.bot.add_view(AdminPanelView(bot))
        self.worker.start()
        self.ensure_panel.start()

    def cog_unload(self):
        self.worker.cancel()
        self.ensure_panel.cancel()

    @tasks.loop(seconds=5)
    async def worker(self):
        try:
            await self.service.tick()
            self.bot.health.ok('Admin-Verarbeitung')
        except Exception as exc:
            self.bot.health.error('Admin-Verarbeitung', exc)

    @tasks.loop(minutes=1)
    async def ensure_panel(self):
        if not config.ADMIN_PANEL_CHANNEL_ID:
            return
        try:
            channel = self.bot.get_channel(int(config.ADMIN_PANEL_CHANNEL_ID)) or await self.bot.fetch_channel(int(config.ADMIN_PANEL_CHANNEL_ID))
            saved = read_state(config.ADMIN_PANEL_MSG_ID_FILE, {})
            message = None
            if saved.get('message_id'):
                try:
                    message = await channel.fetch_message(saved['message_id'])
                except discord.NotFound:
                    pass
            embed = discord.Embed(title='🛡️ Kartell Global Admin Panel', color=discord.Color.dark_theme(),
                description='Spieler auf allen drei Servern bannen oder entbannen.\n'
                'Zeitliche Bans laufen ab Vergabe. Offline-Aufträge werden bis zum Ablauf erneut versucht.\n'
                'Alle Aktionen werden zentral protokolliert.')
            if message is None:
                message = await channel.send(embed=embed, view=AdminPanelView(self.bot))
                write_state(config.ADMIN_PANEL_MSG_ID_FILE, {'message_id': message.id})
            elif not message.embeds or message.embeds[0].to_dict() != embed.to_dict():
                await message.edit(embed=embed, view=AdminPanelView(self.bot))
            self.bot.health.ok('Admin-Panel')
        except Exception as exc:
            self.bot.health.error('Admin-Panel', exc)

    @worker.before_loop
    @ensure_panel.before_loop
    async def ready(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name='adminpanel', description='Zeigt das globale Admin-Panel.')
    async def admin_panel(self, interaction: discord.Interaction):
        if await require_admin(interaction):
            await interaction.response.send_message('Globales Admin-Panel', view=AdminPanelView(self.bot), ephemeral=True)

    @app_commands.command(name='ban_lookup', description='Sucht die Ban-Historie eines Spielers.')
    async def ban_lookup(self, interaction: discord.Interaction, steam_id: str):
        await show_history(interaction, steam_id.strip())


async def setup(bot):
    await bot.add_cog(AdminPanelCog(bot))
