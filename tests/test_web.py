"""Portal tests run independently of Discord and of a production database."""
import ast
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import httpx
from jinja2 import Environment, FileSystemLoader

from web.app import create_app
from web.repository import DataUnavailable, Repository, ranking_query, server_status
from web.settings import Settings
from web.steam import LoginError, OPENID_NS, OPENID_URL, SteamLogin

web_app = importlib.import_module('web.app')

STEAM_ID = '76561199711897930'
OTHER_ID = '76561198000000001'
SETTINGS = Settings(session_secret='unit-test-secret-only-never-for-production', steam_key='test-key')


def state_snapshot(age=0, uncertain=False):
    return {'observed_at': (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
            'uncertain': uncertain, 'ended': False,
            'snapshot': {'map': 'Kavkazi', 'experiences': ['Bakurani_KOTH_01'],
                         'players': {'current': 100, 'max': 120},
                         'factionScores': [{'name': 'Test faction', 'score': 42}]}}


def assertion(state, **overrides):
    values = {'state': state, 'openid.ns': OPENID_NS, 'openid.mode': 'id_res',
              'openid.op_endpoint': OPENID_URL, 'openid.claimed_id': f'https://steamcommunity.com/openid/id/{STEAM_ID}',
              'openid.identity': f'https://steamcommunity.com/openid/id/{STEAM_ID}',
              'openid.return_to': SETTINGS.callback + '?state=' + state,
              'openid.response_nonce': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') + 'test-nonce',
              'openid.assoc_handle': 'test-handle', 'openid.sig': 'test-signature',
              'openid.signed': 'op_endpoint,claimed_id,identity,return_to,response_nonce,assoc_handle'}
    values.update(overrides)
    return values


class FixtureDatabase:
    def __init__(self):
        self.calls = []
        self.points = 17

    async def query(self, sql, args=()):
        self.calls.append((sql, args))
        if sql == 'SELECT 1':
            return [{'1': 1}]
        if 'FROM durable_state' in sql:
            return [{'item_key': 'server1', 'payload': state_snapshot()},
                    {'item_key': 'server2', 'payload': state_snapshot(age=300)}]
        if 'ROW_NUMBER' in sql:
            return [{'player_rank': 1, 'name': '<script>alert(1)</script>', 'kills': 12,
                     'deaths': 3, 'cash': 500, 'kd': 4, 'playtime_seconds': 3_600,
                     'legacy_seconds': 3_600}]
        if 'FROM quest_points' in sql:
            return [{'points': self.points}] if args and args[0] == STEAM_ID else []
        if 'FROM quest_progress' in sql:
            return [{'eligible_playtime_seconds': 5_400, 'awarded_eligible_hours': 1, 'awarded_cash_blocks': 0}] if args and args[0] == STEAM_ID else []
        if 'COALESCE(SUM(lifetime_cash)' in sql:
            return [{'lifetime_cash': 250_000}] if args and args[0] == STEAM_ID else []
        if 'FROM quest_team_playtime' in sql:
            return [{'team': 'Valkyra', 'playtime_seconds': 7_200}] if args and args[0] == STEAM_ID else []
        if 'FROM quest_point_ledger' in sql:
            return [{'amount': 5, 'kind': 'weekly_team', 'created_at': datetime(2026, 9, 20, 10)},
                    {'amount': 1, 'kind': 'challenge_round', 'reference_key': 'server1:round-1:streak',
                     'created_at': datetime(2026, 9, 20, 9)}] if args and args[0] == STEAM_ID else []
        if 'FROM combat_player_stats' in sql:
            return [{'server_id': 'server1', 'kills': 10, 'deaths': 2,
                     'headshots': 4, 'longest_kill_m': 166.5}] if args[0] == STEAM_ID else []
        if 'FROM combat_weapon_stats' in sql:
            return [{'server_id': 'server1', 'weapon': 'Id.Item.Rifle', 'kills': 7}] if args[0] == STEAM_ID else []
        if 'FROM combat_events' in sql:
            return [{'server_id': 'server1', 'occurred_at': datetime(2026, 9, 24, 12),
                     'killer_steam_id': STEAM_ID, 'victim_steam_id': OTHER_ID,
                     'map_name': 'Kavkazi', 'cause': '<script>unsafe</script>',
                     'distance_m': 166.5, 'headshot': 1}] if args[0] == STEAM_ID else []
        if 'FROM combat_daily_quests' in sql or 'FROM combat_round_quests' in sql:
            return []
        if 'FROM challenge_daily_progress' in sql:
            return []
        if 'FROM challenge_round_progress' in sql:
            return []
        if 'FROM challenge_daily_round_kills' in sql:
            return [{'count': 0}]
        if 'FROM seed_server_state' in sql:
            return [{'server_id': 'server2'}]
        if 'FROM vip_memberships' in sql:
            return [{'server_id': 'server1', 'duration_kind': 'week', 'status': 'active',
                     'ordered_at': datetime(2026, 9, 20, 10), 'activated_at': datetime(2026, 9, 20, 10),
                     'expires_at': datetime(2026, 9, 27, 10), 'removed_at': None}] if args and args[0] == STEAM_ID else []
        if 'FROM reward_requests' in sql:
            return [{'id': 'safe-id', 'kind': 'faction', 'server_id': 'server1', 'faction': 'Valkyra',
                     'duration_kind': None, 'status': 'success', 'reason': None,
                     'created_at': datetime(2026, 9, 20, 10), 'completed_at': datetime(2026, 9, 20, 10)}] if args and args[0] == STEAM_ID else []
        if args != (STEAM_ID,):
            return []
        if 'FROM leaderboard' in sql:
            return [{'server_id': f'server{i}', 'name': f'Player {i}', 'lifetime_kills': i * 10,
                     'lifetime_deaths': i, 'lifetime_cash': 100 * i, 'current_match_kills': 3,
                     'current_match_deaths': 1, 'current_match_cash': 20,
                     'last_seen': datetime(2026, 9, 20, 10)} for i in range(1, 4)]
        if 'FROM player_playtime' in sql:
            return [{'server_id': f'server{i}', 'name': f'Alias {i}', 'playtime_seconds': 3600 * i,
                     'last_seen': datetime(2026, 9, 20, 9)} for i in range(1, 4)]
        if 'DISTINCT name' in sql:
            return [{'name': 'Old name'}, {'name': '<script>alert(2)</script>'}]
        if 'FROM player_daily_stats' in sql:
            return [{'server_id': f'server{i}', 'kills_7d': 5, 'deaths_7d': 1, 'cash_7d': 60,
                     'kills_30d': 10, 'deaths_30d': 2, 'cash_30d': 120} for i in range(1, 4)]
        if 'FROM player_faction_stats' in sql:
            return [{'server_id': 'server1', 'faction': 'Test faction', 'times_seen': 15}]
        if 'FROM player_ping_stats' in sql:
            return [{'server_id': 'server1', 'total_ping': 300, 'ping_samples': 5}]
        if 'FROM global_bans' in sql:
            return [{'reason': '<script>secret</script>', 'duration_str': '7d', 'status': 'expired',
                     'issued_at': datetime(2026, 8, 1), 'expires_at': datetime(2026, 8, 8)}]
        raise AssertionError(sql)


def steam_response(request):
    if request.url.host == 'steamcommunity.com':
        return httpx.Response(200, text='ns:' + OPENID_NS + '\nis_valid:true\n')
    return httpx.Response(200, json={'response': {'players': [{'steamid': STEAM_ID, 'personaname': 'Steam Player',
                                                            'avatarfull': 'https://avatars.steamstatic.com/test.jpg'}]}})


class SteamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(steam_response))
        self.login = SteamLogin(SETTINGS, self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_verified_identity_and_replay_protection(self):
        state, url = await self.login.begin()
        self.assertEqual(urlsplit(url).hostname, 'steamcommunity.com')
        args = list(assertion(state).items())
        self.assertEqual(await self.login.verify(args, state), STEAM_ID)
        with self.assertRaises(LoginError):
            await self.login.verify(args, state)

    async def test_reject_bad_provider_return_identity_signed_fields_nonce(self):
        changes = [ {'openid.op_endpoint': 'https://attacker.test'},
                    {'openid.return_to': 'https://attacker.test'}, {'openid.identity': 'invalid'},
                    {'openid.claimed_id': 'https://steamcommunity.com/openid/id/123'},
                    {'openid.signed': 'return_to'}, {'openid.ns': 'wrong'},
                    {'openid.mode': 'cancel'}, {'openid.response_nonce': '2000-01-01T00:00:00Zold'}]
        for change in changes:
            with self.subTest(change=change):
                state, _ = await self.login.begin()
                with self.assertRaises(LoginError):
                    await self.login.verify(list(assertion(state, **change).items()), state)

    async def test_reject_wrong_session_duplicate_and_expired_state(self):
        state, _ = await self.login.begin()
        args = list(assertion(state).items())
        with self.assertRaises(LoginError):
            await self.login.verify(args, 'other-browser')
        with self.assertRaises(LoginError):
            await self.login.verify(args + [('openid.claimed_id', 'other')], state)
        self.login.pending[state] = (0, 'unknown', 'unknown')
        with self.assertRaises(LoginError):
            await self.login.verify(args, state)

    async def test_reject_invalid_provider_signature(self):
        self.login.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='is_valid:false')))
        state, _ = await self.login.begin()
        try:
            with self.assertRaises(LoginError):
                await self.login.verify(list(assertion(state).items()), state)
        finally:
            await self.login.client.aclose()

    async def test_login_state_is_rate_limited_per_ip_and_per_browser(self):
        for _ in range(self.login.MAX_PENDING_PER_SESSION):
            await self.login.begin('198.51.100.5', 'browser-a')
        with self.assertRaises(LoginError):
            await self.login.begin('198.51.100.5', 'browser-a')
        # A distinct browser on the same address remains possible until the
        # address-wide allocation budget is exhausted.
        await self.login.begin('198.51.100.5', 'browser-b')

    async def test_profile_requires_matching_id_and_safe_avatar(self):
        self.assertEqual((await self.login.summary(STEAM_ID))['name'], 'Steam Player')
        self.assertEqual(await self.login.summary(OTHER_ID), {'name': '', 'avatar': ''})


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_servers_names_periods_bans_and_no_admin_data(self):
        db = FixtureDatabase()
        result = await Repository(db).profile(STEAM_ID)
        self.assertEqual(result['totals']['lifetime_kills'], 60)
        self.assertEqual(result['totals']['playtime_seconds'], 21600)
        self.assertEqual(result['servers'][0]['ping'], 60)
        self.assertEqual(result['servers'][0]['last_seen'].hour, 10)
        self.assertIn('Old name', result['names'])
        self.assertEqual(len(result['bans']), 1)
        self.assertEqual(result['servers'][2]['kills_7d'], 5)
        self.assertEqual(result['quest_points'], 17)
        self.assertEqual(result['combat']['headshots'], 4)
        self.assertEqual(result['combat']['headshot_percent'], 40)
        self.assertEqual(result['combat']['longest_kill_m'], 166.5)
        for sql, args in db.calls:
            self.assertEqual(args, (STEAM_ID, STEAM_ID) if 'FROM combat_events' in sql else (STEAM_ID,))
            self.assertNotIn('admin_mention', sql)
            self.assertNotIn('admin_jobs', sql)

    async def test_quests_show_only_player_data_and_team_progress(self):
        result = await Repository(FixtureDatabase()).quests(STEAM_ID)
        self.assertEqual(result['points'], 17)
        self.assertEqual(result['cash_remaining'], 50_000)
        self.assertEqual([team['team'] for team in result['teams']], ['Lonestar', 'Valkyra', 'Manticore'])
        self.assertEqual(result['teams'][0]['seconds'], 0)
        self.assertTrue(result['teams'][1]['two_hours_done'])
        self.assertFalse(result['teams'][1]['four_hours_done'])
        self.assertEqual(result['history'][0]['kind'], 'weekly_team')
        self.assertEqual(result['history'][1]['source_key'], 'challenge_streak')
        self.assertEqual(result['active_seeds'], [{'server_id': 'server2', 'title': 'Server 2'}])

    async def test_unknown_user_empty(self):
        result = await Repository(FixtureDatabase()).profile(OTHER_ID)
        self.assertFalse(result['has_data'])
        self.assertEqual(result['bans'], [])

    async def test_rewards_use_live_server_and_do_not_offer_unavailable_actions(self):
        db = FixtureDatabase()
        db.points = 50
        result = await Repository(db).rewards(STEAM_ID)
        self.assertEqual(result['faction']['server_id'], 'server1')
        self.assertTrue(result['faction']['targets']['Valkyra']['available'])
        self.assertTrue(result['faction']['targets']['Manticore']['available'])
        self.assertFalse(result['vip_options']['server1']['week']['available'])

    async def test_public_cache_and_status_age(self):
        db = FixtureDatabase()
        repo = Repository(db)
        result = await repo.statuses()
        self.assertTrue(result[0]['fresh'])
        self.assertFalse(result[1]['fresh'])
        self.assertEqual(result[2]['label'], 'Keine Messung')
        await repo.statuses()
        self.assertEqual(len(db.calls), 1)
        await repo.leaderboard('server1', '7d', 'kd', 1)
        await repo.leaderboard('server1', '7d', 'kd', 1)
        self.assertEqual(len(db.calls), 2)


