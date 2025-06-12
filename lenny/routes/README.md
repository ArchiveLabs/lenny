# Routing Documentation

Let's assume our server is called lib.org...

* A request to `lib.org/v1/api/read/{id}` which redirects to thorium web on lib.org:3000/read?book=lib.org/v1/api/items/{id}/manifest.json (with CORS enabled for :3000 on FastAPI)
* `thorium web` then fetches this manifest `lib.org/v1/api/items/{id}/manifest.json` served by FastAPI, which makes an internal network call to `lib.org:15081/{base64(id)}/manifest.json`
* Once thorium web has established `lib.org/v1/api/items/{id}/manifest.json` as "self" it tries to make a series of request for content to for `lib.org/v1/api/read/{id}/*`
* Any request to `lib.org/v1/api/read/{id}/{path}` will be auth-checked, rewritten and then proxied internally to `lib.org:15081/{base64(id)}/{path}`
