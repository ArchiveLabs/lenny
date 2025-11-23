# Testing Rate Limiting

This guide explains how to verify that rate limiting is working correctly in Lenny.

## Prerequisites

1. Make sure Lenny is running:
   ```bash
   make start
   # or
   docker compose -p lenny up -d
   ```

2. Verify services are up:
   ```bash
   docker ps | grep lenny
   ```

3. Check the API is accessible:
   ```bash
   curl http://localhost:8080/v1/api/items
   ```

---

## Testing Methods

### Method 1: Quick Test with curl (Single Endpoint)

#### Test 1: Verify OPDS endpoints have NO rate limiting
```bash
# Make many rapid requests to /opds (should all succeed)
for i in {1..50}; do
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/v1/api/opds
done
# Expected: All return 200 (no rate limiting)
```

#### Test 2: Verify /items has NO rate limiting
```bash
# Make many rapid requests to /items (should all succeed)
for i in {1..50}; do
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/v1/api/items
done
# Expected: All return 200 (no rate limiting)
```

#### Test 3: Test STRICT rate limiting on /upload
```bash
# Make 25 requests rapidly (limit is 20/min + 5 burst = 25 total)
for i in {1..25}; do
  echo "Request $i:"
  curl -s -o /dev/null -w "HTTP %{http_code}\n" \
    -X POST http://localhost:8080/v1/api/upload \
    -F "openlibrary_edition=1" \
    -F "file=@/dev/null" 2>/dev/null || echo "Failed"
done
# Expected: First 25 succeed (20 + 5 burst), then 503 (Service Unavailable)
```

#### Test 4: Test GENERAL rate limiting on other endpoints
```bash
# Make 120 requests rapidly (limit is 100/min + 10 burst = 110 total)
for i in {1..120}; do
  echo "Request $i:"
  curl -s -o /dev/null -w "HTTP %{http_code}\n" \
    http://localhost:8080/v1/api/items/borrowed
done
# Expected: First 110 succeed, then 503 or 429
```

---

### Method 2: Comprehensive Test Script

Create a test script `test_rate_limits.sh`:

```bash
#!/bin/bash

BASE_URL="http://localhost:8080/v1/api"
echo "Testing Rate Limiting..."
echo "========================="

# Test 1: OPDS (should NOT be rate limited)
echo -e "\n[Test 1] OPDS endpoint (should NOT be rate limited):"
for i in {1..10}; do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/opds")
  echo -n "$STATUS "
done
echo ""

# Test 2: Items (should NOT be rate limited)
echo -e "\n[Test 2] Items endpoint (should NOT be rate limited):"
for i in {1..10}; do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/items")
  echo -n "$STATUS "
done
echo ""

# Test 3: General endpoint (should be rate limited after 110 requests)
echo -e "\n[Test 3] General endpoint rate limiting (100/min + 10 burst):"
SUCCESS=0
FAILED=0
for i in {1..120}; do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/items/borrowed")
  if [ "$STATUS" = "200" ] || [ "$STATUS" = "401" ]; then
    SUCCESS=$((SUCCESS + 1))
  else
    FAILED=$((FAILED + 1))
  fi
  if [ $i -eq 110 ]; then
    echo "At request 110 (limit + burst): Success=$SUCCESS, Failed=$FAILED"
  fi
done
echo "Total: Success=$SUCCESS, Failed=$FAILED"

# Test 4: Strict endpoint (should be rate limited after 25 requests)
echo -e "\n[Test 4] Strict endpoint rate limiting (20/min + 5 burst):"
SUCCESS=0
FAILED=0
for i in {1..30}; do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "$BASE_URL/authenticate" \
    -H "Content-Type: application/json" \
    -d '{"email":"test@example.com"}' 2>/dev/null)
  if [ "$STATUS" = "200" ] || [ "$STATUS" = "400" ] || [ "$STATUS" = "401" ]; then
    SUCCESS=$((SUCCESS + 1))
  else
    FAILED=$((FAILED + 1))
  fi
  if [ $i -eq 25 ]; then
    echo "At request 25 (limit + burst): Success=$SUCCESS, Failed=$FAILED"
  fi
done
echo "Total: Success=$SUCCESS, Failed=$FAILED"
```

Make it executable and run:
```bash
chmod +x test_rate_limits.sh
./test_rate_limits.sh
```

---

### Method 3: Using Apache Bench (ab) or wrk

