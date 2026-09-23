"""FastAPI application factory. Run from the repository root with --factory."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import asyncio
import logging
import os
import re
import secrets
from typing import Literal
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx
from jinja2 import pass_context
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .repository import (DataUnavailable, ReadDatabase, Repository,
                         RewardSubmissionDatabase, parse_time, server_status)
from .settings import ROOT, SERVERS, Settings
from .steam import LoginError, SteamLogin
from .i18n import LANGUAGES, translate
from .session import ConsentSessionMiddleware
from .visitor_tracker import VisitorTracker

logger = logging.getLogger('kartell.web')
PERIODS = {'de': {'7d': '7 Tage', '30d': '30 Tage', 'all': 'Gesamt'},
           'en': {'7d': '7 days', '30d': '30 days', 'all': 'All time'}}
ARTILLERY_ASSET_PATH = re.compile(
    r'(?:maps/tiles(?:-color)?/(?:bakurani|ozeti|zestafona)/zoom_(?P<zoom>[0-7])/(?P<x>\d+)_(?P<y>\d+)\.webp'
    r'|data/terrain/(?:bakurani|ozeti|zestafona)/(?:manifest\.json|chunks/\d+_\d+\.bin))\Z'
)
ARTILLERY_ASSET_ORIGIN = 'https://assets.wardogs-artillery.com/releases/assets-v1/'
ARTILLERY_CACHE_ROOT = ROOT / 'data' / 'artillery_assets'


def cache_artillery_asset(path, content):
    """Keep immutable upstream release files locally after their first request."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@pass_context
def number(context, value, digits=0):
    if value is None:
        return '—'
    rendered = f'{float(value):,.{digits}f}'
    return rendered if context.get('language') == 'en' else rendered.replace(',', '\u00a0').replace('.', ',')


@pass_context
def duration(context, value):
    if value is None:
        return '—'
    hours, remaining = divmod(int(value), 3600)
    if context.get('language') == 'en':
        return f'{number(context, hours)} hr {remaining // 60:02d} min'
    return f'{number(context, hours)} Std. {remaining // 60:02d} Min.'


@pass_context
def when(context, value):
    stamp = parse_time(value)
    if not stamp:
        return '—'
    local = stamp.astimezone(ZoneInfo('Europe/Berlin'))
    return local.strftime('%b %d, %Y · %H:%M') if context.get('language') == 'en' else local.strftime('%d.%m.%Y · %H:%M')


@pass_context
def date(context, value):
    if not value:
        return '—'
    return value.strftime('%b %d, %Y') if context.get('language') == 'en' else value.strftime('%d.%m.%Y')


def iso(value):
    stamp = parse_time(value)
    return stamp.isoformat() if stamp else ''


def internal_redirect_path(value):
    """Return one normalized-origin relative target, never a network path."""
    target = value if isinstance(value, str) else ''
    parsed = urlsplit(target)
    if (not target.startswith('/') or target.startswith('//') or '\\' in target
            or parsed.scheme or parsed.netloc):
        return '/'
    return target


