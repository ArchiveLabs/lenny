
import requests
from io import BytesIO
from lenny.configs import LENNY_HTTP_HEADERS    

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
            response = requests.post(
                cls.UPLOAD_API_URL,
                data=data_payload,
                files=files_payload,
                headers=cls.HTTP_HEADERS,
                timeout=timeout,
                verify=False
            )
            print(response.content)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            print(f"Error uploading to Lenny (OLID: {olid}): {e}")
            return False
