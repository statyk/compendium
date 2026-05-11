from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from compendium.api.schemas import LoginRequest, TokenResponse
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import AuthError
from compendium.repositories.sql.failed_login_repository import SqlFailedLoginRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService
from compendium.services.rate_limit import RateLimitService, resolve_client_ip

router = APIRouter()


def _auth_service(session: Session = Depends(get_session)) -> AuthService:
    return AuthService(
        user_repo=SqlUserRepository(session),
        role_repo=SqlRoleRepository(session),
        settings=get_settings(),
    )


def _rate_limit_svc(session: Session = Depends(get_session)) -> RateLimitService:
    return RateLimitService(SqlFailedLoginRepository(session))


@router.post("/login", response_model=TokenResponse)
def login(
    request: Request,
    body: LoginRequest,
    auth: AuthService = Depends(_auth_service),
    rl: RateLimitService = Depends(_rate_limit_svc),
) -> TokenResponse:
    client_ip = resolve_client_ip(
        request.client.host if request.client else None,
        request.headers.get("X-Forwarded-For", ""),
        get_settings().trusted_proxies,
    )

    retry_after = rl.check("login_user", body.username)
    if retry_after is None and client_ip:
        retry_after = rl.check_ip(client_ip)
    if retry_after is not None:
        return JSONResponse(
            status_code=429,
            content={"detail": f"Too many failed login attempts. Try again in {retry_after} seconds."},
            headers={"Retry-After": str(retry_after)},
        )
    try:
        user = auth.authenticate(body.username, body.password)
    except AuthError as exc:
        rl.record_failure("login_user", body.username)
        if client_ip:
            rl.record_ip_failure(client_ip)
        # Return rather than raise so the session commits and the failure row is
        # persisted — raising HTTPException would trigger a session rollback.
        return JSONResponse(status_code=401, content={"detail": str(exc)})
    rl.clear("login_user", body.username)
    if client_ip:
        rl.clear_ip(client_ip)
    return TokenResponse(access_token=auth.issue_token(user))
