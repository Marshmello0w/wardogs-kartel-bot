import json
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
from cogs.player_lookup import PlayerLookupCog, lookup_day, round_playtime_text


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
STEAM = '76561190000000001'


def round_row(server_id='server1', **changes):
    state = dict(round_id='current-round', observed_at=NOW.isoformat(), ended=False)
    row = dict(server_id=server_id, round_id='current-round', active_seconds=2500,
               active=1, last_seen=NOW.replace(tzinfo=None), round_state=json.dumps(state))
    row.update(changes)
    return row


class LookupPlaytimeTests(TestCase):
    def test_today_resets_at_local_midnight_in_summer_and_winter(self):
        self.assertEqual(lookup_day(datetime(2026, 9, 29, 21, 59, tzinfo=timezone.utc)), date(2026, 9, 29))
        self.assertEqual(lookup_day(datetime(2026, 9, 29, 22, 0, tzinfo=timezone.utc)), date(2026, 9, 30))
        self.assertEqual(lookup_day(datetime(2026, 12, 29, 22, 59, tzinfo=timezone.utc)), date(2026, 12, 29))
        self.assertEqual(lookup_day(datetime(2026, 12, 29, 23, 0, tzinfo=timezone.utc)), date(2026, 12, 30))
        self.assertEqual(lookup_day(datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)), date(2026, 10, 25))

    def test_round_time_is_measured_not_extrapolated(self):
        self.assertEqual(round_playtime_text(round_row(), NOW + timedelta(seconds=15)), '41 Min. 40 Sek.')
        self.assertIn('Spieler inaktiv', round_playtime_text(round_row(active=0), NOW))
        self.assertIn('Spieler inaktiv', round_playtime_text(
            round_row(last_seen=(NOW - timedelta(seconds=31)).replace(tzinfo=None)), NOW))

    def test_stale_uncertain_ended_and_missing_rounds_are_explicit(self):
        self.assertIn('keine erfasste Zeit', round_playtime_text(None, NOW))
        self.assertIn('Rundenstatus unbekannt', round_playtime_text(round_row(round_id='old-round'), NOW))
        self.assertIn('Rundenstatus unbekannt', round_playtime_text(round_row(round_state='broken'), NOW))
        self.assertIn('letzter Stand', round_playtime_text(round_row(), NOW + timedelta(seconds=31)))
        for key, value, expected in [('uncertain', True, 'letzter Stand'), ('ended', True, 'Runde beendet')]:
            state = json.loads(round_row()['round_state'])
            state[key] = value
            self.assertIn(expected, round_playtime_text(round_row(round_state=json.dumps(state)), NOW))

    def test_lookup_shows_daily_total_and_separate_server_rounds(self):
        cog = PlayerLookupCog.__new__(PlayerLookupCog)
        profile = dict(steam_id=STEAM, names=['Tester'], target=None, bans=[], jobs=[],
                       today_active_seconds=7200, servers={
                           'server1': {'round_playtime': round_row()},
                           'server2': {'round_playtime': round_row('server2', active_seconds=1800)},
                           'server3': {},
                       })
        with patch('cogs.player_lookup.round_playtime_text', side_effect=lambda row: round_playtime_text(row, NOW)):
            pages = cog.pages(profile)
        self.assertIn('Aktive Spielzeit heute (alle Server):** 2 Std. 00 Min.', pages[0].description)
        servers = [field.value for field in pages[0].fields if field.name.startswith('📊')]
        self.assertIn('Aktive Spielzeit diese Runde:** 41 Min. 40 Sek.', servers[0])
        self.assertIn('Aktive Spielzeit diese Runde:** 30 Min. 00 Sek.', servers[1])
        self.assertIn('keine erfasste Zeit', servers[2])
        self.assertTrue(all(len(field.value) <= 1024 for page in pages for field in page.fields))


class LookupPlaytimeQueryTests(IsolatedAsyncioTestCase):
    async def test_profile_queries_local_day_and_only_current_round_ids(self):
        queries = []

        class Cursor:
            sql = ''

            async def execute(self, sql, args=()):
                self.sql = sql
                queries.append((sql, args))

            async def fetchone(self):
                return {'active_seconds': 7300} if 'FROM challenge_daily_progress' in self.sql else None

            async def fetchall(self):
                if 'FROM challenge_round_progress' in self.sql:
                    return [round_row(), round_row('server2', active_seconds=4800)]
                return []

        @asynccontextmanager
        async def transaction():
            yield Cursor()

        with (patch('cogs.player_lookup.database.transaction', transaction),
              patch('cogs.player_lookup.lookup_day', return_value=date(2026, 9, 30))):
            profile = await PlayerLookupCog.__new__(PlayerLookupCog).profile(STEAM)
        self.assertEqual(profile['today_active_seconds'], 7300)
        self.assertEqual(profile['servers']['server2']['round_playtime']['active_seconds'], 4800)
        self.assertTrue(profile['exists'])
        daily_query = next(item for item in queries if 'FROM challenge_daily_progress' in item[0])
        self.assertEqual(daily_query[1], (STEAM, date(2026, 9, 30)))
        round_query = next(item for item in queries if 'FROM challenge_round_progress' in item[0])
        self.assertIn("s.namespace='round'", round_query[0])
        self.assertIn("c.round_id=JSON_UNQUOTE(JSON_EXTRACT(s.payload,'$.round_id'))", round_query[0])
        self.assertEqual(round_query[1], (STEAM,))

    async def test_unknown_player_remains_unknown(self):
        class Cursor:
            async def execute(self, *args):
                pass

            async def fetchone(self):
                return None

            async def fetchall(self):
                return []

        @asynccontextmanager
        async def transaction():
            yield Cursor()

        with patch('cogs.player_lookup.database.transaction', transaction):
            profile = await PlayerLookupCog.__new__(PlayerLookupCog).profile(STEAM)
        self.assertFalse(profile['exists'])
        self.assertEqual(profile['today_active_seconds'], 0)
