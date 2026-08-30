
# Lenny API Routing Documentation

Base URL: `http://localhost:8080/v1/api`

## Endpoints


### 1. Home Page

- **GET /**
  - Renders the index HTML page.
  - **Parameters:** None


### 2. Get Items

- **GET /items**
  - Returns enriched items.
  - **Query Parameters:**
    - `fields` (optional, comma-separated string): Fields to include in response
    - `offset` (optional, int): Pagination offset
    - `limit` (optional, int): Pagination limit
  - Example: `GET /items?fields=title,author&offset=0&limit=10`


### 3. OPDS Feed

- **GET /opds**
  - Returns OPDS feed.
  - Query params:
    - `offset` (int, optional): Items to skip. Paired with `limit`, pages the feed.
    - `limit` (int, optional): Items per page (default 50).
    - `modified_since` (str, optional): ISO 8601 date or timestamp
      (`2026-08-01`, `2026-08-01T12:30:00Z`). Limits the feed to items whose
      `updated_at` is at or after that instant. A value with no timezone is read
      as UTC. Returns `400` if unparseable.
  - Items are ordered oldest-change-first (`updated_at`, then `id`), so paging is
    stable and an incremental consumer can resume where it stopped.
  - The catalog carries a `rel=next` link while more items remain, preserving
    `modified_since`; each publication carries `metadata.modified`.
  - `metadata.numberOfItems` is the size of the whole matching set, not of the
    current page.
  - Example — everything changed since August 1st, 100 at a time:
    ```
    curl "http://localhost:8080/v1/api/opds?modified_since=2026-08-01&limit=100"
    ```
  - **Query Parameters:**
    - `offset` (optional, int): Pagination offset
    - `limit` (optional, int): Pagination limit


### 4. Read Book (Redirect)

- **GET /items/{book_id}/read**
  - Redirects to Thorium Web Reader for the book. Requires authentication.
  - **Path Parameters:**
    - `book_id` (str): Book identifier
  - **Query Parameters:**
    - `format` (optional, str, default: "epub"): Book format


### 5. Readium Manifest

- **GET /items/{book_id}/readium/manifest.json**
  - Returns Readium manifest for the book. Requires authentication.
  - **Path Parameters:**
    - `book_id` (str): Book identifier
  - **Query Parameters:**
    - `format` (optional, str, default: ".epub"): Book format


### 6. Proxy Readium Requests

- **GET /items/{book_id}/readium/{readium_path}**
  - Proxies requests to Readium server. Requires authentication.
  - **Path Parameters:**
    - `book_id` (str): Book identifier
    - `readium_path` (str): Path to resource in Readium
  - **Query Parameters:**
    - `format` (optional, str, default: ".epub"): Book format


### 7. Upload Item

- **POST /upload**
  - Uploads a PDF or EPUB file for an OpenLibrary edition.
  - **Form Data:**
    - `openlibrary_edition` (int, required): OpenLibrary Edition ID (must be positive)
    - `encrypted` (bool, optional, default: false): Set to true if file is encrypted
    - `file` (UploadFile, required): PDF or EPUB file (max 50MB)
  - Example:
    ```sh
    curl -X POST "http://localhost:8080/v1/api/upload" \
      -F "openlibrary_edition=12345678" \
      -F "encrypted=false" \
      -F "file=@book.epub"
    ```


### 8. Authenticate

- **POST /authenticate**
  - Authenticates user via email and OTP. Sets session cookie on success.
  - **Form Data:**
    - `email` (str, required): User email
    - `otp` (str, required): One-time password


### 9. Borrow Item

- **POST /items/{book_id}/borrow**
  - Borrows a book for the authenticated user. Requires session cookie for encrypted books.
  - **Path Parameters:**
    - `book_id` (int): Book identifier
  - **Body Parameters:**
    - `otp` (str, optional): One-time password (if not logged in)
  - Example:
    ```sh
    curl -X POST "http://localhost:8080/v1/api/items/12345678/borrow" \
      -H "Content-Type: application/json" \
      -d '{"otp": "123456"}'
    ```


### 10. Checkout Multiple Items

- **POST /items/checkout**
  - Checks out multiple books for a user.
  - **Body Parameters (JSON):**
    - `openlibrary_editions` (List[int], required): List of OpenLibrary Edition IDs
    - `email` (str, required): User email
  - Example:
    ```sh
    curl -X POST "http://localhost:8080/v1/api/items/checkout" \
      -H "Content-Type: application/json" \
      -d '{"openlibrary_editions": [12345678, 23456789], "email": "user@example.com"}'
    ```


### 11. Return Item

- **POST /items/{book_id}/return**
  - Returns a borrowed book for the authenticated user. Requires session cookie.
  - **Path Parameters:**
    - `book_id` (int): Book identifier


### 12. Get Borrowed Items

- **POST /items/borrowed**
  - Returns a list of active borrowed items for the authenticated user. Requires session cookie.


### 13. Logout

- **GET /logout**
  - Logs out the user by deleting the session cookie.
  - **Parameters:** None

---

## Authentication
Most endpoints require a valid session cookie. Use `/authenticate` to obtain one via email and OTP.

## Error Handling
Endpoints return appropriate HTTP status codes and error messages for unauthorized access, invalid input, and server errors.