class PureTests(unittest.TestCase):
    def test_discord_ranking_parity(self):
        # Load only the pure query function, without loading bot credentials/imports.
        source = Path(__file__).resolve().parents[1] / 'bot/domain/stats.py'
        node = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'ranking_query')
        scope = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), scope)
        for server in ('server1', 'server2', 'server3'):
            for period in ('all', '7d', '30d'):
                for sort in ('kd', 'cash', 'playtime'):
                    self.assertEqual(ranking_query(server, period, sort), scope['ranking_query'](server, period, sort))

    def test_kd_qualification_and_cash_ranking_rules(self):
        kd_sql, _ = ranking_query('server1', '7d', 'kd')
        cash_sql, _ = ranking_query('server1', '7d', 'cash')
        playtime_sql, _ = ranking_query('server1', '7d', 'playtime')
        all_time_sql, _ = ranking_query('server1', 'all', 'kd')
        self.assertIn('JOIN player_playtime', kd_sql)
        self.assertIn('totals.deaths>=5', kd_sql)
        self.assertIn('totals.qualifying_playtime_seconds>=1800', kd_sql)
        self.assertNotIn('totals.deaths>=5', cash_sql)
        self.assertNotIn('totals.deaths>=5', playtime_sql)
        self.assertIn('playtime_seconds DESC', playtime_sql)
        self.assertIn('FROM player_daily_playtime', playtime_sql)
        self.assertIn('JOIN player_playtime', all_time_sql)

    def test_stale_uncertain_missing_bad_player_counts(self):
        self.assertFalse(server_status('server1', state_snapshot(31))['fresh'])
        self.assertFalse(server_status('server1', state_snapshot(0, True))['fresh'])
        for value in (None, 'bad json', {}, {'snapshot': None}, {'observed_at': 'invalid'}):
            self.assertFalse(server_status('server1', value)['fresh'])
        for value in (True, -1, '20', None):
            state = state_snapshot()
            state['snapshot']['players']['current'] = value
            self.assertFalse(server_status('server1', state)['fresh'])

    def test_settings_https_and_secret_required(self):
        for args in ({'session_secret': ''}, {'session_secret': 'x' * 32, 'base_url': 'http://example.com'},
                     {'session_secret': 'x' * 32, 'base_url': 'https://example.com/path'}):
            with self.assertRaises(ValueError):
                Settings(**args)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.db = FixtureDatabase()
        self.repo = Repository(self.db)
        self.steam_client = httpx.AsyncClient(transport=httpx.MockTransport(steam_response))
        self.submissions = AsyncMock()
        self.app = create_app(SETTINGS, self.repo, self.steam_client, self.submissions)
        self.client = TestClient(self.app, base_url=SETTINGS.base_url)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def login(self):
        result = self.client.get('/auth/steam', follow_redirects=False)
        return_to = parse_qs(urlsplit(result.headers['location']).query)['openid.return_to'][0]
        state = parse_qs(urlsplit(return_to).query)['state'][0]
        return self.client.get('/auth/steam/callback', params=assertion(state), follow_redirects=False)

    def test_legal_pages_are_public_linked_and_translated(self):
        privacy = self.client.get('/datenschutz')
        imprint = self.client.get('/impressum')
        self.assertEqual(privacy.status_code, 200)
        self.assertEqual(imprint.status_code, 200)
        self.assertIn('Datenschutzerklärung', privacy.text)
        self.assertIn('Besucherzählung', privacy.text)
        self.assertIn('keine automatische Löschfrist', privacy.text)
        self.assertIn('Vor Veröffentlichung ergänzen', imprint.text)
        self.assertIn('href="/datenschutz"', self.client.get('/').text)
        self.assertIn('href="/impressum"', self.client.get('/').text)
        self.client.get('/language/en?next=/datenschutz', follow_redirects=False)
        english_privacy = self.client.get('/datenschutz')
        english_imprint = self.client.get('/impressum')
        self.assertIn('Privacy policy', english_privacy.text)
        self.assertIn('no automatic deletion period', english_privacy.text)
        self.assertIn('Legal notice', english_imprint.text)
        self.assertIn('Complete before publication', english_imprint.text)
        self.assertIn('data-cookie-choice="rejected">Reject</button>', english_privacy.text)

    def test_cookie_choice_controls_persistence_and_can_be_revoked(self):
        self.client.get('/')
        response = self.client.post('/cookie-preferences', data={'choice': 'accepted'},
                                    headers={'origin': SETTINGS.base_url})
        self.assertEqual(response.json(), {'accepted': True})
        self.assertIn('Max-Age=43200', response.headers['set-cookie'])
        self.assertIn('id="cookie-remember"', self.client.get('/').text)
        self.login()
        self.assertEqual(self.client.get('/me').status_code, 200)
        response = self.client.post('/cookie-preferences', data={'choice': 'rejected'},
                                    headers={'origin': SETTINGS.base_url})
        self.assertEqual(response.json(), {'accepted': False})
        session_header = next(value for value in response.headers.get_list('set-cookie')
                              if value.startswith('kartell_session='))
        self.assertNotIn('Max-Age=', session_header)
        self.assertEqual(self.client.get('/me').status_code, 200)
        # Rejection remains unrecorded: the notice returns on the next page.
        page = self.client.get('/me').text
        banner = re.search(r'<section id="cookie-banner"[^>]*>', page).group(0)
        self.assertNotIn(' hidden', banner)
        remember = re.search(r'<input id="cookie-remember"[^>]*>', page).group(0)
        self.assertNotIn(' checked', remember)

    def test_cookie_choice_rejects_invalid_or_cross_origin_requests(self):
        self.assertEqual(self.client.post('/cookie-preferences', data={'choice': 'selected'}).status_code, 400)
        self.assertEqual(self.client.post('/cookie-preferences', data={'choice': 'accepted'},
                                        headers={'origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.post('/cookie-preferences', content=b'x' * 129).status_code, 400)

    def test_anonymous_private_route_and_bad_callback(self):
        result = self.client.get('/me?steam_id=' + OTHER_ID, follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers['location'], '/auth/steam')
        self.assertEqual(self.client.get('/quests', follow_redirects=False).headers['location'], '/auth/steam')
        self.assertEqual(self.client.get('/rewards', follow_redirects=False).headers['location'], '/auth/steam')
        self.assertEqual(self.client.get('/auth/steam/callback', params=assertion('fake')).status_code, 400)

    def test_login_secure_cookie_own_profile_only_and_logout(self):
        result = self.login()
        self.assertEqual(result.status_code, 303)
        for flag in ('httponly', 'secure', 'samesite=lax'):
            self.assertIn(flag, result.headers['set-cookie'].lower())
        result = self.client.get('/me?steam_id=' + OTHER_ID)
        self.assertEqual(result.status_code, 200)
        self.assertIn(STEAM_ID, result.text)
        self.assertNotIn(OTHER_ID, result.text)
        self.assertIn('Kampfstatistiken', result.text)
        self.assertNotIn('Seit Beginn der Feed-Erfassung', result.text)
        self.assertIn('&lt;script&gt;unsafe&lt;/script&gt;', result.text)
        self.assertNotIn('<script>unsafe</script>', result.text)
        self.assertIn('&lt;script&gt;secret&lt;/script&gt;', result.text)
        self.assertNotIn('<script>secret</script>', result.text)
        quests = self.client.get('/quests')
        self.assertEqual(quests.status_code, 200)
        self.assertIn('17', quests.text)
        self.assertNotIn('White', quests.text)
        self.assertIn('Killserie', quests.text)
        self.assertIn('Rundenquests', quests.text)
        self.assertIn('Tagesquests', quests.text)
        self.assertIn('no-store', result.headers['cache-control'])
        csrf = re.search(r'name="csrf" value="([^"]+)"', result.text).group(1)
        self.assertEqual(result.headers['referrer-policy'], 'strict-origin')
        self.assertEqual(self.client.post('/logout', data={'csrf': csrf}, headers={'origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.post('/logout', data={'csrf': 'wrong'}).status_code, 403)
        result = self.client.post('/logout', data={'csrf': csrf}, headers={'origin': 'https://kartell.marshmello0w.de'}, follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(self.client.get('/me', follow_redirects=False).headers['location'], '/auth/steam')

    def test_weapon_labels_for_totals_servers_recent_events_and_both_languages(self):
        self.login()
        original_query = self.db.query

        async def query(sql, args=()):
            if 'FROM combat_weapon_stats' in sql:
                return [{'server_id': 'server1', 'weapon': 'ID.Item.WEPN_029', 'kills': 7}]
            if 'FROM combat_events' in sql:
                return [{'server_id': 'server1', 'occurred_at': datetime(2026, 9, 24, 12),
                         'killer_steam_id': STEAM_ID, 'victim_steam_id': OTHER_ID,
                         'map_name': 'Kavkazi', 'cause': 'Id.Vehicle.WeaponExtension.TNK_01.Heavy',
                         'distance_m': 166.5, 'headshot': 1}]
            return await original_query(sql, args)

        with patch.object(self.db, 'query', side_effect=query):
            for lang in ('de', 'en'):
                self.client.get('/language/' + lang, params={'next': '/me'}, follow_redirects=False)
                page = self.client.get('/me')
                with self.subTest(lang=lang):
                    self.assertIn('Kampfstatistiken' if lang == 'de' else 'Combat statistics', page.text)
                    self.assertEqual(page.status_code, 200)
                    self.assertEqual(page.text.count('Galil (7)'), 2)
                    self.assertIn('L2A6 cannon', page.text)
                    self.assertNotIn('WEPN_029', page.text)
                    self.assertNotIn('Id.Vehicle.', page.text)

    def test_rewards_use_only_session_steam_id_and_csrf(self):
        self.login()
        self.db.points = 50
        result = self.client.get('/rewards')
        self.assertEqual(result.status_code, 200)
        self.assertIn('Valkyra', result.text)
        csrf = re.search(r'name="csrf" value="([^"]+)"', result.text).group(1)
        self.assertEqual(self.client.post('/rewards/faction', data={'csrf': 'wrong', 'server': 'server3', 'faction': 'Manticore'}).status_code, 403)
        result = self.client.post('/rewards/faction', data={'csrf': csrf, 'server': 'server3', 'faction': 'Manticore'},
                                  headers={'origin': SETTINGS.base_url}, follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.submissions.create_request.assert_awaited_once_with(STEAM_ID, 'faction', 'server1', faction='Manticore')
        self.assertEqual(self.client.post('/rewards/vip', data={'csrf': csrf, 'server': 'server1', 'duration': 'forever'}).status_code, 400)

    def test_language_redirect_stays_on_this_origin(self):
        for target in ('///attacker.example', '//attacker.example', '/\\attacker.example', 'https://attacker.example'):
            response = self.client.get('/language/en', params={'next': target}, follow_redirects=False)
            self.assertEqual(response.headers['location'], '/')
        response = self.client.get('/language/en', params={'next': '/quests?from=language'}, follow_redirects=False)
        self.assertEqual(response.headers['location'], '/quests?from=language')
        self.login()
        page = self.client.get('/me')
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = self.client.post('/language', data={'csrf': csrf, 'language': 'en', 'next': '///attacker.example'},
                                    headers={'origin': SETTINGS.base_url}, follow_redirects=False)
        self.assertEqual(response.headers['location'], '/')
        self.assertIn('Combat statistics', self.client.get('/me').text)
        self.assertNotIn('Since feed tracking began', self.client.get('/me').text)

    def test_challenge_quests_are_translated_on_private_page(self):
        self.login()
        self.client.get('/language/en', params={'next': '/quests'}, follow_redirects=False)
        page = self.client.get('/quests')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Round quests', page.text)
        self.assertIn('Daily quests', page.text)
        self.assertIn('Kill streak', page.text)
        self.assertNotIn('Killserie', page.text)

    def test_reward_request_is_not_created_when_red(self):
        self.login()
        result = self.client.get('/rewards')
        csrf = re.search(r'name="csrf" value="([^"]+)"', result.text).group(1)
        result = self.client.post('/rewards/faction', data={'csrf': csrf, 'faction': 'Manticore'},
                                  headers={'origin': SETTINGS.base_url}, follow_redirects=False)
        self.assertEqual(result.status_code, 409)
        self.submissions.create_request.assert_not_awaited()

    def test_public_pages_escape_names_and_validate_filters(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        artillery = self.client.get('/artillery')
        self.assertEqual(artillery.status_code, 200)
        self.assertRegex(artillery.text, r'/static/site\.css\?v=[0-9a-f]{12}')
        self.assertRegex(artillery.text, r'/static/site\.js\?v=[0-9a-f]{12}')
        self.assertIn('https://wardogs-artillery.com/', artillery.text)
        self.assertIn('/artillery-app/de/', artillery.text)
        vendor = self.client.get('/artillery-app/de/')
        self.assertEqual(vendor.status_code, 200)
        self.assertIn('SAMEORIGIN', vendor.headers['x-frame-options'])
        self.assertNotIn('cloud.umami.is/script.js', vendor.text)
        self.assertIn('kartell-theme.css', vendor.text)
        self.assertFalse(self.client.get('/artillery-app/config/app.json').json()['collab']['enabled'])
        self.assertIn('/artillery-assets/', self.client.get('/artillery-app/maps/bakurani.json').text)
        self.assertEqual(self.client.get('/artillery-assets/https://evil.example/x').status_code, 404)
        self.assertEqual(self.client.get('/artillery-assets/maps/tiles/bakurani/../0_0.webp').status_code, 404)
        self.assertEqual(self.client.get('/artillery-assets/maps/tiles/bakurani/zoom_0/1_0.webp').status_code, 404)
        result = self.client.get('/leaderboard')
        self.assertEqual(result.status_code, 200)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', result.text)
        self.assertIn('name="sort" value="playtime"', result.text)
        self.assertIn('data-leaderboard-filters', result.text)
        self.assertIn('leaderboard', result.text)
        self.assertNotIn('übernommene Gesamtzeit', result.text)
        self.assertNotIn('mindestens 5 Tode', result.text)
        self.assertEqual(self.client.get('/leaderboard?sort=playtime').status_code, 200)
        filtered = self.client.get('/leaderboard?server=server2&period=30d&sort=cash')
        self.assertIn('<option value="server2" selected>', filtered.text)
        self.assertIn('<option value="30d" selected>', filtered.text)
        self.assertIn('name="sort" value="cash" aria-pressed="true"', filtered.text)
        for query in ('server=other', 'period=forever', 'sort=steam_id', 'page=0', 'page=10001'):
            self.assertEqual(self.client.get('/leaderboard?' + query).status_code, 400)
        self.assertEqual(self.client.get('/health').json(), {'status': 'ok'})

    def test_base_template_remains_compatible_during_deployment(self):
        # AMP can load a new template before its running Python process restarts.
        root = Path(__file__).resolve().parents[1]
        template = Environment(loader=FileSystemLoader(root / 'web' / 'templates')).get_template('base.html')
        html = template.render(language='de', t=lambda key: key, user=None,
                               languages={'de': 'Deutsch'}, next_path='/', path='/',
                               show_cookie_banner=False)
        self.assertRegex(html, r'/static/site\.css\?v=[0-9a-f]{12}')

    def test_database_failure_is_not_empty_success(self):
        self.repo.statuses = AsyncMock(side_effect=DataUnavailable('secret db error'))
        result = self.client.get('/')
        self.assertIn('gerade nicht erreichbar', result.text)
        self.assertNotIn('secret db error', result.text)
        self.repo.leaderboard = AsyncMock(side_effect=DataUnavailable())
        self.assertEqual(self.client.get('/leaderboard').status_code, 503)
        self.repo.db.query = AsyncMock(side_effect=DataUnavailable())
        self.assertEqual(self.client.get('/health').status_code, 503)

    def test_forged_cookie_does_not_authenticate(self):
        self.client.cookies.set('kartell_session', 'forged', domain='kartell.marshmello0w.de')
        self.assertEqual(self.client.get('/me', follow_redirects=False).status_code, 303)


class ArtilleryAssetTests(unittest.TestCase):
    def test_existing_cache_is_pruned_at_startup(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web_app, 'ARTILLERY_CACHE_ROOT', Path(directory)), \
             patch.object(web_app, 'ARTILLERY_CACHE_MAX_BYTES', 100), \
             patch.object(web_app, 'ARTILLERY_CACHE_MAX_FILES', 2):
            for number in range(3):
                (Path(directory) / f'old-{number}.webp').write_bytes(b'x')
            app = create_app(SETTINGS, Repository(FixtureDatabase()))
            with TestClient(app, base_url=SETTINGS.base_url):
                self.assertEqual(len(list(Path(directory).glob('*.webp'))), 2)

    def test_cache_is_bounded_and_reuses_cached_assets(self):
        requested = []

        def upstream(request):
            requested.append(request.url.path)
            return httpx.Response(200, content=b'tile')

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web_app, 'ARTILLERY_CACHE_ROOT', Path(directory)), \
             patch.object(web_app, 'ARTILLERY_CACHE_MAX_BYTES', 8), \
             patch.object(web_app, 'ARTILLERY_CACHE_MAX_FILES', 2):
            asset_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
            app = create_app(SETTINGS, Repository(FixtureDatabase()), asset_client=asset_client)
            paths = [f'/artillery-assets/maps/tiles/bakurani/zoom_1/{x}_{y}.webp'
                     for x, y in ((0, 0), (1, 0), (0, 1))]
            with TestClient(app, base_url=SETTINGS.base_url) as client:
                for path in paths:
                    self.assertEqual(client.get(path).content, b'tile')
                self.assertEqual(client.get(paths[-1]).content, b'tile')
            files = [path for path in Path(directory).rglob('*') if path.is_file()]
            self.assertLessEqual(len(files), 2)
            self.assertLessEqual(sum(path.stat().st_size for path in files), 8)
            self.assertEqual(len(requested), 3)

    def test_oversized_stream_stops_before_full_upstream_body(self):
        chunks_read = []

        class UpstreamBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                for chunk in (b'12345', b'67890', b'ignored'):
                    chunks_read.append(chunk)
                    yield chunk

        def upstream(request):
            return httpx.Response(200, stream=UpstreamBody())

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(web_app, 'ARTILLERY_CACHE_ROOT', Path(directory)), \
             patch.object(web_app, 'ARTILLERY_ASSET_MAX_BYTES', 8):
            asset_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
            app = create_app(SETTINGS, Repository(FixtureDatabase()), asset_client=asset_client)
            with TestClient(app, base_url=SETTINGS.base_url) as client:
                result = client.get('/artillery-assets/maps/tiles/bakurani/zoom_0/0_0.webp')
            self.assertEqual(result.status_code, 503)
            self.assertEqual(chunks_read, [b'12345', b'67890'])
            self.assertFalse(list(Path(directory).rglob('*.webp')))


if __name__ == '__main__':
    unittest.main()