#### Install Apache Bench (if not installed):
```bash
# macOS
brew install httpd

# Ubuntu/Debian
sudo apt-get install apache2-utils
```

#### Test rate limiting:
```bash
# Test general endpoint (100/min limit)
ab -n 120 -c 10 http://localhost:8080/v1/api/items/borrowed

# Test strict endpoint (20/min limit)
ab -n 30 -c 5 -p auth.json -T application/json \
   http://localhost:8080/v1/api/authenticate
```

---

## What to Look For

### Success Indicators:

1. **OPDS and /items endpoints**: 
   - Should return `200 OK` for all requests
   - No `429 Too Many Requests` or `503 Service Unavailable`

2. **Rate-limited endpoints**:
   - First N requests (limit + burst) return `200`, `401`, or `400`
   - Subsequent requests return:
     - `429 Too Many Requests` (from slowapi)
     - `503 Service Unavailable` (from Nginx)
     - Or both headers indicating rate limiting

3. **Response Headers**:
   ```bash
   curl -I http://localhost:8080/v1/api/items/borrowed
   ```
   Look for:
   - `X-RateLimit-Limit`: Rate limit value
   - `X-RateLimit-Remaining`: Remaining requests
   - `Retry-After`: When to retry (if rate limited)

---

## Checking Logs

### Nginx Logs (Rate Limit Blocks):
```bash
# Check Nginx error log for rate limit messages
docker exec lenny_api tail -f /var/log/nginx/error.log

# Check Nginx access log
docker exec lenny_api tail -f /var/log/nginx/access.log | grep "503"
```

### FastAPI/slowapi Logs:
```bash
# Check FastAPI application logs
docker compose logs -f api | grep -i "rate\|limit\|429"
```

### All Logs:
```bash
make log
# or
docker compose logs -f
```

---

## Testing Both Layers

### Test Nginx Layer (First Defense):
```bash
# Make requests that should be blocked by Nginx before reaching FastAPI
# Check Nginx logs for "limiting requests" messages
for i in {1..120}; do
  curl -s http://localhost:8080/v1/api/items/borrowed > /dev/null
done
docker exec lenny_api grep "limiting requests" /var/log/nginx/error.log
```

### Test slowapi Layer (Second Defense):
```bash
# If Nginx allows a request through, slowapi should catch it
# Look for 429 responses with X-RateLimit headers
curl -v http://localhost:8080/v1/api/items/borrowed 2>&1 | grep -i "rate"
```

---

## Expected Behavior Summary

| Endpoint | Rate Limit | Burst | Total Allowed | Status Code When Limited |
|----------|------------|-------|---------------|-------------------------|
| `/v1/api/opds*` | None | N/A | Unlimited | N/A |
| `/v1/api/items` | None | N/A | Unlimited | N/A |
| `/v1/api/upload` | 20/min | 5 | 25 | 503 (Nginx) or 429 (slowapi) |
| `/v1/api/authenticate` | 20/min | 5 | 25 | 503 (Nginx) or 429 (slowapi) |
| Other `/v1/api/*` | 100/min | 10 | 110 | 503 (Nginx) or 429 (slowapi) |

---

## Troubleshooting

### Rate limiting not working?

1. **Check Nginx configuration is loaded**:
   ```bash
   docker exec lenny_api nginx -t
   docker exec lenny_api nginx -s reload
   ```

2. **Verify slowapi is installed**:
   ```bash
   docker exec lenny_api pip list | grep slowapi
   ```

3. **Check if rate limit zones are active**:
   ```bash
   docker exec lenny_api cat /etc/nginx/nginx.conf | grep limit_req_zone
   ```

4. **Restart services**:
   ```bash
   make restart
   # or
   docker compose restart api
   ```

### Rate limiting too aggressive?

- Adjust limits in `lenny/core/ratelimit.py` or via environment variables
- Adjust Nginx limits in `docker/nginx/nginx.conf`
- Restart services after changes

---

## Quick Verification Commands

```bash
# 1. Check services are running
docker ps | grep lenny

# 2. Test OPDS (should work unlimited)
curl http://localhost:8080/v1/api/opds

# 3. Test rate limited endpoint (make 120 requests)
for i in {1..120}; do curl -s http://localhost:8080/v1/api/items/borrowed; done | tail -5

# 4. Check logs for rate limit messages
docker compose logs api | grep -i "rate\|429\|503" | tail -10
```

