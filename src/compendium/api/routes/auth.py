from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.schemas import LoginRequest, TokenResponse
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import AuthError
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService

router = APIRouter()


def _auth_service(session: Session = Depends(get_session)) -> AuthService:
    return AuthService(
        user_repo=SqlUserRepository(session),
        role_repo=SqlRoleRepository(session),
        settings=get_settings(),
    )


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, auth: AuthService = Depends(_auth_service)) -> TokenResponse:
    try:
        user = auth.authenticate(body.username, body.password)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return TokenResponse(access_token=auth.issue_token(user))
