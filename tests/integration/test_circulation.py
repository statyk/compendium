from unittest.mock import patch

import pytest

from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService

_OPEN_LIB_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}
_ISBN = "9780441013593"


def _catalog(session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
    )


def _circulation(session) -> CirculationService:
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
    )


@pytest.fixture
def item(session):
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        _, item = _catalog(session).add_from_isbn(_ISBN)
    session.flush()
    return item


@pytest.fixture
def patron(session):
    p = Patron(library_card_number="TEST0001", full_name="Test Patron")
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


def test_checkout_marks_item_checked_out(session, item, patron):
    loan = _circulation(session).checkout(item.barcode, patron.library_card_number)

    assert loan.item_id == item.id
    assert loan.patron_id == patron.id
    assert loan.returned_at is None
    assert item.status == "checked_out"


def test_checkout_sets_due_date(session, item, patron):
    loan = _circulation(session).checkout(item.barcode, patron.library_card_number)
    delta = loan.due_at - loan.checked_out_at
    assert delta.days == 14


def test_checkout_unavailable_item_raises(session, item, patron):
    _circulation(session).checkout(item.barcode, patron.library_card_number)
    with pytest.raises(BusinessRuleError, match="not available"):
        _circulation(session).checkout(item.barcode, patron.library_card_number)


def test_checkin_clears_loan(session, item, patron):
    _circulation(session).checkout(item.barcode, patron.library_card_number)
    loan = _circulation(session).checkin(item.barcode)

    assert loan.returned_at is not None
    assert item.status == "available"


def test_checkin_without_loan_raises(session, item):
    with pytest.raises(BusinessRuleError, match="no active loan"):
        _circulation(session).checkin(item.barcode)


def test_checkout_unknown_barcode_raises(session, patron):
    with pytest.raises(NotFoundError):
        _circulation(session).checkout("NOTREAL", patron.library_card_number)


def test_checkout_unknown_patron_raises(session, item):
    with pytest.raises(NotFoundError):
        _circulation(session).checkout(item.barcode, "NOTREAL")


def test_checkout_non_loanable_item_raises(session, item, patron):
    item.is_loanable = False
    item.loan_restriction_reason = "reference"
    session.flush()
    with pytest.raises(BusinessRuleError, match="not loanable"):
        _circulation(session).checkout(item.barcode, patron.library_card_number)


class TestDefaultLoanPeriodFromSiteSetting:
    """default_loan_period_days migrated from env-only Settings to the
    DB-editable registry. The setting only fires when *no* loan policy
    matches — which means we must delete the seeded default policy for it
    to take effect at all. Tests verify the env→db→default lookup chain
    actually drives the loan period in that fallback window.

    Uses a function-scoped engine so DB-row writes (set_site_setting
    requires session.commit() to be visible to the cache reader) don't
    leak into other tests in the module.
    """

    @pytest.fixture
    def fresh_engine(self):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        from compendium.domain.models import Base
        from tests.helpers import setup_sqlite_fts

        eng = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(eng)
        setup_sqlite_fts(eng)
        return eng

    @pytest.fixture
    def fresh_session(self, fresh_engine):
        from sqlalchemy.orm import sessionmaker

        from compendium.config.seed import seed_defaults
        from compendium.domain.models import LoanPolicy

        factory = sessionmaker(
            bind=fresh_engine, autoflush=False, expire_on_commit=False
        )
        s = factory()
        seed_defaults(s)
        # Delete the seeded default policy so _get_policy falls through to
        # the site_setting / descriptor default.
        s.query(LoanPolicy).delete()
        s.commit()
        yield s
        s.rollback()
        s.close()

    @pytest.fixture
    def fresh_item(self, fresh_session):
        with patch(
            "compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE
        ):
            _, item = _catalog(fresh_session).add_from_isbn(_ISBN)
        # Commit so this row survives any cross-session activity (the
        # site_settings cache opens its own Session against the same
        # StaticPool-shared connection).
        fresh_session.commit()
        return item

    @pytest.fixture
    def fresh_patron(self, fresh_session):
        p = Patron(library_card_number="DLPCARD1", full_name="Default-Loan Test Patron")
        SqlPatronRepository(fresh_session).add(p)
        fresh_session.commit()
        return p

    @pytest.fixture(autouse=True)
    def _site_settings_isolation(self, fresh_engine, monkeypatch):
        from compendium.services import site_settings as ss

        monkeypatch.setattr("compendium.db.engine.get_engine", lambda: fresh_engine)
        monkeypatch.delenv("COMPENDIUM_DEFAULT_LOAN_PERIOD_DAYS", raising=False)
        ss.invalidate_cache()
        yield
        ss.invalidate_cache()

    def test_descriptor_default_when_no_policy_no_override(
        self, fresh_session, fresh_item, fresh_patron
    ):
        loan = _circulation(fresh_session).checkout(
            fresh_item.barcode, fresh_patron.library_card_number
        )
        assert (loan.due_at - loan.checked_out_at).days == 14

    def test_env_override_changes_due_date(
        self, fresh_session, fresh_item, fresh_patron, monkeypatch
    ):
        from compendium.services import site_settings as ss

        monkeypatch.setenv("COMPENDIUM_DEFAULT_LOAN_PERIOD_DAYS", "21")
        ss.invalidate_cache()
        loan = _circulation(fresh_session).checkout(
            fresh_item.barcode, fresh_patron.library_card_number
        )
        assert (loan.due_at - loan.checked_out_at).days == 21

    def test_db_override_changes_due_date(
        self, fresh_session, fresh_item, fresh_patron
    ):
        from compendium.services import site_settings as ss
        from compendium.services.site_settings import set_site_setting

        set_site_setting("default_loan_period_days", 10, session=fresh_session)
        fresh_session.commit()
        ss.invalidate_cache()

        loan = _circulation(fresh_session).checkout(
            fresh_item.barcode, fresh_patron.library_card_number
        )
        assert (loan.due_at - loan.checked_out_at).days == 10

    def test_env_wins_over_db(
        self, fresh_session, fresh_item, fresh_patron, monkeypatch
    ):
        from compendium.services import site_settings as ss
        from compendium.services.site_settings import set_site_setting

        monkeypatch.setenv("COMPENDIUM_DEFAULT_LOAN_PERIOD_DAYS", "30")
        set_site_setting("default_loan_period_days", 10, session=fresh_session)
        fresh_session.commit()
        ss.invalidate_cache()

        loan = _circulation(fresh_session).checkout(
            fresh_item.barcode, fresh_patron.library_card_number
        )
        assert (loan.due_at - loan.checked_out_at).days == 30

    def test_default_policy_takes_precedence_over_setting(
        self, session, item, patron, monkeypatch
    ):
        """With the seeded default policy present (the normal case), the
        setting is dormant. Documents the descriptor's "when no policy
        matches" semantics."""
        from compendium.services import site_settings as ss

        monkeypatch.setenv("COMPENDIUM_DEFAULT_LOAN_PERIOD_DAYS", "99")
        ss.invalidate_cache()
        try:
            loan = _circulation(session).checkout(
                item.barcode, patron.library_card_number
            )
            assert (loan.due_at - loan.checked_out_at).days == 14
        finally:
            ss.invalidate_cache()
