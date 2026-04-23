"""Integration tests for category-aware loan policy resolution.

Covers the 4-case precedence (most specific first):
  1. (media, category)        ← most specific
  2. (media, any)              ← media wins tiebreaker over (any, category)
  3. (any, category)
  4. (any, any) — the default
"""

from __future__ import annotations

from compendium.domain.models import LoanPolicy, MediaType, PatronCategory
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository


def _book_id(session) -> int:
    return session.query(MediaType).filter_by(code="book").one().id


def _dvd_id(session) -> int:
    return session.query(MediaType).filter_by(code="dvd").one().id


def _adult_id(session) -> int:
    return session.query(PatronCategory).filter_by(code="adult").one().id


def _child_id(session) -> int:
    return session.query(PatronCategory).filter_by(code="child").one().id


def _add_policy(
    session, *, name, days, media_type_id=None, patron_category_id=None, is_default=False
):
    p = LoanPolicy(
        name=name,
        loan_period_days=days,
        max_renewals=2,
        media_type_id=media_type_id,
        patron_category_id=patron_category_id,
        is_default=is_default,
    )
    session.add(p)
    session.flush()
    return p


class TestResolutionPrecedence:
    def test_specific_media_specific_category_wins(self, session):
        # Default (already seeded with 14d) + Adult-books (21d) + Child-books (28d) +
        # Book-only (any patron, 30d) + Adult-only (any media, 60d).
        _add_policy(session, name="Adult Books", days=21,
                    media_type_id=_book_id(session), patron_category_id=_adult_id(session))
        _add_policy(session, name="Books any patron", days=30, media_type_id=_book_id(session))
        _add_policy(session, name="Adult any media", days=60, patron_category_id=_adult_id(session))

        repo = SqlLoanPolicyRepository(session)
        p = repo.resolve(_book_id(session), _adult_id(session))
        assert p.name == "Adult Books"  # most-specific match
        assert p.loan_period_days == 21

    def test_media_only_beats_category_only_tiebreaker(self, session):
        # Two policies, no specific overlap. Both could match (Child checking out a DVD).
        _add_policy(session, name="DVDs any patron", days=7, media_type_id=_dvd_id(session))
        _add_policy(session, name="Child any media", days=28,
                    patron_category_id=_child_id(session))

        repo = SqlLoanPolicyRepository(session)
        p = repo.resolve(_dvd_id(session), _child_id(session))
        # Media wins → 7-day DVD rule
        assert p.name == "DVDs any patron"
        assert p.loan_period_days == 7

    def test_category_only_used_when_no_media_match(self, session):
        _add_policy(session, name="Child any media", days=28,
                    patron_category_id=_child_id(session))

        repo = SqlLoanPolicyRepository(session)
        # DVD has no media-specific rule → category rule applies
        p = repo.resolve(_dvd_id(session), _child_id(session))
        assert p.name == "Child any media"
        assert p.loan_period_days == 28

    def test_falls_back_to_default(self, session):
        repo = SqlLoanPolicyRepository(session)
        p = repo.resolve(_book_id(session), _adult_id(session))
        # Only the seeded default (14d) exists.
        assert p is not None
        assert p.is_default is True
        assert p.loan_period_days == 14

    def test_null_category_uses_media_then_default(self, session):
        _add_policy(session, name="Book general", days=21, media_type_id=_book_id(session))

        repo = SqlLoanPolicyRepository(session)
        p = repo.resolve(_book_id(session), None)
        assert p.name == "Book general"

    def test_null_media_uses_category_then_default(self, session):
        _add_policy(session, name="Adult general", days=60, patron_category_id=_adult_id(session))

        repo = SqlLoanPolicyRepository(session)
        p = repo.resolve(None, _adult_id(session))
        assert p.name == "Adult general"

    def test_get_for_media_type_only_matches_null_category(self, session):
        # Old back-compat helper: should NOT return a (media, category) policy.
        _add_policy(session, name="Adult Books", days=21,
                    media_type_id=_book_id(session), patron_category_id=_adult_id(session))
        repo = SqlLoanPolicyRepository(session)
        assert repo.get_for_media_type(_book_id(session)) is None
        # Add a true media-only policy and verify it returns that one.
        _add_policy(session, name="Book general", days=14, media_type_id=_book_id(session))
        assert repo.get_for_media_type(_book_id(session)).name == "Book general"
