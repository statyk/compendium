from sqlalchemy.orm import Session

from compendium.domain.models import Role


class SqlRoleRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, role: Role) -> Role:
        self._s.add(role)
        self._s.flush()
        return role

    def get(self, role_id: int) -> Role | None:
        return self._s.get(Role, role_id)

    def get_by_name(self, name: str) -> Role | None:
        return self._s.query(Role).filter_by(name=name).first()

    def list(self) -> list[Role]:
        return self._s.query(Role).order_by(Role.name).all()

    def update(self, role: Role) -> Role:
        self._s.flush()
        return role
