"""Session handling with an explicit choice for persistent Steam sign-in."""
from __future__ import annotations

import json
from base64 import b64decode, b64encode

from itsdangerous.exc import BadSignature
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import Session, SessionMiddleware
from starlette.requests import HTTPConnection


class ConsentSessionMiddleware(SessionMiddleware):
    """Keep sessions browser-only unless the visitor opted in to persistence.

    Starlette's stock middleware uses one cookie lifetime for every session.
    Steam OpenID needs a session while the visitor is redirected to Steam, but
    that does not require a cookie to survive a browser restart.  The
    ``remember_login`` marker is set only after the visitor explicitly accepts
    persistent sign-in in the cookie notice.
    """

    persistent_marker = 'remember_login'

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):  # pragma: no cover
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        initial_session_was_empty = True
        if self.session_cookie in connection.cookies:
            data = connection.cookies[self.session_cookie].encode('utf-8')
            try:
                # ``self.max_age`` still provides an upper safety bound for
                # signed values, including browser-session cookies.
                data = self.signer.unsign(data, max_age=self.max_age)
                scope['session'] = Session(json.loads(b64decode(data)))
                initial_session_was_empty = False
            except (BadSignature, ValueError, TypeError, json.JSONDecodeError):
                scope['session'] = Session()
        else:
            scope['session'] = Session()

        async def send_wrapper(message):
            if message['type'] == 'http.response.start':
                session = scope['session']
                headers = MutableHeaders(scope=message)
                if session.accessed:
                    headers.add_vary_header('Cookie')
                if session.modified and session:
                    data = b64encode(json.dumps(session).encode('utf-8'))
                    data = self.signer.sign(data)
                    # No Max-Age makes this a browser-session cookie.  It is
                    # the default until the visitor opted in.
                    max_age = self.max_age if session.get(self.persistent_marker) is True else None
                    header_value = (
                        f'{self.session_cookie}={data.decode("utf-8")}; path={self.path}; '
                        f'{"Max-Age=" + str(max_age) + "; " if max_age else ""}{self.security_flags}'
                    )
                    headers.append('Set-Cookie', header_value)
                elif session.modified and not initial_session_was_empty:
                    header_value = (
                        f'{self.session_cookie}=null; path={self.path}; '
                        f'expires=Thu, 01 Jan 1970 00:00:00 GMT; {self.security_flags}'
                    )
                    headers.append('Set-Cookie', header_value)
            await send(message)

        await self.app(scope, receive, send_wrapper)
