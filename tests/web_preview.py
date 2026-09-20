"""Loopback-only visual QA with synthetic data; never deploy this test app."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import Request
from fastapi.responses import RedirectResponse
import uvicorn

from test_web import FixtureDatabase, STEAM_ID
from web.app import create_app
from web.repository import Repository
from web.settings import Settings


def create_preview():
    app = create_app(Settings(base_url='http://127.0.0.1:8765', session_secret='visual-test-only-secret-not-for-deployment'),
                     Repository(FixtureDatabase()))

    @app.get('/__fixture_login')
    async def fixture_login(request: Request):
        request.session['user'] = {'steam_id': STEAM_ID, 'name': 'Testprofil · Vorschaudaten', 'avatar': ''}
        return RedirectResponse('/me')

    return app


if __name__ == '__main__':
    uvicorn.run(create_preview(), host='127.0.0.1', port=8765, access_log=False)
