# Lenny Routes

The Routes directory defines and registers all the Routers responsible for handling requests and responses through the Lenny Lending system.

## Overview

This directory contains API endpoint definitions using FastAPI routers. These routes handle HTTP requests and provide appropriate responses, acting as the interface between clients and the Lenny system's core functionality.

## API Endpoints

### Root Endpoint (`api.py`)

- `GET /`: Returns an HTML welcome page with information about Lenny and a link to the GitHub repository

### Item Management (`api.py`)

- `POST /items`: Creates a new item in the system
  - Requires form data with item metadata and file upload
  - Validates librarian credentials via S3 access and secret keys
  - Supports PDF and EPUB formats only
  - Handles metadata such as:
    - identifier, title, language
    - lending settings (readable, lendable, waitlistable)
    - access control settings (print disabled, login required)
    - availability settings (total lendable copies, current lendable copies, waitlist size)
  - Interacts with core functions to handle file storage in S3 buckets

- `DELETE /items/{identifier}`: Removes an item from the system
  - Requires librarian authentication via S3 credentials
  - Returns information about the deleted item (identifier, title, and status)
  - Removes the item from both the database and associated S3 storage

## Integration

Routes are integrated with:

1. **Core Functions**: Routes call core modules (like `upload_item` and `delete_item`) to execute business logic
2. **Database Models**: Routes use SQLAlchemy session dependency for database operations
3. **Authentication**: Routes handle librarian credential validation via the core admin functions
4. **File Handling**: Routes process uploaded files and store them temporarily before passing to core functions
5. **Error Handling**: Routes implement appropriate HTTP status codes and error responses

## Future Routes

Planned or in-development routes may include:
- Management interfaces for librarians
- Reading interfaces for patrons
- OPDS catalog endpoints (Open Publication Distribution System)
- Usage statistics and reporting endpoints
- Waitlist and loan management