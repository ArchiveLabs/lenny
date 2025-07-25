# Routing Documentation

Let's assume our server is called lib.org...

* A request to `lib.org/v1/api/read/{id}` which redirects to thorium web on lib.org:3000/read?book=lib.org/v1/api/items/{id}/manifest.json (with CORS enabled for :3000 on FastAPI)
* `thorium web` then fetches this manifest `lib.org/v1/api/items/{id}/manifest.json` served by FastAPI, which makes an internal network call to `lib.org:15081/{base64(id)}/manifest.json`
* Once thorium web has established `lib.org/v1/api/items/{id}/manifest.json` as "self" it tries to make a series of request for content to for `lib.org/v1/api/read/{id}/*`
* Any request to `lib.org/v1/api/read/{id}/{path}` will be auth-checked, rewritten and then proxied internally to `lib.org:15081/{base64(id)}/{path}`

## API Usage Examples (curl)

### Borrow a Book
```sh
curl -X POST "http://localhost:8080/v1/api/items/{openlibrary_editions}/borrow" \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com"}'
```

### Return a Book
```sh
curl -X POST "http://localhost:8080/v1/api/items/{openlibrary_editions}/return" \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com"}'
```


### Get Borrowed Items
```sh
curl -X POST "http://localhost:8080/v1/api/items/borrowed" \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com"}'
```

### Read a Borrowed Book (Encrypted)
> **Note:** The /read endpoint requires the email as a query parameter for encrypted books.
```sh
curl "http://localhost:8080/v1/api/items/{openlibrary_editions}/read?email=user@example.com"
```

### Checkout Multiple Books
```sh
curl -X POST "http://localhost:8080/v1/api/items/checkout" \
  -H "Content-Type: application/json" \
  -d '{"openlibrary_editions": [12345678, 23456789], "email": "user@example.com"}'
```

### Open Access Book Redirect
```sh
curl -X POST "http://localhost:8080/v1/api/item/openaccess" \
  -H "Content-Type: application/json" \
  -d '{"item_id": {openlibrary_editions}, "email": "user@example.com"}'
```
