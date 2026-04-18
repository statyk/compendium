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


class ExternalLookupError(DomainError):
    """An external metadata source failed or returned nothing usable."""
