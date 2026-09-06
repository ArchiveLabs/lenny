# End-to-end browser tests

These drive a real Chromium against a running Lenny node. They are skipped
unless `--lenny` is supplied, so the normal suite stays hermetic.

## Why they exist

`TestClient` cannot honestly exercise the consent screen: it does not follow a
303 across origins, run the form, or carry a `Set-Cookie` the way a browser
does. The consent screen is the one part of this flow a patron actually sees.

## Running

Real Postgres, because SQLite hides timestamp and sequence differences:

```bash
docker run -d --name lenny_oauth2_testdb \
  -e POSTGRES_DB=lenny_test -e POSTGRES_USER=lenny -e POSTGRES_PASSWORD=lennytest \
  -p 127.0.0.1:55432:5432 postgres:16

export TESTING=false PYTHONPATH=$PWD \
       DB_USER=lenny DB_PASSWORD=lennytest DB_HOST=127.0.0.1 \
       DB_PORT=55432 DB_NAME=lenny_test

# The seed MUST match what pytest uses, or every session cookie the tests mint
# is rejected and the flow falls through to the login screen. pytest.ini sets
# it via pytest-env, and that value wins inside pytest regardless of the shell.
export LENNY_SEED=test-seed-for-unit-tests-32chars

# The OAuth metadata document is built from the node's configured public URL,
# never from the request, so a node on a non-default port has to be told where
# it lives. Without this the mock consumer follows discovery to localhost:8080
# and gets a 405 from whatever is there.
export LENNY_PROXY=http://127.0.0.1:8097

alembic upgrade head
uvicorn lenny.app:app --host 127.0.0.1 --port 8097 &

pytest tests/test_oauth2_browser.py --lenny http://127.0.0.1:8097
```

Pick a port nothing else holds — an unrelated listener on the same port shows
up as a bare `404` from the fixtures, which reads like a routing bug.

## The whole OL -> Lenny arc

To drive the same flow with a book Open Library actually parsed out of the live
feed, run the harvest inside an Open Library checkout first — it needs Python
3.14, which is why it cannot share this process:

```python
# openlibrary/ $ PYTHONPATH=. python - > /tmp/harvest.json
import json, urllib.request
from openlibrary.bookworm.opds import Feed, Publication, to_import_record

feed = Feed(provider_name="lenny", id_strategy="self_link")
raw = json.load(urllib.request.urlopen(
    "https://lennyforlibraries.org/v1/api/opds?limit=100", timeout=60))
pubs = [Publication(**p) for p in raw["publications"]]
records = [r for r in (to_import_record(p, feed) for p in pubs) if r]
borrowable = [r for r in records
              if any(a["data"]["access"] == "borrow" for a in r["acquisitions"])]
print(json.dumps({"publications": len(pubs), "records": len(records),
                  "borrowable": len(borrowable), "chosen": borrowable[0]}))
```

Then pass it to the consumer, which borrows what the harvest found:

```bash
python scripts/mock_openlibrary.py --lenny http://127.0.0.1:8097 \
  --client-id "$ID" --client-secret "$SECRET" \
  --session "$COOKIE" --harvest /tmp/harvest.json
```

The node must actually hold that edition, and needs `LENNY_PROXY` set — see
above. Requires openlibrary#13561, without which the harvest finds no borrowable
publications at all.

## The mock consumer

`scripts/mock_openlibrary.py` drives the same flow without a browser, and
covers what the browser tests cannot: the back-channel token exchange, code
replay, refresh rotation, scope enforcement and revocation.

```bash
# Clients are operator-registered, so issue credentials first:
python scripts/oauth2_client.py register "Open Library" http://127.0.0.1:8092/callback

COOKIE=$(python -c "from lenny.core import auth; \
  print(auth.create_session_cookie('patron@example.org'))")
python scripts/mock_openlibrary.py --lenny http://127.0.0.1:8097 \
  --client-id "$ID" --client-secret "$SECRET" \
  --session "$COOKIE" --edition 37044487
```

It found a bug the other layers could not: the discovery document advertised
`localhost:8080` regardless of where the node was bound, so a consumer
following RFC 8414 was sent to endpoints that did not exist.
