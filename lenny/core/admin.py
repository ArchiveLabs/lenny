from lenny.configs import S3_CONFIG

def verify_librarian(s3_access_key: str, s3_secret_key: str) -> bool:
    """Verify librarian credentials against S3 configuration."""
    return (
        s3_access_key == S3_CONFIG["access_key"]
        and s3_secret_key == S3_CONFIG["secret_key"]
    )