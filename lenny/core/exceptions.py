
INVALID_ITEM = {"error": "invalid_item", "reasons": ["Invalid item selected"]}

class LennyAPIError(Exception): pass

class LoanNotRequiredError(Exception): pass

class ItemExistsError(LennyAPIError): pass

class ItemNotFoundError(LennyAPIError): pass

class InvalidFileError(LennyAPIError): pass

class DatabaseInsertError(LennyAPIError): pass

class FileTooLargeError(LennyAPIError): pass

class S3UploadError(LennyAPIError): pass

class UploaderNotAllowedError(LennyAPIError): pass

class RateLimitError(LennyAPIError): pass

class OTPGenerationError(LennyAPIError): pass

class EmailNotFoundError(LennyAPIError): pass

class ExistingLoanError(LennyAPIError): pass

class LoanNotFoundError(LennyAPIError): pass

class BookUnavailableError(LennyAPIError):
    """Raised when no copies are available for borrowing."""
    pass

class LendingNotConfiguredError(LennyAPIError):
    """Raised when lending is enabled (LENNY_LENDING_ENABLED=true) but no
    IA S3 keys are present. Operator must run `lenny ol-configure` to
    authenticate against Open Library before lending routes can serve OTPs."""
    pass

class InvalidOLCredentialsError(LennyAPIError):
    """Raised when Internet Archive rejects the email/password pair supplied
    to `ol-configure` (or equivalent). Callers should surface a user-safe
    message — no original response text."""
    pass

