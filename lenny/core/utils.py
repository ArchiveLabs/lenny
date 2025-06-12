
import base64

def encode_book_path(book_id: str, format=".epub") -> str:
    """This should be moved to a general utils.py within core"""
    if not "." in book_id:
        book_id += format
    path = f"s3://bookshelf/{book_id}"
    print(f"path: {path}")
    encoded = base64.b64encode(path.encode()).decode()
    return encoded.replace('/', '_').replace('+', '-').replace('=', '')
