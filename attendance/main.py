"""Application factory: middleware, error handling, health and crawler controls."""
import json
import logging
import sys
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from attendance.config import get_settings
from attendance.db import session_factory
from attendance.web import pages
from attendance.web.deps import MustChangePassword, NotAuthenticated, render

log = logging.getLogger('attendance')

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
NOINDEX = 'noindex, nofollow, noarchive, nosnippet'


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(message)s'))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()
    # No interactive docs or public OpenAPI schema in any environment.
    app = FastAPI(title='attendance', docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def security_headers(request: Request, call_next):
        request_id = request.headers.get('x-request-id', '')[:64] or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.monotonic()
        response = await call_next(request)
        h = response.headers
        h['X-Request-ID'] = request_id
        h['Content-Security-Policy'] = CSP
        h['X-Content-Type-Options'] = 'nosniff'
        h['X-Frame-Options'] = 'DENY'
        h['Referrer-Policy'] = 'no-referrer'
        h['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=(), payment=()'
        h['Cross-Origin-Opener-Policy'] = 'same-origin'
        h['X-Robots-Tag'] = NOINDEX
        if settings.hsts:
            h['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        if request.url.path.startswith('/static/'):
            h['Cache-Control'] = 'public, max-age=3600'
        else:
            h['Cache-Control'] = 'private, no-store'
            h['Pragma'] = 'no-cache'
        user_id = getattr(request.state, 'user_id', None)
        log.info(json.dumps({
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'level': 'info', 'request_id': request_id,
            'method': request.method, 'path': request.url.path, 'status': response.status_code,
            'duration_ms': round((time.monotonic() - started) * 1000, 1),
            'ip': request.client.host if request.client else None, 'user_id': user_id,
        }))
        return response

    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie='attendance_session',
                       max_age=settings.session_max_age_minutes * 60, same_site='strict',
                       https_only=settings.session_https_only)

    @app.exception_handler(NotAuthenticated)
    async def not_authenticated(request: Request, exc: NotAuthenticated):
        if request.method == 'GET' and not request.url.path.startswith('/api/'):
            return RedirectResponse('/login', status_code=303)
        return JSONResponse({'detail': 'authentication required'}, status_code=401)

    @app.exception_handler(MustChangePassword)
    async def must_change(request: Request, exc: MustChangePassword):
        return RedirectResponse('/account/password', status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else 'درخواست قابل انجام نیست'
        return render(request, 'error.html', 'خطا', status_code=exc.status_code, code=exc.status_code, detail=detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Never echo the submitted values back.
        return render(request, 'error.html', 'خطا', status_code=422, code=422, detail='اطلاعات ارسال‌شده نامعتبر است')

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.error(json.dumps({'level': 'error', 'request_id': getattr(request.state, 'request_id', None),
                              'path': request.url.path, 'error': type(exc).__name__}))
        return PlainTextResponse('خطای داخلی سامانه رخ داد.', status_code=500)

    @app.get('/health', include_in_schema=False)
    def health():
        """Liveness: the process is up."""
        return {'status': 'ok', 'version': settings.app_version}

    @app.get('/ready', include_in_schema=False)
    def ready():
        """Readiness: the database answers and migrations are applied."""
        try:
            with session_factory()() as db:
                db.execute(text('SELECT 1 FROM login_attempts LIMIT 1'))
        except Exception:
            return JSONResponse({'status': 'unavailable'}, status_code=503)
        return {'status': 'ready', 'version': settings.app_version}

    @app.get('/robots.txt', include_in_schema=False)
    def robots():
        return PlainTextResponse('User-agent: *\nDisallow: /\n')

    app.mount('/static', StaticFiles(directory=str(Path(__file__).resolve().parent / 'static')), name='static')
    app.include_router(pages.router)
    return app


app = create_app()
