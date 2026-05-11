from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import AuthError
from compendium.repositories.sql.failed_login_repository import SqlFailedLoginRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService
from compendium.services.rate_limit import RateLimitService
from compendium.web.csrf import check_csrf_form, generate_token, set_csrf_cookie
from compendium.web.deps import clear_auth_cookie, get_web_user, set_auth_cookie
from compendium.web.jinja import templates

router = APIRouter()


def _auth_svc(session=Depends(get_session)) -> AuthService:
    return AuthService(
        user_repo=SqlUserRepository(session),
        role_repo=SqlRoleRepository(session),
        settings=get_settings(),
    )


def _rate_limit_svc(session=Depends(get_session)) -> RateLimitService:
    return RateLimitService(SqlFailedLoginRepository(session))


@router.get("/login")
def login_page(
    request: Request,
    next: str = "",
    message: str = "",
    user=Depends(get_web_user),
):
    if user is not None:
        return RedirectResponse(url="/ui/catalog", status_code=303)
    token = generate_token()
    resp = templates.TemplateResponse(
        request,
        "login.html",
        {
            "user": None,
            "next": next,
            "csrf_token": token,
            "error": None,
            "message": message or None,
        },
    )
    set_csrf_cookie(resp, token)
    return resp


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(),
    password: str = Form(),
    next: str = Form(default=""),
    csrf_token: str = Form(default=""),
    svc: AuthService = Depends(_auth_svc),
    rl: RateLimitService = Depends(_rate_limit_svc),
):
    check_csrf_form(request, csrf_token)
    token = generate_token()

    retry_after = rl.check("login_user", username)
    if retry_after is not None:
        resp = templates.TemplateResponse(
            request,
            "login.html",
            {
                "user": None,
                "next": next,
                "csrf_token": token,
                "error": f"Too many failed login attempts. Try again in {retry_after} seconds.",
                "message": None,
            },
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
        set_csrf_cookie(resp, token)
        return resp

    try:
        user = svc.authenticate(username, password)
        jwt_token = svc.issue_token(user)
    except AuthError as exc:
        rl.record_failure("login_user", username)
        resp = templates.TemplateResponse(
            request,
            "login.html",
            {
                "user": None,
                "next": next,
                "csrf_token": token,
                "error": str(exc),
                "message": None,
            },
            status_code=401,
        )
        set_csrf_cookie(resp, token)
        return resp

    rl.clear("login_user", username)
    parsed = urlparse(next)
    redirect_to = next if (parsed.netloc == "" and next.startswith("/ui/")) else "/ui/catalog"
    resp = RedirectResponse(url=redirect_to, status_code=303)
    set_auth_cookie(resp, jwt_token)
    set_csrf_cookie(resp, token)
    return resp


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(default="")):
    check_csrf_form(request, csrf_token)
    resp = RedirectResponse(url="/ui/login", status_code=303)
    clear_auth_cookie(resp)
    return resp
