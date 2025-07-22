# LCP (Licensed Content Protection) Testing Guide

This guide explains how to test the LCP encryption service in the Lenny project, which provides DRM protection for EPUB and PDF publications.

## Overview

The LCP system consists of three main components:

- **lcpencrypt**: Command-line tool for encrypting publications
- **lcpserver**: License server that manages DRM licenses
- **lsdserver**: License Status Document server for license management

## Testing

### LCP (Licensed Content Protection) Testing

For detailed instructions on testing the DRM encryption functionality, see:

📖 **[LCP Testing Guide](./LCP_TESTING_README.md)**

This guide covers:

* Setting up and testing the `lcpencrypt` service
* Encrypting EPUB/PDF publications with DRM protection
* Testing license server integration
* Troubleshooting common issues

## Prerequisites

1. Ensure all services are running:

   ```bash
   docker compose up -d
   ```

2. Verify services are healthy:

   ```bash
   docker compose ps
   ```

   You should see all services running, including:
   - `lcpencrypt` (utility container)
   - `lcpserver` (port 8989)
   - `lsdserver` (port 8990)
   - `lenny_db` (PostgreSQL database)

## Testing the LCP Encryption Service

### Step 1: Prepare Test Content

1. **Option A: Use the provided test file**
   
   A test EPUB file should already be available at:

   ```text
   ./readium/bookshelf/test.epub
   ```

2. **Option B: Download a new test file**
   
   ```bash
   cd readium/bookshelf
   curl -L -o test.epub "https://www.gutenberg.org/ebooks/11.epub.noimages"
   ```

3. **Option C: Add your own EPUB/PDF files**
   
   Place any EPUB or PDF files in the `./readium/bookshelf/` directory.

### Step 2: Basic Encryption (Without LCP Server Integration)

Test basic encryption functionality:

```bash
docker compose exec lcpencrypt /usr/local/bin/lcpencrypt \
  -input /bookshelf/test.epub \
  -contentid my-test-book \
  -storage /srv/tmp \
  -url http://localhost:8080/static \
  -verbose
```

**Expected Output:**

```text
Output path: /srv/tmp/my-test-book
The encryption took [X]ms
```

### Step 3: Full LCP Integration (With License Server)

Test encryption with LCP server integration and license creation:

```bash
docker compose exec lcpencrypt /usr/local/bin/lcpencrypt \
  -input /bookshelf/test.epub \
  -contentid my-protected-book \
  -storage /srv/tmp \
  -url http://localhost:8080/static \
  -lcpsv http://admin:zvQ4nzc5GuIUEFLtsgm0@lcpserver:8989 \
  -verbose
```

**Expected Output:**

```text
Output path: /srv/tmp/my-protected-book
LCP Server Notification:
{
 "content-id": "my-protected-book",
 "content-encryption-key": "[base64-key]",
 "storage-mode": 2,
 "protected-content-location": "http://localhost:8080/static/my-protected-book",
 "protected-content-disposition": "test.epub",
 "protected-content-length": [size],
 "protected-content-sha256": "[hash]",
 "protected-content-type": "application/epub+zip"
}
The LCP Server was notified
The encryption took [X]ms
```

### Step 4: Verify Encrypted Files

Check that encrypted files were created:

```bash
# List encrypted files
docker compose exec lcpencrypt ls -la /srv/tmp/

# Or check on host filesystem
ls -la readium/tmp/
```

## Advanced Testing Options

### Test with Custom Parameters

```bash
docker compose exec lcpencrypt /usr/local/bin/lcpencrypt \
  -input /bookshelf/test.epub \
  -contentid custom-book-id \
  -storage /srv/tmp \
  -url http://localhost:8080/static \
  -filename custom-filename.epub \
  -cover true \
  -lcpsv http://admin:zvQ4nzc5GuIUEFLtsgm0@lcpserver:8989 \
  -verbose
```

## How to Create Licenses

To create licenses that will appear in the `/licenses` endpoint, follow this complete workflow:

### Step 1: Encrypt Content with License Creation

```bash
# This creates encrypted content AND notifies the LCP server
docker compose exec lcpencrypt /usr/local/bin/lcpencrypt \
  -input /bookshelf/test.epub \
  -contentid my-licensed-book \
  -storage /srv/tmp \
  -url http://localhost:8080/static \
  -lcpsv http://admin:zvQ4nzc5GuIUEFLtsgm0@lcpserver:8989 \
  -verbose
```

**Expected Output:**

```text
Output path: /srv/tmp/my-licensed-book
LCP Server Notification:
{
 "content-id": "my-licensed-book",
 "content-encryption-key": "[base64-key]",
 "storage-mode": 2,
 "protected-content-location": "http://localhost:8080/static/my-licensed-book",
 "protected-content-disposition": "test.epub",
 "protected-content-length": [size],
 "protected-content-sha256": "[hash]",
 "protected-content-type": "application/epub+zip"
}
The LCP Server was notified
The encryption took [X]ms
```

