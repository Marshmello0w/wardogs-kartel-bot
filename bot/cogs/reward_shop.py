"""Bot-owned quest reward execution, VIP capacity and Discord administration."""
from calendar import monthrange
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.permissions import require_admin, valid_steam_id
from infrastructure import database
from services.rcon import RconError


UTC = timezone.utc
VIP_OCCUPYING = ('pending_activation', 'active', 'expired_pending_removal')
TEAMS = frozenset(config.QUEST_TEAMS)


def add_month(stamp):
    """Calendar-month expiry, preserving time and clamping end-of-month dates."""
    month = stamp.month % 12 + 1
    year = stamp.year + (stamp.month == 12)
    return stamp.replace(year=year, month=month, day=min(stamp.day, monthrange(year, month)[1]))


def valid_faction(value):
    return str(value or '').strip() if str(value or '').strip() in TEAMS else None


class VipPendingView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction):
        return await require_admin(interaction)

    @discord.ui.button(label='Aktiv', style=discord.ButtonStyle.success, custom_id='vip_activate')
    async def activate(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        result = await self.cog.activate_from_message(interaction.message.id, interaction.user)
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label='Stornieren', style=discord.ButtonStyle.danger, custom_id='vip_cancel')
    async def cancel(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        result = await self.cog.cancel_from_message(interaction.message.id, interaction.user)
        await interaction.followup.send(result, ephemeral=True)


class VipExpiryView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction):
        return await require_admin(interaction)

    @discord.ui.button(label='Entfernt', style=discord.ButtonStyle.secondary, custom_id='vip_removed')
    async def removed(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        result = await self.cog.remove_from_message(interaction.message.id, interaction.user)
        await interaction.followup.send(result, ephemeral=True)


class RewardShopCog(commands.Cog):
    health_key = 'Belohnungsshop'

    def __init__(self, bot):
        self.bot = bot
        bot.add_view(VipPendingView(self))
        bot.add_view(VipExpiryView(self))
        self.process_requests.start()
        self.expire_vips.start()

    def cog_unload(self):
        self.process_requests.cancel()
        self.expire_vips.cancel()

    async def _debit(self, cur, steam_id, amount, kind, reference, reason):
        await cur.execute("INSERT INTO quest_points(steam_id) VALUES (%s) ON DUPLICATE KEY UPDATE steam_id=steam_id", (steam_id,))
        await cur.execute("SELECT points FROM quest_points WHERE steam_id=%s FOR UPDATE", (steam_id,))
        points = int((await cur.fetchone())['points'])
        if points < amount:
            return False
        await cur.execute("""INSERT INTO quest_point_ledger(steam_id,amount,kind,reference_key,reason)
            VALUES (%s,%s,%s,%s,%s)""", (steam_id, -amount, kind, reference, reason))
        await cur.execute("UPDATE quest_points SET points=points-%s,updated_at=UTC_TIMESTAMP() WHERE steam_id=%s", (amount, steam_id))
        return True

    async def _refund(self, cur, row, admin):
        cost = int(row['points_cost'])
        if not cost:
            return
        await cur.execute("""INSERT INTO quest_point_ledger(steam_id,amount,kind,reference_key,reason,admin_user_id,admin_mention)
            VALUES (%s,%s,'vip_refund',%s,%s,%s,%s)""",
            (row['steam_id'], cost, f"vip:{row['id']}:refund", 'VIP-Auftrag storniert', str(admin.id), admin.mention[:100]))
        await cur.execute("INSERT INTO quest_points(steam_id,points) VALUES (%s,%s) ON DUPLICATE KEY UPDATE points=points+VALUES(points)",
                          (row['steam_id'], cost))

    async def _slots_available(self, cur, server_id, *, excluding=None):
        clause, args = '', [server_id, *VIP_OCCUPYING]
        if excluding is not None:
            clause = ' AND id<>%s'
            args.append(excluding)
        await cur.execute(f"SELECT COUNT(*) count FROM vip_memberships WHERE server_id=%s AND status IN (%s,%s,%s){clause} FOR UPDATE", args)
        return int((await cur.fetchone())['count']) < config.VIP_SLOT_LIMIT

    async def _claim_request(self, request_id):
        async with database.transaction() as cur:
            await cur.execute("SELECT * FROM reward_requests WHERE id=%s FOR UPDATE", (request_id,))
            row = await cur.fetchone()
            if not row or row['status'] != 'pending':
                return None
            await cur.execute("UPDATE reward_requests SET status='processing' WHERE id=%s", (request_id,))
            return row

    async def _finish_request(self, request_id, status, reason=None):
        async with database.transaction() as cur:
            await cur.execute("UPDATE reward_requests SET status=%s,reason=%s,completed_at=UTC_TIMESTAMP() WHERE id=%s", (status, str(reason or '')[:255] or None, request_id))

    async def _faction_request(self, row):
        server_id, steam_id, target = row['server_id'], str(row['steam_id']), valid_faction(row['faction'])
        if not target:
            await self._finish_request(row['id'], 'rejected', 'Ungültige Fraktion')
            return
        try:
            players = await self.bot.rcon.players(server_id)
            player = next((p for p in players if str(p.get('steamId')) == steam_id), None)
            if not player:
                await self._finish_request(row['id'], 'rejected', 'Spieler ist nicht online')
                return
            current = valid_faction(player.get('faction'))
            if current == target:
                await self._finish_request(row['id'], 'rejected', 'Spieler ist bereits in dieser Fraktion')
                return
            sizes = {team: sum(valid_faction(p.get('faction')) == team for p in players) for team in TEAMS}
            if current:
                sizes[current] -= 1
            sizes[target] += 1
            if max(sizes.values()) - min(sizes.values()) > 4:
                await self._finish_request(row['id'], 'rejected', 'Die Team-Balance würde mehr als vier Spieler abweichen')
                return
            await self.bot.rcon.request(server_id, 'PATCH', f'/v1/players/{steam_id}', payload={'faction': target})
            await self.bot.rcon.request(server_id, 'POST', f'/v1/players/{steam_id}/kill')
            verified = await self.bot.rcon.players(server_id)
            if not any(str(p.get('steamId')) == steam_id and valid_faction(p.get('faction')) == target for p in verified):
                await self._finish_request(row['id'], 'failed', 'Fraktionswechsel konnte nicht bestätigt werden')
                return
            async with database.transaction() as cur:
                if not await self._debit(cur, steam_id, config.REWARD_FACTION_COST, 'reward_faction', row['id'],
                                         f'Fraktionswechsel zu {target} auf {server_id}'):
                    # A successful RCON command is never retried. Keep the auditable failure instead of charging blindly.
                    await cur.execute("UPDATE reward_requests SET status='failed',reason=%s,completed_at=UTC_TIMESTAMP() WHERE id=%s",
                                      ('Punktestand hat sich während der Prüfung geändert', row['id']))
                    return
                await cur.execute("UPDATE reward_requests SET status='success',completed_at=UTC_TIMESTAMP() WHERE id=%s", (row['id'],))
            self.bot.dispatch('bot_log', '🏴 Fraktionswechsel eingelöst', f'`{steam_id}` wechselte auf {server_id} zu **{target}**.', discord.Color.blue())
        except RconError as exc:
            await self._finish_request(row['id'], 'failed', exc.safe_message)
            self.bot.dispatch('bot_log', '⚠️ Fraktionswechsel fehlgeschlagen', f'`{steam_id}` auf {server_id}: {exc.safe_message}', discord.Color.orange())

    async def _vip_request(self, row):
        duration = row['duration_kind']
        cost = config.VIP_WEEK_COST if duration == 'week' else config.VIP_MONTH_COST if duration == 'month' else None
        if cost is None:
            await self._finish_request(row['id'], 'rejected', 'Ungültige VIP-Dauer')
            return
        membership_id = None
        async with database.transaction() as cur:
            if not await self._slots_available(cur, row['server_id']):
                await cur.execute("UPDATE reward_requests SET status='rejected',reason=%s,completed_at=UTC_TIMESTAMP() WHERE id=%s",
                                  ('Alle VIP-Plätze auf diesem Server sind belegt', row['id']))
                return
            await cur.execute("SELECT id FROM vip_memberships WHERE steam_id=%s AND server_id=%s AND status IN (%s,%s,%s) FOR UPDATE",
                              (row['steam_id'], row['server_id'], *VIP_OCCUPYING))
            if await cur.fetchone():
                await cur.execute("UPDATE reward_requests SET status='rejected',reason=%s,completed_at=UTC_TIMESTAMP() WHERE id=%s",
                                  ('Für diesen Server besteht bereits ein VIP-Auftrag', row['id']))
                return
            if not await self._debit(cur, row['steam_id'], cost, 'vip_purchase', row['id'], f'VIP {duration} auf {row["server_id"]}'):
                await cur.execute("UPDATE reward_requests SET status='rejected',reason=%s,completed_at=UTC_TIMESTAMP() WHERE id=%s",
                                  ('Nicht genügend Quest-Punkte', row['id']))
                return
            await cur.execute("""INSERT INTO vip_memberships(steam_id,server_id,duration_kind,points_cost,status)
                VALUES (%s,%s,%s,%s,'pending_activation')""", (row['steam_id'], row['server_id'], duration, cost))
            membership_id = cur.lastrowid
            await cur.execute("UPDATE reward_requests SET status='success',completed_at=UTC_TIMESTAMP() WHERE id=%s", (row['id'],))
        self.bot.dispatch('vip_order', membership_id)
        self.bot.dispatch('bot_log', '⭐ VIP eingelöst', f'`{row["steam_id"]}` löste VIP für {row["server_id"]} ein.', discord.Color.blue())

    @tasks.loop(seconds=1)
    async def process_requests(self):
        try:
            async with database.transaction() as cur:
                await cur.execute("SELECT id FROM reward_requests WHERE status='pending' AND created_at>=DATE_SUB(UTC_TIMESTAMP(),INTERVAL 10 SECOND) ORDER BY created_at LIMIT 10")
                ids = [r['id'] for r in await cur.fetchall()]
                await cur.execute("UPDATE reward_requests SET status='rejected',reason='Prüfung abgelaufen',completed_at=UTC_TIMESTAMP() WHERE status='pending' AND created_at<DATE_SUB(UTC_TIMESTAMP(),INTERVAL 10 SECOND)")
            for request_id in ids:
                row = await self._claim_request(request_id)
                if row:
                    await (self._faction_request(row) if row['kind'] == 'faction' else self._vip_request(row))
            self.bot.health.ok(self.health_key)
        except Exception as exc:
            self.bot.health.error(self.health_key, exc)

    @process_requests.before_loop
    async def before_process_requests(self):
        await self.bot.wait_until_ready()

    async def membership(self, membership_id=None, message_id=None, expiry_message_id=None):
        key, value = ('id', membership_id) if membership_id is not None else ('discord_message_id', message_id) if message_id is not None else ('expiry_message_id', expiry_message_id)
        async with database.transaction() as cur:
            await cur.execute(f'''SELECT v.*,(SELECT name FROM leaderboard l WHERE l.steam_id=v.steam_id
                ORDER BY l.last_seen DESC LIMIT 1) AS player_name FROM vip_memberships v WHERE v.{key}=%s''', (value,))
            return await cur.fetchone()

    def vip_embed(self, row):
        title = '⭐ VIP-Einlösung' if row['status'] == 'pending_activation' else '⭐ VIP aktiv'
        embed = discord.Embed(title=title, color=discord.Color.gold())
        name = row.get('player_name') or 'Unbekannt'
        embed.description = f"**Spieler:** {name}\n**Steam64-ID:** `{row['steam_id']}`\n**Server:** {config.server(row['server_id']).title}\n**Dauer:** {'1 Woche' if row['duration_kind'] == 'week' else '1 Monat'}"
        if row['points_cost']:
            embed.add_field(name='Punkte', value=str(row['points_cost']))
        if row['expires_at']:
            embed.add_field(name='Ablauf', value=discord.utils.format_dt(row['expires_at'].replace(tzinfo=UTC), 'R'), inline=False)
        embed.set_footer(text=f'VIP-Auftrag {row["id"]}')
        return embed

    async def delivered(self, membership_id, message_id, expiry=False):
        async with database.transaction() as cur:
            column = 'expiry_message_id' if expiry else 'discord_message_id'
            await cur.execute(f'UPDATE vip_memberships SET {column}=%s WHERE id=%s', (message_id, membership_id))

    async def activate_from_message(self, message_id, admin):
        async with database.transaction() as cur:
            await cur.execute('SELECT * FROM vip_memberships WHERE discord_message_id=%s FOR UPDATE', (message_id,))
            row = await cur.fetchone()
            if not row or row['status'] != 'pending_activation':
                return 'Dieser VIP-Auftrag ist nicht mehr aktivierbar.'
            now = datetime.now(UTC).replace(tzinfo=None)
            expiry = now + timedelta(days=7) if row['duration_kind'] == 'week' else add_month(now)
            await cur.execute("UPDATE vip_memberships SET status='active',activated_at=%s,expires_at=%s,admin_user_id=%s,admin_mention=%s WHERE id=%s",
                              (now, expiry, str(admin.id), admin.mention[:100], row['id']))
        self.bot.dispatch('vip_update', row['id'])
        self.bot.dispatch('bot_log', '⭐ VIP aktiviert', f'{admin.mention} aktivierte VIP für `{row["steam_id"]}` auf {row["server_id"]}.', discord.Color.blue())
        return 'VIP ist aktiv und die Laufzeit wurde gestartet.'

    async def cancel_from_message(self, message_id, admin):
        async with database.transaction() as cur:
            await cur.execute('SELECT * FROM vip_memberships WHERE discord_message_id=%s FOR UPDATE', (message_id,))
            row = await cur.fetchone()
            if not row or row['status'] != 'pending_activation':
                return 'Dieser VIP-Auftrag ist nicht mehr stornierbar.'
            await self._refund(cur, row, admin)
            await cur.execute("UPDATE vip_memberships SET status='cancelled',removed_at=UTC_TIMESTAMP(),admin_user_id=%s,admin_mention=%s WHERE id=%s",
                              (str(admin.id), admin.mention[:100], row['id']))
        self.bot.dispatch('vip_update', row['id'])
        self.bot.dispatch('bot_log', '⭐ VIP storniert', f'{admin.mention} stornierte VIP für `{row["steam_id"]}` und erstattete Punkte.', discord.Color.blue())
        return 'VIP-Auftrag wurde storniert und die Punkte wurden erstattet.'

    async def remove_from_message(self, message_id, admin):
        async with database.transaction() as cur:
            await cur.execute('SELECT * FROM vip_memberships WHERE expiry_message_id=%s FOR UPDATE', (message_id,))
            row = await cur.fetchone()
            if not row or row['status'] != 'expired_pending_removal':
                return 'Dieser VIP-Slot ist bereits freigegeben.'
            await cur.execute("UPDATE vip_memberships SET status='removed',removed_at=UTC_TIMESTAMP(),admin_user_id=%s,admin_mention=%s WHERE id=%s",
                              (str(admin.id), admin.mention[:100], row['id']))
        self.bot.dispatch('vip_expiry_update', row['id'])
        self.bot.dispatch('bot_log', '⭐ VIP entfernt', f'{admin.mention} bestätigte die VIP-Entfernung für `{row["steam_id"]}`.', discord.Color.blue())
        return 'Entfernung dokumentiert; der VIP-Slot ist wieder frei.'

    @tasks.loop(seconds=config.VIP_EXPIRY_CHECK_SECONDS)
    async def expire_vips(self):
        try:
            async with database.transaction() as cur:
                await cur.execute("SELECT id FROM vip_memberships WHERE status='active' AND expires_at<=UTC_TIMESTAMP() FOR UPDATE")
                ids = [r['id'] for r in await cur.fetchall()]
                if ids:
                    await cur.execute("UPDATE vip_memberships SET status='expired_pending_removal' WHERE status='active' AND expires_at<=UTC_TIMESTAMP()")
            for membership_id in ids:
                self.bot.dispatch('vip_expired', membership_id)
                self.bot.dispatch('bot_log', '⭐ VIP abgelaufen', f'VIP-Auftrag `{membership_id}` muss extern entfernt werden.', discord.Color.orange())
        except Exception as exc:
            self.bot.health.error(self.health_key, exc)

    @expire_vips.before_loop
    async def before_expire_vips(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name='addvip', description='Dokumentiert extern vergebenes VIP.')
    @app_commands.choices(server=[app_commands.Choice(name='Server 1', value='server1'), app_commands.Choice(name='Server 2', value='server2'), app_commands.Choice(name='Server 3', value='server3')],
                          zeitraum=[app_commands.Choice(name='1 Woche', value='week'), app_commands.Choice(name='1 Monat', value='month')])
    async def addvip(self, interaction, steamid: str, server: app_commands.Choice[str], zeitraum: app_commands.Choice[str]):
        if not await require_admin(interaction):
            return
        if not valid_steam_id(steamid):
            await interaction.response.send_message('Ungültige Steam64-ID.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        created = None
        extended = False
        async with database.transaction() as cur:
            await cur.execute("SELECT * FROM vip_memberships WHERE steam_id=%s AND server_id=%s AND status='active' FOR UPDATE", (steamid, server.value))
            existing = await cur.fetchone()
            now = datetime.now(UTC).replace(tzinfo=None)
            base = max(now, existing['expires_at']) if existing and existing['expires_at'] else now
            expiry = base + timedelta(days=7) if zeitraum.value == 'week' else add_month(base)
            if existing:
                await cur.execute("UPDATE vip_memberships SET expires_at=%s,admin_user_id=%s,admin_mention=%s WHERE id=%s", (expiry, str(interaction.user.id), interaction.user.mention[:100], existing['id']))
                created = existing['id']
                extended = True
            else:
                await cur.execute("SELECT id FROM vip_memberships WHERE steam_id=%s AND server_id=%s AND status IN (%s,%s,%s) FOR UPDATE", (steamid, server.value, *VIP_OCCUPYING))
                if await cur.fetchone():
                    await interaction.followup.send('Für diesen Server besteht bereits ein reservierter VIP-Auftrag.', ephemeral=True)
                    return
                if not await self._slots_available(cur, server.value):
                    await interaction.followup.send('Auf diesem Server sind bereits alle 20 VIP-Plätze belegt.', ephemeral=True)
                    return
                await cur.execute("""INSERT INTO vip_memberships(steam_id,server_id,duration_kind,status,source,activated_at,expires_at,admin_user_id,admin_mention)
                    VALUES (%s,%s,%s,'active','admin',%s,%s,%s,%s)""", (steamid, server.value, zeitraum.value, now, expiry, str(interaction.user.id), interaction.user.mention[:100]))
                created = cur.lastrowid
        self.bot.dispatch('vip_update' if extended else 'vip_order', created)
        self.bot.dispatch('bot_log', '⭐ VIP administrativ gesetzt', f'{interaction.user.mention} setzte VIP für `{steamid}` auf {server.value}.', discord.Color.blue())
        await interaction.followup.send('VIP wurde gespeichert und im VIP-Channel dokumentiert.', ephemeral=True)


async def setup(bot):
    await bot.add_cog(RewardShopCog(bot))