def create_app(settings=None, repository=None, steam_client=None, reward_submission=None):
    settings = settings or Settings.from_env()
    db = ReadDatabase(settings.db_url)
    reward_db = reward_submission or RewardSubmissionDatabase(settings.reward_db_url)
    repo = repository or Repository(db)
    visitors = VisitorTracker(ROOT / 'data' / 'unique_visitors.json', settings.session_secret)
    # API keys and OpenID signatures must not appear in request logs.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    client = steam_client or httpx.AsyncClient(timeout=10, follow_redirects=False)
    artillery_client = httpx.AsyncClient(timeout=12, follow_redirects=False)
    steam = SteamLogin(settings, client)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await db.close()
        if reward_submission is None:
            await reward_db.close()
        if steam_client is None:
            await client.aclose()
        await artillery_client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.repo, app.state.steam, app.state.reward_db, app.state.visitors = repo, steam, reward_db, visitors
    app.add_middleware(ConsentSessionMiddleware, secret_key=settings.session_secret,
                       session_cookie='kartell_session', max_age=43200,
                       same_site='lax', https_only=settings.secure)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(settings.base_url).hostname,
                                                           'localhost', '127.0.0.1'])
    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    app.mount('/artillery-app', StaticFiles(directory=ROOT / 'artillery_app', html=True), name='artillery-app')
    templates = Jinja2Templates(directory=ROOT / 'templates')
    templates.env.filters.update(number=number, duration=duration, when=when, date=date, iso=iso)

    @app.get('/tokens.css', include_in_schema=False)
    async def tokens():
        return FileResponse(ROOT / 'tokens.css', media_type='text/css')

    @app.middleware('http')
    async def security_headers(request, call_next):
        response = await call_next(request)
        # Count successful HTML/page requests, not static assets, health checks,
        # or form/API posts.  Uvicorn supplies the proxy-validated client IP.
        if (request.method == 'GET' and response.status_code < 400
                and request.url.path not in ('/health', '/tokens.css')
                and not request.url.path.startswith(('/static/', '/artillery-app/', '/artillery-assets/'))):
            try:
                await visitors.record(request.client.host if request.client else None)
            except OSError:
                logger.warning('Unique visitor counter file unavailable')
        # Remove the legacy preference cookie from earlier banner versions.
        # Consent is now never recorded separately.
        if request.cookies.get('kartell_cookie_choice'):
            response.delete_cookie('kartell_cookie_choice', path='/', secure=settings.secure,
                                   httponly=True, samesite='lax')
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # Strip paths/claims while retaining same-origin POST Origin headers.
        # no-referrer makes Chromium submit forms with Origin: null.
        response.headers['Referrer-Policy'] = 'strict-origin'
        if request.url.path.startswith('/artillery-app/'):
            response.headers['X-Frame-Options'] = 'SAMEORIGIN'
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; script-src 'self'; script-src-attr 'none'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "connect-src 'self'; font-src 'self' data:; worker-src 'self' blob:; "
                "frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'")
        else:
            response.headers['X-Frame-Options'] = 'DENY'
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
                "img-src 'self' https://avatars.steamstatic.com https://avatars.akamai.steamstatic.com "
                "https://steamcdn-a.akamaihd.net https://cdn.akamai.steamstatic.com; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if settings.secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        # All pages include per-session UI; public *data* caching lives in Repository.
        response.headers['Cache-Control'] = ('public, max-age=86400' if request.url.path.startswith('/artillery-assets/')
            else 'public, max-age=3600' if request.url.path.startswith(('/static/', '/artillery-app/'))
            else 'private, no-store')
        return response

    @app.get('/artillery-assets/{asset_path:path}', include_in_schema=False)
    async def artillery_asset(asset_path: str):
        # Fixed CDN and strict path allowlist: this route must not be a general proxy.
        match = ARTILLERY_ASSET_PATH.fullmatch(asset_path)
        if len(asset_path) > 180 or '..' in asset_path or not match:
            raise HTTPException(404)
        if match.group('zoom') and (int(match.group('x')) >= 2 ** int(match.group('zoom'))
                                    or int(match.group('y')) >= 2 ** int(match.group('zoom'))):
            raise HTTPException(404)
        media_type = ('image/webp' if asset_path.endswith('.webp') else
                      'application/json' if asset_path.endswith('.json') else 'application/octet-stream')
        cached = ARTILLERY_CACHE_ROOT / asset_path
        if cached.is_file():
            return FileResponse(cached, media_type=media_type)
        try:
            upstream = await artillery_client.get(ARTILLERY_ASSET_ORIGIN + asset_path)
        except httpx.HTTPError:
            raise HTTPException(503)
        if upstream.status_code != 200 or len(upstream.content) > 8_000_000:
            raise HTTPException(404 if upstream.status_code == 404 else 503)
        try:
            await asyncio.to_thread(cache_artillery_asset, cached, upstream.content)
        except OSError:
            # A read-only deployment still serves the requested asset.
            logger.warning('Artillery asset cache unavailable')
        return Response(upstream.content, media_type=media_type)

    def render(request, template, status=200, **context):
        user = request.session.get('user')
        language = request.session.get('language', 'de')
        if language not in LANGUAGES:
            language = 'de'
        if not request.session.get('csrf'):
            request.session['csrf'] = secrets.token_urlsafe(32)
        next_path = request.url.path
        return templates.TemplateResponse(request=request, name=template, status_code=status,
            context={'user': user, 'csrf': request.session.get('csrf', ''), 'servers': SERVERS,
                     'periods': PERIODS[language], 'path': request.url.path, 'language': language,
                     'languages': LANGUAGES, 'next_path': next_path,
                     'show_cookie_banner': request.session.get('remember_login') is not True,
                     't': lambda key, **values: translate(language, key, **values), **context})

    @app.post('/cookie-preferences')
    async def cookie_preferences(request: Request):
        """Apply a choice without retaining a separate consent record."""
        if request.headers.get('origin') not in (None, settings.base_url):
            raise HTTPException(403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 128:
                raise HTTPException(400)
        choice = parse_qs(body.decode('utf-8', errors='replace')).get('choice', [''])[0]
        if choice not in ('accepted', 'rejected'):
            raise HTTPException(400)
        if choice == 'accepted':
            # The session itself is the only persistent record, and only when
            # the visitor explicitly wants their Steam login remembered.
            request.session['remember_login'] = True
        else:
            # No preference or Steam login survives a browser restart after a
            # rejection.  The temporary session remains solely for OpenID.
            request.session.pop('remember_login', None)
        response = JSONResponse({'accepted': choice == 'accepted'})
        response.delete_cookie('kartell_cookie_choice', path='/', secure=settings.secure,
                               httponly=True, samesite='lax')
        return response

    async def protected_form(request):
        """Read a small same-origin form and validate its session CSRF token."""
        if request.headers.get('origin') not in (None, settings.base_url):
            raise HTTPException(403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(400)
        form = parse_qs(body.decode('utf-8', errors='replace'))
        supplied = form.get('csrf', [''])[0]
        expected = request.session.get('csrf', '')
        if not expected or not secrets.compare_digest(supplied, expected):
            raise HTTPException(403)
        return form

    @app.post('/language')
    async def language(request: Request):
        if request.headers.get('origin') not in (None, settings.base_url):
            raise HTTPException(403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(400)
        form = parse_qs(body.decode('utf-8', errors='replace'))
        supplied = form.get('csrf', [''])[0]
        expected = request.session.get('csrf', '')
        if expected and not secrets.compare_digest(supplied, expected):
            raise HTTPException(403)
        selected = form.get('language', ['de'])[0]
        if selected not in LANGUAGES:
            raise HTTPException(400)
        request.session['language'] = selected
        target = internal_redirect_path(form.get('next', ['/'])[0])
        return RedirectResponse(target, status_code=303)

    @app.get('/language/{selected}')
    async def select_language(request: Request, selected: Literal['de', 'en'], next: str = '/'):
        """Language preference is non-sensitive and works without form submission."""
        next = internal_redirect_path(next)
        request.session['language'] = selected
        return RedirectResponse(next, status_code=303)

    @app.get('/')
    async def home(request: Request):
        error = None
        try:
            statuses = await repo.statuses()
        except DataUnavailable:
            logger.warning('Server dashboard database unavailable')
            statuses = [server_status(key, None) for key in SERVERS]
            error = translate(request.session.get('language', 'de'), 'error_db')
        live = [s for s in statuses if s['fresh']]
        return render(request, 'home.html', statuses=statuses, error=error,
                      online_players=sum(s['players'] for s in live) if live else None,
                      live_servers=len(live), refresh=True)

    @app.get('/leaderboard')
    async def leaderboard(request: Request, server: Literal['server1', 'server2', 'server3'] = 'server1',
                          period: Literal['7d', '30d', 'all'] = '7d', sort: Literal['kd', 'cash'] = 'kd',
                          page: int = Query(1, ge=1, le=10000)):
        rows = await repo.leaderboard(server, period, sort, page)
        return render(request, 'leaderboard.html', rows=rows[:50], more=len(rows) > 50,
                      server=server, period=period, sort=sort, page=page)

    @app.get('/artillery')
    async def artillery(request: Request):
        return render(request, 'artillery.html')

    @app.get('/auth/steam')
    async def login(request: Request):
        if request.session.get('user'):
            return RedirectResponse('/me', status_code=303)
        session_key = request.session.get('login_client_key')
        if not isinstance(session_key, str) or len(session_key) < 16:
            session_key = secrets.token_urlsafe(24)
            request.session['login_client_key'] = session_key
        # A missing acceptance behaves like rejection: users can authenticate
        # with Steam, but their login ends with the browser.
        client_ip = request.client.host if request.client else 'unknown'
        try:
            state, url = await steam.begin(client_ip, session_key)
        except LoginError:
            return render(request, 'error.html', status=429, title_key='error_busy_title', message_key='error_busy')
        request.session['login_state'] = state
        return RedirectResponse(url, status_code=303)

    @app.get('/auth/steam/callback')
    async def callback(request: Request):
        state = request.session.pop('login_state', None)
        try:
            steam_id = await steam.verify(request.query_params.multi_items(), state)
        except (LoginError, httpx.HTTPError):
            logger.info('Steam login rejected or provider unavailable')
            return render(request, 'error.html', status=400, title_key='error_login_title', message_key='error_login')
        try:
            summary = await steam.summary(steam_id)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            logger.warning('Steam profile details unavailable')
            summary = {'name': '', 'avatar': ''}
        remember_login = request.session.get('remember_login') is True
        request.session.clear()
        request.session.update(user={'steam_id': steam_id, **summary}, csrf=secrets.token_urlsafe(32))
        if remember_login:
            request.session['remember_login'] = True
        return RedirectResponse('/me', status_code=303)

    @app.get('/me')
    async def profile(request: Request):
        user = request.session.get('user')
        if not user:
            return RedirectResponse('/auth/steam', status_code=303)
        # No Steam ID from URL, forms, or query parameters is ever consulted.
        data = await repo.profile(user['steam_id'])
        return render(request, 'profile.html', profile=data,
                      display_name=user.get('name') or (data['names'][0] if data['names'] else 'Dein Profil'))

    @app.get('/quests')
    async def quests(request: Request):
        user = request.session.get('user')
        if not user:
            return RedirectResponse('/auth/steam', status_code=303)
        data = await repo.quests(user['steam_id'])
        return render(request, 'quests.html', quests=data,
                      display_name=user.get('name') or 'Deine Quests')

    @app.get('/rewards')
    async def rewards(request: Request):
        user = request.session.get('user')
        if not user:
            return RedirectResponse('/auth/steam', status_code=303)
        data = await repo.rewards(user['steam_id'])
        pending = any(row['status'] in ('pending', 'processing', 'charged', 'executing') for row in data['requests'])
        return render(request, 'rewards.html', rewards=data, refresh=pending)

    @app.post('/rewards/faction')
    async def redeem_faction(request: Request):
        user = request.session.get('user')
        if not user:
            return RedirectResponse('/auth/steam', status_code=303)
        form = await protected_form(request)
        faction = form.get('faction', [''])[0]
        if faction not in ('Lonestar', 'Valkyra', 'Manticore'):
            raise HTTPException(400)
        state = await repo.rewards(user['steam_id'])
        server = state['faction']['server_id']
        option = state['faction']['targets'].get(faction, {})
        # The server is derived exclusively from the most recent bot player
        # poll.  A forged form can never select a different server.
        if not server or not option.get('available'):
            raise HTTPException(409)
        await reward_db.create_request(user['steam_id'], 'faction', server, faction=faction)
        return RedirectResponse('/rewards', status_code=303)

    @app.post('/rewards/vip')
    async def redeem_vip(request: Request):
        user = request.session.get('user')
        if not user:
            return RedirectResponse('/auth/steam', status_code=303)
        form = await protected_form(request)
        server = form.get('server', [''])[0]
        duration = form.get('duration', [''])[0]
        if server not in SERVERS or duration not in ('week', 'month'):
            raise HTTPException(400)
        state = await repo.rewards(user['steam_id'])
        option = state['vip_options'].get(server, {}).get(duration, {})
        if not option.get('available'):
            raise HTTPException(409)
        await reward_db.create_request(user['steam_id'], 'vip', server, duration_kind=duration)
        return RedirectResponse('/rewards', status_code=303)

    @app.post('/logout')
    async def logout(request: Request):
        await protected_form(request)
        request.session.clear()
        return RedirectResponse('/', status_code=303)

    @app.get('/health')
    async def health():
        try:
            await repo.db.query('SELECT 1')
            return JSONResponse({'status': 'ok'})
        except DataUnavailable:
            return JSONResponse({'status': 'unavailable'}, status_code=503)

    @app.exception_handler(DataUnavailable)
    async def database_error(request, exc):
        logger.warning('Portal database unavailable')
        return render(request, 'error.html', status=503, title_key='error_db_title', message_key='error_db')

    @app.exception_handler(RequestValidationError)
    async def invalid_filter(request, exc):
        return render(request, 'error.html', status=400, title_key='error_filter_title', message_key='error_filter')

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return render(request, 'error.html', status=exc.status_code, title_key='error_page_title', message_key='error_page')

    return app
