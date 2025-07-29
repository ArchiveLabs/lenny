# Routing Documentation

Let's assume our server is called lib.org...

* A request to `lib.org/v1/api/read/{id}` which redirects to thorium web on lib.org:3000/read?book=lib.org/v1/api/items/{id}/manifest.json (with CORS enabled for :3000 on FastAPI)
* `thorium web` then fetches this manifest `lib.org/v1/api/items/{id}/manifest.json` served by FastAPI, which makes an internal network call to `lib.org:15081/{base64(id)}/manifest.json`
* Once thorium web has established `lib.org/v1/api/items/{id}/manifest.json` as "self" it tries to make a series of request for content to for `lib.org/v1/api/read/{id}/*`
* Any request to `lib.org/v1/api/read/{id}/{path}` will be auth-checked, rewritten and then proxied internally to `lib.org:15081/{base64(id)}/{path}`

## API Usage Examples (curl)



### Borrow a Book (Open Access)
```sh
curl -X POST "http://localhost:8080/v1/api/items/{openlibrary_edition}/borrow"
```

### Borrow a Book (Encrypted, OTP Flow)
1. Authenticate with OTP to set cookies:
```sh
curl -X POST "http://localhost:8080/v1/api/items/{openlibrary_edition}/borrow" \
  -H "Content-Type: application/json" \
  -c cookies.txt \
  -d '{"otp": "STATIC_OTP"}'
```

2. Borrow the book using the cookies set above:
```sh
curl -X POST "http://localhost:8080/v1/api/items/{openlibrary_edition}/borrow" \
  -b cookies.txt
```

> Replace `STATIC_OTP` with your actual OTP key. The first call sets cookies, the second call borrows the book.


### Return a Book
```sh
curl -X POST "http://localhost:8080/v1/api/items/{openlibrary_edition}/return" \
  --cookie "email=user@example.com; session=SIGNED_SESSION_COOKIE"
```

### Get Borrowed Items
```sh
curl -X POST "http://localhost:8080/v1/api/items/borrowed" \
  --cookie "email=user@example.com; session=SIGNED_SESSION_COOKIE"
```

### Checkout Multiple Books
```sh
curl -X POST "http://localhost:8080/v1/api/items/checkout" \
  --cookie "email=user@example.com; session=SIGNED_SESSION_COOKIE" \
  -d '{"openlibrary_editions": [12345678, 23456789]}'
```

### Authenticate (get cookies for encrypted books)
```sh
# The /auth endpoint will set both 'email' and 'session' cookies if OTP is valid.
curl -X POST "http://localhost:8080/v1/api/auth" \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "otp": "STATIC_OTP"}'
```

> **Note:**
> For encrypted books, you must authenticate first (using `/auth`) to set the `email` and `session` cookies before borrowing or reading. The `session` cookie is a signed value and must match the email. Use the `/auth` endpoint or the OTP flow to obtain valid cookies.
