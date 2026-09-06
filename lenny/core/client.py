import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx
from io import BytesIO
from typing import Optional
from lenny.configs import LENNY_HTTP_HEADERS
import logging

logger = logging.getLogger(__name__)

EPUB_HEADER = b'PK\x03\x04'
HTTP_TIMEOUT = 15
# No EPUB is anywhere near this large; the cap exists so a hostile or broken
# URL cannot stream unbounded bytes into the API container's memory.
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_REDIRECTS = 5


def safe_log_url(url: str) -> str:
    """`url` with its query string dropped, for logging.

    Book URLs handed to us by a marketplace are often pre-signed (the
    signature lives in the query string), so logging one verbatim writes a
    working credential into the container logs.
    """
    return urlparse(url)._replace(query="", fragment="").geturl()


def is_https_url(url: str) -> bool:
    """True if `url` is a syntactically valid https URL.

    Split out from `assert_fetchable` because it costs nothing: callers that
    only want to reject junk early (parsing a redeem response) can use it
    without paying for a DNS lookup.
    """
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.hostname)


def assert_fetchable(url: str) -> None:
    """Raise ValueError unless `url` is https and resolves to a public address.

    Download URLs arrive from a third party (a BRIET bundle names where each
    book lives), so fetching one unchecked is a server-side request forgery
    primitive aimed at everything reachable from inside the compose network —
    Postgres, Garage, the cloud metadata endpoint, the API's own /upload.

    ponytail: resolves once and then trusts httpx to resolve the same name the
    same way, so a DNS-rebinding attacker can still get through. Pin the
    resolved IP and pass an explicit Host header if that ever matters.
    """
    parsed = urlparse(url)
    if not is_https_url(url):
        raise ValueError(f"refusing non-https download URL: {safe_log_url(url)}")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"could not resolve download host {parsed.hostname}") from e
    for info in addresses:
        ip = ipaddress.ip_address(info[4][0])
        # is_global is False for private, loopback, link-local, and reserved
        # ranges — one stdlib check instead of a hand-maintained CIDR list.
        if not ip.is_global:
            raise ValueError(f"refusing download from non-public address {ip}")


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
    """Stream an EPUB into memory. Returns None on 404, timeout, or transport error.

    Redirects are followed by hand rather than by httpx so that every hop is
    re-checked by `assert_fetchable` — otherwise a public https URL could
    bounce us straight onto an internal address.
    """
    try:
        with httpx.Client() as client:
            for _ in range(MAX_REDIRECTS + 1):
                assert_fetchable(url)
                with client.stream(
                    "GET", url,
                    headers=headers or LENNY_HTTP_HEADERS,
                    follow_redirects=False,
                    timeout=timeout or HTTP_TIMEOUT,
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            logger.error(f"Redirect without a Location header: {safe_log_url(url)}")
                            return None
                        url = urljoin(url, location)
                        continue
                    if response.status_code == 404:
                        logger.warning(f"EPUB not found (404): {safe_log_url(url)}")
                        return None
                    response.raise_for_status()
                    content = BytesIO()
                    for chunk in response.iter_bytes(chunk_size=8192):
                        content.write(chunk)
                        if content.tell() > MAX_DOWNLOAD_BYTES:
                            logger.error(
                                f"Aborting download over {MAX_DOWNLOAD_BYTES} bytes: {safe_log_url(url)}"
                            )
                            return None
                    content.seek(0)
                    return content
            logger.error(f"Too many redirects downloading {safe_log_url(url)}")
            return None
    except ValueError as e:
        logger.error(f"Refusing to download: {e}")
        return None
    except httpx.TimeoutException:
        logger.error(f"Timed out downloading {safe_log_url(url)}")
        return None
    except httpx.HTTPError as e:
        logger.error(f"Error downloading {safe_log_url(url)}: {e}")
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
