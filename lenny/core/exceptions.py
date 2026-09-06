from typing import Optional

INVALID_ITEM = {"error": "invalid_item", "reasons": ["Invalid item selected"]}

class LennyAPIError(Exception): pass

class LoanNotRequiredError(Exception): pass

class ItemExistsError(LennyAPIError): pass

class ItemNotFoundError(LennyAPIError): pass

class InvalidFileError(LennyAPIError): pass

class DatabaseInsertError(LennyAPIError): pass

class DatabaseDeleteError(LennyAPIError): pass

class FileTooLargeError(LennyAPIError): pass

class S3UploadError(LennyAPIError): pass

class UploaderNotAllowedError(LennyAPIError): pass

class RateLimitError(LennyAPIError): pass

class OTPGenerationError(LennyAPIError):
    """Open Library refused an OTP issue/redeem request.

    `code` carries Open Library's own error string (e.g. `unauthorized`,
    `ratelimit`, `missing_keys`), or `timeout`/`transport` when we never got a
    usable answer. Those endpoints reply HTTP 200 with a JSON error body, so this
    code is the only thing distinguishing "the patron typed it wrong" from "this
    library's credentials expired" — keep it on the exception rather than
    flattening every failure into one message.
    """

    def __init__(self, message: str = "", code: Optional[str] = None):
        super().__init__(message)
        self.code = code

class EmailNotFoundError(LennyAPIError): pass

class ExistingLoanError(LennyAPIError): pass

class LoanNotFoundError(LennyAPIError): pass

class BookUnavailableError(LennyAPIError):
    """Raised when no copies are available for borrowing."""
    pass

class PatronLoanLimitError(LennyAPIError):
    """Raised when patron has reached their concurrent loan limit."""
    pass

class LendingNotConfiguredError(LennyAPIError):
    """Raised when OL lending is the active mode (LENNY_LENDING_MODE=ol) but
    IA S3 keys are absent, or when the active mode does not match the
    requested lending operation. Operator must run `make ol-login` first."""
    pass

class InvalidOLCredentialsError(LennyAPIError):
    """Raised when Internet Archive rejects the email/password pair supplied
    to `make ol-login` (or equivalent). Callers should surface a user-safe
    message — no original response text."""
    pass

