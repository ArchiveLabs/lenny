import requests
import tempfile
import re
import os
import time

OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json?q=id_standard_ebooks:*&fields=key,title,id_standard_ebooks,editions&limit=103"
STANDARDEBOOKS_BASE_URL = "https://standardebooks.org/ebooks"
UPLOAD_API_URL = "http://localhost:{LENNY_PORT}/v1/api/upload"

def extract_edition_number(ol_key_str):
    if not ol_key_str:
        return None
    match = re.search(r"/OL(\d+)[MW]$", ol_key_str)
    if match:
        return int(match.group(1))
    return None

def process_book(session, book_data):
    title = book_data.get('title', 'Unknown Title')
    standard_ebook_ids = book_data.get("id_standard_ebooks")
    if not standard_ebook_ids:
        return False
    
    standard_ebook_id = standard_ebook_ids[0] 
    standard_ebook_id_path_part = standard_ebook_id
    standard_ebook_id_file_part = standard_ebook_id.replace("/", "_")
    download_url = f"{STANDARDEBOOKS_BASE_URL}/{standard_ebook_id_path_part}/downloads/{standard_ebook_id_file_part}.epub?source=download"
    
    tmp_epub_filepath = None

    try:
        response = session.get(download_url, stream=True, timeout=60, allow_redirects=True)
        response.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp_epub_file:
            for chunk in response.iter_content(chunk_size=8192):
                tmp_epub_file.write(chunk)
            tmp_epub_filepath = tmp_epub_file.name
        
        ol_edition_key_str = None
        editions_data = book_data.get("editions")
        if editions_data:
            edition_docs = editions_data.get("docs")
            if edition_docs and isinstance(edition_docs, list) and len(edition_docs) > 0:
                first_edition = edition_docs[0]
                if isinstance(first_edition, dict):
                    ol_edition_key_str = first_edition.get("key")
        
        if not ol_edition_key_str:
            ol_edition_key_str = book_data.get("key")
            if not ol_edition_key_str:
                if tmp_epub_filepath and os.path.exists(tmp_epub_filepath):
                    os.remove(tmp_epub_filepath)
                return False

        openlibrary_edition_num = extract_edition_number(ol_edition_key_str)
        if openlibrary_edition_num is None:
            if tmp_epub_filepath and os.path.exists(tmp_epub_filepath):
                os.remove(tmp_epub_filepath)
            return False

        data_payload = {
            'openlibrary_edition': openlibrary_edition_num,
            'encrypted': False 
        }
        
        with open(tmp_epub_filepath, 'rb') as f_to_upload:
            files_payload = {
                'file': (os.path.basename(tmp_epub_filepath), f_to_upload, 'application/epub+zip')
            }
            upload_response = session.post(UPLOAD_API_URL, data=data_payload, files=files_payload, timeout=120, verify=False)
            upload_response.raise_for_status()
            return True

    except requests.exceptions.RequestException as e:
        return False
    except IOError as e:
        return False
    finally:
        if tmp_epub_filepath and os.path.exists(tmp_epub_filepath):
            try:
                os.remove(tmp_epub_filepath)
            except OSError:
                pass
        time.sleep(0.1)
    return False


def main():
    uploaded_books_count = 0
    print("Starting to load books from Open Library...")
    with requests.Session() as session:
        session.headers.update({'User-Agent': 'OpenBooksLoaderScript/1.0'})
        try:
            response = session.get(OPENLIBRARY_SEARCH_URL, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException:
            return
        except ValueError:
            return

        books = data.get("docs", [])
        if not books:
            return
            
        for book in books:
            if isinstance(book, dict):
                if process_book(session, book):
                    uploaded_books_count += 1
        
        print(f"Total books successfully uploaded: {uploaded_books_count}")

if __name__ == "__main__":
    requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)
    main()
