"""FastAPI application factory. Run from the repository root with --factory."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import secrets
from typing import Literal
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx
from jinja2 import pass_context
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .repository import DataUnavailable, ReadDatabase, Repository, parse_time, server_status
from .settings import ROOT, SERVERS, Settings
from .steam import LoginError, SteamLogin
from .i18n import LANGUAGES, translate

logger = logging.getLogger('kartell.web')
PERIODS = {'de': {'7d': '7 Tage', '30d': '30 Tage', 'all': 'Gesamt'},
           'en': {'7d': '7 days', '30d': '30 days', 'all': 'All time'}}


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


def create_app(settings=None, repository=None, steam_client=None):
    settings = settings or Settings.from_env()
    db = ReadDatabase(settings.db_url)
    repo = repository or Repository(db)
    # API keys and OpenID signatures must not appear in request logs.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    client = steam_client or httpx.AsyncClient(timeout=10, follow_redirects=False)
    steam = SteamLogin(settings, client)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await db.close()
        if steam_client is None:
            await client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.repo, app.state.steam = repo, steam
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       session_cookie='kartell_session', max_age=43200,
                       same_site='lax', https_only=settings.secure)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(settings.base_url).hostname,
                                                           'localhost', '127.0.0.1'])
    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    templates = Jinja2Templates(directory=ROOT / 'templates')
    templates.env.filters.update(number=number, duration=duration, when=when, date=date, iso=iso)

    @app.get('/tokens.css', include_in_schema=False)
    async def tokens():
        return FileResponse(ROOT / 'tokens.css', media_type='text/css')

    @app.middleware('http')
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # Strip paths/claims while retaining same-origin POST Origin headers.
        # no-referrer makes Chromium submit forms with Origin: null.
        response.headers['Referrer-Policy'] = 'strict-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
            "img-src 'self' https://avatars.steamstatic.com https://avatars.akamai.steamstatic.com "
            "https://steamcdn-a.akamaihd.net https://cdn.akamai.steamstatic.com; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if settings.secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        # All pages include per-session UI; public *data* caching lives in Repository.
        response.headers['Cache-Control'] = 'private, no-store' if not request.url.path.startswith('/static/') else 'public, max-age=3600'
        return response

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
                     't': lambda key, **values: translate(language, key, **values), **context})

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
        target = form.get('next', ['/'])[0]
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith('/'):
            target = '/'
        return RedirectResponse(target, status_code=303)

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

    @app.get('/auth/steam')
    async def login(request: Request):
        if request.session.get('user'):
            return RedirectResponse('/me', status_code=303)
        try:
            state, url = await steam.begin()
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
        request.session.clear()
        request.session.update(user={'steam_id': steam_id, **summary}, csrf=secrets.token_urlsafe(32))
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

    @app.post('/logout')
    async def logout(request: Request):
        if request.headers.get('origin') not in (None, settings.base_url):
            raise HTTPException(403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(400)
        supplied = parse_qs(body.decode('utf-8', errors='replace')).get('csrf', [''])[0]
        expected = request.session.get('csrf', '')
        if not expected or not secrets.compare_digest(supplied, expected):
            raise HTTPException(403)
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
