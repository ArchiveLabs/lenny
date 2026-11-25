"""Post-build: emit a second app shell so ONE published image serves the OPDS
client at both `/` (default installs) and `/app` (a host whose `/` is owned by
another site, e.g. lennyforlibraries.org).

SvelteKit's adapter-static SPA `fallback` always emits ABSOLUTE asset paths
(`/_app/...`) in index.html — `paths.relative` does not apply to a fallback,
because it is served at an unknown depth (verified against the built artifact).
Absolute paths are immune to serving depth, which is what we want; we just need
a copy whose absolute prefix is `/app`.

So we keep the original `build/index.html` exactly as built (served at `/`,
`base: ""`), and write `build/app/index.html` with every absolute reference
prefixed by `/app` and the SvelteKit base set to `/app`. The runtime base is
read from `__sveltekit_<hash>.base`, which drives:
  - the two inline `import("/_app/entry/...")` calls,
  - the modulepreload + CSS `<link href="/_app/...">`,
  - `$app/paths` `base` (used by the client's own `{base}/...` refs),
  - the version poll `fetch(`${base}/_app/version.json`)`.
The JS chunks themselves import each other RELATIVELY, so they need no edit; the
one asset tree under `build/_app` is served at both `/_app/` and `/app/_app/` by
nginx (one copy on disk). The service worker is already mount-relative (it keys
off its own path), so served at `/app/service-worker.js` it caches `/app/...`.

This is a deterministic transform of our own build output. It bakes a fixed
`/app` — the same for every install — so it is NOT a per-install build arg and
does not change the content-hash scheme (constraint 4): default installs serve
`/` and simply leave `/app` dormant.
"""
import base64
import hashlib
import os
import re
import sys

BASE = "/app"

src_path = "build/index.html"
out_dir = os.path.join("build", BASE.strip("/"))
out_path = os.path.join(out_dir, "index.html")

# The root shell served at "/" (every default install, and every self-hoster who
# takes the container nginx whether or not they use /app) MUST stay byte-for-byte
# unchanged — constraint 1. We only ever WRITE build/app/index.html; capture the
# root bytes now and assert they are untouched at the end.
_root_before = open(src_path, "rb").read()

html = open(src_path).read()


def swap(text, old, new, *, count):
    n = text.count(old)
    if n != count:
        sys.exit(f"mount-at-app: expected {count} of {old!r} in {src_path}, found {n}")
    return text.replace(old, new)


# Every substitution asserts its anchor, like lock-to-lenny.py: SvelteKit's
# generated shell is not contractual (`__sveltekit_<hash>` carries a per-build
# hash, the spelling of `base:""` can change), so a missing anchor FAILS THE
# BUILD LOUDLY. A half-rewritten shell that still renders is the worst outcome:
# it passes a smoke test and breaks one route.

# SvelteKit base: drives $app/paths, the version poll, and routing. Exactly one.
html = swap(html, 'base: ""', f'base: "{BASE}"', count=1)

# Service-worker registration URL and the (relative) manifest link: exactly one each.
html = swap(html, "'/service-worker.js'", f"'{BASE}/service-worker.js'", count=1)
html = swap(html, 'href="public/manifest.json"',
            f'href="{BASE}/public/manifest.json"', count=1)

# Absolute SvelteKit asset refs: modulepreload/css <link> + the two inline
# import() calls. The exact COUNT is not asserted because it tracks the chunk
# graph (it changes on any client upgrade); instead we assert a floor (the shell
# format still uses absolute "/_app/") and a hard post-condition (nothing is left
# unprefixed). Both the two import() calls and >=1 link must be present.
_n_app = html.count("/_app/")
if _n_app < 3:
    sys.exit(f"mount-at-app: expected >=3 absolute /_app/ refs in the shell, found {_n_app}")
_n_import = html.count('import("/_app/')
if _n_import != 2:
    sys.exit(f"mount-at-app: expected 2 inline import(\"/_app/...) calls, found {_n_import}")
html = html.replace("/_app/", f"{BASE}/_app/")

# Post-condition: no absolute asset ref escaped the /app prefix.
if "/_app/" in html.replace(f"{BASE}/_app/", ""):
    sys.exit("mount-at-app: an unprefixed /_app/ remains in the /app shell")
if f'import("{BASE}/_app/' not in html:
    sys.exit("mount-at-app: the inline import() calls were not prefixed")

# This is an ALLOWLIST rewrite (base, /_app/, /service-worker.js, the manifest) —
# never a blanket absolute-path prefixer. The API is CROSS-PREFIX by design
# (`feedUrl = window.location.origin + "/v1/api/opds"`), so an API path must never
# be rewritten. None appears in the generated shell today; if one ever does, fail
# loudly so this rewrite is revisited rather than silently mangling the API URL.
if "/v1/" in html:
    sys.exit("mount-at-app: an API ref appeared in the shell; the API is "
             "cross-prefix and must NOT be rewritten — revisit the allowlist")

# The CSP <meta> pins a sha256 of the inline bootstrap script, and we just
# rewrote that script (base + import paths), so its hash no longer matches and a
# browser would REFUSE to run the shell at /app. Recompute it. First reproduce
# the ORIGINAL hash from the ORIGINAL script and confirm it is the one in the
# CSP header — if we cannot, SvelteKit's CSP format changed and we fail loudly
# rather than ship a shell that silently won't boot.
_script = re.compile(r"<script>(.*?)</script>", re.S)


def _csp_hash(script_inner):
    return base64.b64encode(hashlib.sha256(script_inner.encode()).digest()).decode()


_orig = _script.search(open(src_path).read())
if not _orig:
    sys.exit("mount-at-app: no inline <script> found in the source shell")
_orig_hash = _csp_hash(_orig.group(1))
_new = _script.search(html)
if not _new:
    sys.exit("mount-at-app: no inline <script> found in the /app shell")
_new_hash = _csp_hash(_new.group(1))
html = swap(html, f"'sha256-{_orig_hash}'", f"'sha256-{_new_hash}'", count=1)

os.makedirs(out_dir, exist_ok=True)
open(out_path, "w").write(html)

# Guard constraint 1: the root shell must be untouched.
if open(src_path, "rb").read() != _root_before:
    sys.exit("mount-at-app: build/index.html changed; the root shell must stay "
             "byte-for-byte identical")

print(f"mount-at-app: wrote {out_path} (base={BASE}); root shell unchanged")
