import httpx
from io import BytesIO
from lenny.configs import LENNY_HTTP_HEADERS    
import logging

logger = logging.getLogger(__name__)

class LennyClient:

    UPLOAD_API_URL = f"http://localhost:1337/v1/api/upload"
    HTTP_HEADERS = LENNY_HTTP_HEADERS

    @classmethod
    def upload(cls, olid: int, file_content: BytesIO, encrypted: bool = False,  timeout: int = 120) -> bool:
        data_payload = {
            'openlibrary_edition': olid,
            'encrypted': str(encrypted).lower()
        }
        files_payload = {
            'file': ('book.epub', file_content, 'application/epub+zip')
        }
        try:
            with httpx.Client(verify=False) as client:
                response = client.post(
                    cls.UPLOAD_API_URL,
                    data=data_payload,
                    files=files_payload,
                    headers=cls.HTTP_HEADERS,
                    timeout=timeout
                )
                logger.info(f"Upload response (OLID: {olid}): {response.content}")
                response.raise_for_status()
                return True
        except httpx.HTTPError as e:
            logger.error(f"Error uploading to Lenny (OLID: {olid}): {e}")
            return False
