from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import AuthError
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService
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


@router.get("/login")
def login_page(request: Request, next: str = "", user=Depends(get_web_user)):
    if user is not None:
        return RedirectResponse(url="/ui/catalog", status_code=303)
    token = generate_token()
    resp = templates.TemplateResponse(
        request,
        "login.html",
        {"user": None, "next": next, "csrf_token": token, "error": None},
    )
    set_csrf_cookie(resp, token, get_settings().jwt_secret_key)
    return resp


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(),
    password: str = Form(),
    next: str = Form(default=""),
    csrf_token: str = Form(default=""),
    svc: AuthService = Depends(_auth_svc),
):
    check_csrf_form(request, csrf_token)
    token = generate_token()
    try:
        user = svc.authenticate(username, password)
        jwt_token = svc.issue_token(user)
    except AuthError as exc:
        resp = templates.TemplateResponse(
            request,
            "login.html",
            {"user": None, "next": next, "csrf_token": token, "error": str(exc)},
            status_code=401,
        )
        set_csrf_cookie(resp, token, get_settings().jwt_secret_key)
        return resp

    redirect_to = (
        next
        if next.startswith("/ui/") and not next.startswith("/ui//") and "\\" not in next
        else "/ui/catalog"
    )
    resp = RedirectResponse(url=redirect_to, status_code=303)
    set_auth_cookie(resp, jwt_token)
    set_csrf_cookie(resp, token, get_settings().jwt_secret_key)
    return resp


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(default="")):
    check_csrf_form(request, csrf_token)
    resp = RedirectResponse(url="/ui/login", status_code=303)
    clear_auth_cookie(resp)
    return resp
