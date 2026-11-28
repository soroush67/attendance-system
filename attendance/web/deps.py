"""Shared request helpers: templates, authentication/authorization, CSRF, audit."""
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from attendance.config import get_settings
from attendance.db import get_db
from attendance.models import ROLES, Audit, User
from attendance.security import csrf_token, csrf_valid, session_user

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / 'templates'))
templates.env.globals.update(ROLES=ROLES)

ALL_ROLES = ('admin', 'operator', 'exporter', 'viewer')
CAN_UPLOAD = ('admin', 'operator')
CAN_EXPORT = ('admin', 'operator', 'exporter')


class NotAuthenticated(Exception):
    pass


class MustChangePassword(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = session_user(request, db)
    if not user:
        raise NotAuthenticated()
    if user.must_change_password and request.url.path not in ('/account/password', '/logout'):
        raise MustChangePassword()
    request.state.user = user
    request.state.user_id = user.id
    return user


def require(*roles: str):
    """Dependency: signed-in user whose role is one of `roles` (server-side RBAC)."""
    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, 'مجوز انجام این عملیات را ندارید')
        return user
    return dependency


def check_csrf(request: Request, token: str | None) -> None:
    if not csrf_valid(request, token):
        raise HTTPException(403, 'درخواست نامعتبر است؛ صفحه را تازه‌سازی و دوباره تلاش کنید')


def audit(db: Session, actor: str, event: str, details: str = '') -> None:
    db.add(Audit(actor=actor[:80], event=event[:80], details=details[:300]))


def redirect(path: str, message: str | None = None, error: bool = False) -> RedirectResponse:
    if message:
        path += ('&' if '?' in path else '?') + ('error=' if error else 'message=') + quote(message)
    return RedirectResponse(path, status_code=303)


def render(request: Request, template: str, title: str, section: str = '', status_code: int = 200, **ctx):
    user = getattr(request.state, 'user', None)
    return templates.TemplateResponse(request, template, {
        'title': title, 'section': section, 'user': user, 'csrf': csrf_token(request),
        'message': request.query_params.get('message', '')[:300],
        'error': request.query_params.get('error', '')[:300],
        'version': get_settings().app_version,
        'can_upload': bool(user and user.role in CAN_UPLOAD),
        'can_export': bool(user and user.role in CAN_EXPORT),
        **ctx,
    }, status_code=status_code)