### Step 2: Create a License for a Specific User

Based on the official [Readium LCP Server repository](https://github.com/readium/readium-lcp-server), licenses are created using the `/contents/{content_id}/license` endpoint. However, the current implementation requires specific user authentication and passphrase hashing that may not be fully configured.

**Current Status:**

- ✅ Content encryption works correctly
- ✅ LCP and LSD servers are running and accessible
- ✅ License creation via API works with correct payload and credentials
- ✅ The `/licenses` endpoint returns created licenses after successful license creation

**For production use, licenses would typically be created through:**

1. A frontend application that handles user authentication
2. Proper passphrase hashing using the required algorithm
3. Integration with a Content Management System (CMS)

The LCP system is **functioning correctly** for content encryption and license creation. The license creation process requires a valid API payload and working credentials.

## Testing License Management

### Working LCP Server API Endpoints

```bash
# List encrypted content/publications (WORKS - no auth required)
curl -X GET http://localhost:8989/contents
```

```bash
# Fetch a specific encrypted publication (replace <content_id> as needed)
curl -X GET http://localhost:8989/contents/<content_id>
```

```bash
# Create a license for a user (replace <content_id> as needed)
curl -u "admin:zvQ4nzc5GuIUEFLtsgm0" \
  -H "Content-Type: application/json" \
  -X POST http://localhost:8989/contents/<content_id>/license \
  -d '{
    "provider": "http://localhost:8989",
    "user": {
      "id": "test-user-1",
      "email": "testuser@example.com",
      "encrypted": ["email"]
    },
    "encryption": {
      "user_key": {
        "text_hint": "The title of the first book you ever read",
        "hex_value": "4981AA0A50D563040519E9032B5D74367B1D129E239A1BA82667A57333866494"
      }
    },
    "rights": {
      "print": 10,
      "copy": 2048,
      "start": "2025-07-08T01:08:15+01:00",
      "end": "2025-07-25T01:08:15+01:00"
    }
  }'
```

**Expected Response:**

A JSON license document with a unique `id`, `links`, `user`, `rights`, and `signature` fields. Example:

```json
{
  "provider": "http://localhost:8989",
  "id": "<license-id>",
  "issued": "2025-07-08T18:13:22Z",
  ...
}
```

### Working LSD Server API Endpoints

**Note:** The LSD server root endpoint (`/`) returns 404 - this is expected behavior.

```bash
# List available licenses (WORKS - requires authentication)
curl -u "admin:zvQ4nzc5GuIUEFLtsgm0" -X GET http://localhost:8990/licenses
```

**Expected Response:**

```bash
[{"id":"c702d38f-3720-4970-978c-fa3a0e5a9799","status":"ready","updated":{"license":"2025-07-08T19:06:08Z","status":"2025-07-08T19:06:08Z"},"message":"","device_count":0}]
```

**Note:** The licenses list will be empty until you create licenses. This is normal behavior for a fresh setup.

**To view formatted JSON in browser:**

1. Visit: `http://localhost:8990/licenses`
2. Enter credentials: Username: `admin`, Password: `zvQ4nzc5GuIUEFLtsgm0`
3. Install a JSON formatter browser extension for better readability

```bash
# Check license status (WORKS - requires valid license ID and authentication)
curl -u "admin:zvQ4nzc5GuIUEFLtsgm0" -X GET "http://localhost:8990/licenses/{license-id}/status"
```

**Expected Response:**

```bash
{
  "id": "c702d38f-3720-4970-978c-fa3a0e5a9799",
  "status": "ready",
  "updated": {
    "license": "2025-07-08T19:06:08Z",
    "status": "2025-07-08T19:06:08Z"
  },
  "message": "The license is in ready state",
  "links": [
    {
      "rel": "self",
      "href": "http://lsdserver:8990/licenses/c702d38f-3720-4970-978c-fa3a0e5a9799/status",
      "type": "application/vnd.readium.license.status.v1.0+json"
    },
    {
      "rel": "license",
      "href": "http://0.0.0.0:8989/lcp/licenses/c702d38f-3720-4970-978c-fa3a0e5a9799",
      "type": "application/vnd.readium.lcp.license.v1.0+json"
    },
    {
      "rel": "register",
      "href": "http://lsdserver:8990/licenses/c702d38f-3720-4970-978c-fa3a0e5a9799/register{?id,name}",
      "type": "application/vnd.readium.license.status.v1.0+json",
      "templated": true
    },
    {
      "rel": "return",
      "href": "http://lsdserver:8990/licenses/c702d38f-3720-4970-978c-fa3a0e5a9799/return{?id,name}",
      "type": "application/vnd.readium.license.status.v1.0+json",
      "templated": true
    }
  ],
  "potential_rights": {
    "end": "2025-09-06T19:06:08Z"
  }
}
```

### Authentication Details

**LSD Server Authentication:**

- **Username:** `admin` (from LCP_HTPASSWD_USER)
- **Password:** `zvQ4nzc5GuIUEFLtsgm0` (from LCP_HTPASSWD_PASS)

**Working Authentication Example:**

```bash
# List licenses with correct authentication
curl -u "admin:zvQ4nzc5GuIUEFLtsgm0" -X GET http://localhost:8990/licenses
```

## Configuration Details

### Authentication Credentials

The LCP system uses the following credentials (from `.env` file):

**For LCP Server Operations:**

- **LCP Update Username**: `XBqmgJg7JHl9RowY` (LCP_UPDATE_USER)
- **LCP Update Password**: `73WkskHlEfdMRfsnCPDW68Kzc0RGqiWtqq7rSOsF` (LCP_UPDATE_PASS)

**For LSD Server API Access:**

- **Username**: `admin` (LCP_HTPASSWD_USER)
- **Password**: `zvQ4nzc5GuIUEFLtsgm0` (LCP_HTPASSWD_PASS)

**For LSD Notifications:**

- **LSD Notify Username**: `VtLF3tLnRmHUPQiR` (LSD_NOTIFY_USER)
- **LSD Notify Password**: `MAMC1Qd66pcJ3AAwDzDnNDCsDM0SUsetiIfnrN3V` (LSD_NOTIFY_PASS)

**Note:** The htpasswd file currently only contains the `admin` user for LSD server access.

### File Paths and Volumes

- **Input files**: Place in `./readium/bookshelf/` (mounted as `/bookshelf` in container)
- **Output files**: Created in `./readium/tmp/` (mounted as `/srv/tmp` in container)
- **Configuration**: Located in `./readium/config/` (mounted as `/srv/config` in container)

### Available Parameters

The `lcpencrypt` tool supports these parameters:

- `-input`: Source file (local path or HTTP URL)
- `-contentid`: Unique identifier for the content
- `-storage`: Output directory path
- `-url`: Base URL for accessing encrypted content
- `-filename`: Custom filename for output (optional)
- `-temp`: Working directory for temporary files (optional)
- `-cover`: Generate covers when possible (optional, boolean)
- `-contentkey`: Custom base64 content key (optional)
- `-lcpsv`: LCP server URL with credentials (optional)
- `-v2`: Use License Server v2 protocol (optional, boolean)
- `-notify`: CMS notification endpoint (optional)
- `-verbose`: Show detailed information (optional, boolean)

## Troubleshooting

### Common Issues

1. **"lcpencrypt service not running"**

   ```bash
   docker compose up -d lcpencrypt
   ```

2. **"no such file or directory"**
   - Verify the input file exists in `./readium/bookshelf/`
   - Use correct container path: `/bookshelf/filename.epub`

3. **"404 Not Found" on LSD server root endpoint**
   - **This is expected behavior** - LSD server has no root endpoint
   - Use specific endpoints like `/licenses` instead

4. **"404 - license Status not found"**
   - **This is normal** when no licenses exist yet
   - Create a license first using the LCP encryption process

5. **"401 Unauthorized" from LSD server**
   - Use correct credentials: `admin:zvQ4nzc5GuIUEFLtsgm0`
   - Ensure authentication format: `-u "admin:zvQ4nzc5GuIUEFLtsgm0"`

6. **"401 Unauthorized" from LCP server**
   - Check credentials in `.env` file
   - Ensure lcpserver service is running
   - Verify authentication format: `http://username:password@lcpserver:8989`

7. **Database connection issues**
   - Ensure `lenny_db` service is healthy
   - Check database configuration in `readium/config/config.yaml`

### Debug Commands

```bash
# Check service logs
docker compose logs lcpencrypt
docker compose logs lcpserver
docker compose logs lsdserver

# Check service status
docker compose ps

# Access container shell for debugging
docker compose exec lcpencrypt /bin/bash
```

## Integration with Lenny API

The encrypted content can be served through the Lenny API. The encrypted files in `/srv/tmp/` are accessible via:

```text
http://localhost:8080/static/[content-id]
```

This URL structure matches the `-url` parameter used during encryption, allowing seamless integration with the main Lenny application.

## Security Notes

- The test credentials are for development only
- In production, use strong, unique passwords
- Consider using environment variables for sensitive configuration
- The LCP certificates in `readium/config/` are test certificates

## Additional Resources

- [Readium LCP Specification](https://readium.org/lcp-specs/)
- [Readium LCP Server Documentation](https://github.com/readium/readium-lcp-server)
