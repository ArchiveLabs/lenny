class OLRateLimited(Exception):
    """Raised on OL 429 response. Caller should back off and retry."""


class OLWriteError(Exception):
    """Raised when OL record creation/update fails for a non-retryable reason."""


class InsufficientMetadata(Exception):
    """Raised when a BookMetadata record lacks the minimum fields to attempt OL lookup."""
