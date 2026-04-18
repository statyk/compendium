from sqlalchemy.orm import Session

from compendium.domain.models import AppUser


class SqlUserRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, user: AppUser) -> AppUser:
        self._s.add(user)
        self._s.flush()
        return user

    def get(self, user_id: int) -> AppUser | None:
        return self._s.get(AppUser, user_id)

    def get_by_username(self, username: str) -> AppUser | None:
        return self._s.query(AppUser).filter_by(username=username).first()
