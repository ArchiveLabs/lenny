import httpx
from io import BytesIO
from typing import Optional
from lenny.configs import LENNY_HTTP_HEADERS
import logging

logger = logging.getLogger(__name__)

EPUB_HEADER = b'PK\x03\x04'
HTTP_TIMEOUT = 15


def verify_epub(content: Optional[BytesIO]) -> Optional[BytesIO]:
    """Return `content` if it looks like an EPUB (a ZIP), else None."""
    if not content or not content.getbuffer().nbytes:
        return None
    header = content.read(4)
    content.seek(0)
    if not header.startswith(EPUB_HEADER):
        logger.warning(f"Downloaded file failed EPUB verification (bad magic bytes: {header!r})")
        return None
    return content


def download_epub(url: str, timeout: Optional[int] = None, headers: Optional[dict] = None) -> Optional[BytesIO]:
    """Stream an EPUB into memory. Returns None on 404, timeout, or transport error."""
    try:
        with httpx.Client() as client:
            with client.stream(
                "GET", url,
                headers=headers or LENNY_HTTP_HEADERS,
                follow_redirects=True,
                timeout=timeout or HTTP_TIMEOUT,
            ) as response:
                if response.status_code == 404:
                    logger.warning(f"EPUB not found (404): {url}")
                    return None
                response.raise_for_status()
                content = BytesIO()
                for chunk in response.iter_bytes(chunk_size=8192):
                    content.write(chunk)
                content.seek(0)
                return content
    except httpx.TimeoutException:
        logger.error(f"Timed out downloading {url}")
        return None
    except httpx.HTTPError as e:
        logger.error(f"Error downloading {url}: {e}")
        return None


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
                if response.status_code == 409:
                    logger.info(f"Skipping OLID {olid}: already exists")
                    return True
                response.raise_for_status()
                return True
        except httpx.HTTPError as e:
            logger.error(f"Error uploading to Lenny (OLID: {olid}): {e}")
            return False
