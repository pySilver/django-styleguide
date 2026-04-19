class ApplicationError(Exception):
    """Base for all service-layer exceptions."""


class OptimisticLockError(ApplicationError):
    """Raised when an optimistic locking condition fails.

    The target row was modified between read and write — typically by a
    concurrent re-import. Callers should discard work and retry (e.g.,
    NackMessage for JetStream redelivery).
    """
