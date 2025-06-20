
import logging
import boto3
from lenny.configs import S3_CONFIG

logger = logging.getLogger(__name__)

class LennyS3:

    BOOKSHELF_BUCKET = "bookshelf"
    
    def __init__(self):
        # Initialize S3 client for MinIO
        self.s3 = boto3.session.Session().client(
            service_name='s3',
            aws_access_key_id=S3_CONFIG['access_key'],
            aws_secret_access_key=S3_CONFIG['secret_key'],
            endpoint_url=f"http://{S3_CONFIG['endpoint']}",
            use_ssl=S3_CONFIG['secure']
        )
        self._initialize()

    def __getattr__(self, name):
        # Delegate any unknown attribute or method to the boto3 s3 client
        return getattr(self.s3, name)

    def _initialize(self):
        try:
            self.s3.head_bucket(Bucket=self.BOOKSHELF_BUCKET)
            logger.info(f"Bucket '{self.BOOKSHELF_BUCKET}' already exists.")
        except Exception as e:
            try:
                self.s3.create_bucket(Bucket=self.BOOKSHELF_BUCKET)
                logger.info(f"Bucket '{self.BOOKSHELF_BUCKET}' created successfully.")
            except Exception as create_error:
                logger.error(f"Error creating bucket '{self.BOOKSHELF_BUCKET}': {create_error}")

    def get_keys(self, bucket=None, prefix=''):
        """
        Lists all object keys (filenames) in a specified S3 bucket,
        optionally filtered by a prefix. Handles pagination automatically.
        """
        bucket=bucket or self.BOOKSHELF_BUCKET
        paginator = self.s3.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

        for page in pages:
            if 'Contents' in page:
                for obj in page['Contents']:
                    yield obj['Key']
