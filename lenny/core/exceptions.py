
class LennyAPIError(Exception): pass

class ItemExistsError(LennyAPIError): pass

class InvalidFileError(LennyAPIError): pass

class DatabaseInsertError(LennyAPIError): pass

class FileTooLargeError(LennyAPIError): pass

class S3UploadError(LennyAPIError): pass
