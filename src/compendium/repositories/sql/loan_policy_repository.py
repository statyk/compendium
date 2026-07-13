from __future__ import annotations

from sqlalchemy.orm import Session

from compendium.domain.models import LoanPolicy


class SqlLoanPolicyRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, policy: LoanPolicy) -> LoanPolicy:
        self._s.add(policy)
        self._s.flush()
        return policy

    def get(self, policy_id: int) -> LoanPolicy | None:
        return self._s.get(LoanPolicy, policy_id)

    def get_for_media_type(self, media_type_id: int) -> LoanPolicy | None:
        # Back-compat: returns a media-only policy (NULL category) if present.
        return (
            self._s.query(LoanPolicy)
            .filter(
                LoanPolicy.media_type_id == media_type_id,
                LoanPolicy.patron_category_id.is_(None),
            )
            .first()
        )

    def get_default(self) -> LoanPolicy | None:
        return self._s.query(LoanPolicy).filter(LoanPolicy.is_default.is_(True)).first()

    def resolve(
        self, media_type_id: int | None, patron_category_id: int | None
    ) -> LoanPolicy | None:
        """Find the most-specific applicable policy.

        Precedence (most specific first):
          1. (media, category)
          2. (media, any)            ← media wins the tiebreaker (Koha convention)
          3. (any, category)
          4. (any, any) — the default

        Returns None only if no policy at all is configured.
        """
        if media_type_id is not None and patron_category_id is not None:
            p = (
                self._s.query(LoanPolicy)
                .filter(
                    LoanPolicy.media_type_id == media_type_id,
                    LoanPolicy.patron_category_id == patron_category_id,
                )
                .first()
            )
            if p is not None:
                return p
        if media_type_id is not None:
            p = (
                self._s.query(LoanPolicy)
                .filter(
                    LoanPolicy.media_type_id == media_type_id,
                    LoanPolicy.patron_category_id.is_(None),
                )
                .first()
            )
            if p is not None:
                return p
        if patron_category_id is not None:
            p = (
                self._s.query(LoanPolicy)
                .filter(
                    LoanPolicy.media_type_id.is_(None),
                    LoanPolicy.patron_category_id == patron_category_id,
                )
                .first()
            )
            if p is not None:
                return p
        return self.get_default()

    def list(self) -> list[LoanPolicy]:
        return self._s.query(LoanPolicy).order_by(LoanPolicy.id).all()

    def update(self, policy: LoanPolicy) -> LoanPolicy:
        self._s.flush()
        return policy

    def delete(self, policy: LoanPolicy) -> None:
        self._s.delete(policy)
        self._s.flush()

    def clear_defaults(self) -> None:
        self._s.query(LoanPolicy).filter(LoanPolicy.is_default.is_(True)).update(
            {"is_default": False}, synchronize_session="evaluate"
        )
