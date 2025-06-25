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

### Quick Test

To quickly test LCP encryption:

```bash
# Ensure services are running
docker compose up -d

# Encrypt a test publication
docker compose exec lcpencrypt /usr/local/bin/lcpencrypt \
  -input /bookshelf/test.epub \
  -contentid test-book \
  -storage /srv/tmp \
  -url http://localhost:8080/static \
  -lcpsv http://admin:zvQ4nzc5GuIUEFLtsgm0@lcpserver:8989 \
  -verbose
```


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

### Test with External Content

You can also encrypt content from HTTP URLs:

```bash
docker compose exec lcpencrypt /usr/local/bin/lcpencrypt \
  -input "https://www.gutenberg.org/ebooks/11.epub.noimages" \
  -contentid external-book \
  -storage /srv/tmp \
  -url http://localhost:8080/static \
  -lcpsv http://admin:zvQ4nzc5GuIUEFLtsgm0@lcpserver:8989 \
  -verbose
```

## Testing License Management

### Check LCP Server Status

```bash
# Test LCP server health
curl http://localhost:8989/health

# List available licenses (requires authentication)
curl -u admin:zvQ4nzc5GuIUEFLtsgm0 http://localhost:8989/lcpserver/licenses
```

### Check LSD Server Status

```bash
# Test LSD server health
curl http://localhost:8990/health
```

## Configuration Details

### Authentication Credentials

The LCP system uses the following credentials (from `.env` file):

- **LCP Server Username**: `admin`
- **LCP Server Password**: `zvQ4nzc5GuIUEFLtsgm0`
- **LSD Notify Username**: `VtLF3tLnRmHUPQiR`
- **LSD Notify Password**: `MAMC1Qd66pcJ3AAwDzDnNDCsDM0SUsetiIfnrN3V`

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

3. **"401 Unauthorized" from LCP server**
   - Check credentials in `.env` file
   - Ensure lcpserver service is running
   - Verify authentication format: `http://username:password@lcpserver:8989`

4. **Database connection issues**
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
