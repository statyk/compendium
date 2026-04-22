class DomainError(Exception):
    """Base class for all domain-level errors."""


class NotFoundError(DomainError):
    """A requested entity does not exist."""


class ConflictError(DomainError):
    """An operation would violate a uniqueness or state invariant."""


class ValidationError(DomainError):
    """Input failed validation."""


class BusinessRuleError(DomainError):
    """An operation would violate a business rule (e.g. checking out an item already on loan)."""


class BlockedByFinesError(BusinessRuleError):
    """A patron's outstanding fines exceed the configured checkout/hold threshold."""

    def __init__(self, patron_card: str, outstanding_cents: int, threshold_cents: int):
        self.patron_card = patron_card
        self.outstanding_cents = outstanding_cents
        self.threshold_cents = threshold_cents
        super().__init__(
            f"Patron '{patron_card}' has {outstanding_cents} cents in outstanding "
            f"fines (threshold {threshold_cents} cents)."
        )


class ExternalLookupError(DomainError):
    """An external metadata source failed or returned nothing usable."""


class AuthError(DomainError):
    """Authentication failed (bad credentials, expired token, etc.)."""
