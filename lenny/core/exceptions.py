
class LennyAPIError(Exception): pass

class ItemExistsError(LennyAPIError): pass

class ItemNotFoundError(LennyAPIError): pass

class InvalidFileError(LennyAPIError): pass

class DatabaseInsertError(LennyAPIError): pass

class FileTooLargeError(LennyAPIError): pass

class S3UploadError(LennyAPIError): pass

class UploaderNotAllowedError(LennyAPIError): pass

class RateLimitError(LennyAPIError): pass
