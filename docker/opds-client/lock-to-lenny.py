"""Patch opds-client at build time so it only ever talks to this Lenny node.

Every feed/publication fetch goes through createFetchUrl; an off-origin target
(e.g. a hand-edited ?opds=https://other.example/opds) falls back to Lenny's own
feed, and sign-in redirects must stay on-origin. Off-origin rel="alternate" publications are skipped, not fetched.
"""
import sys

path = "src/lib/stores/opdsStore.ts"
src = open(path).read()

def swap(old, new):
    global src
    assert src.count(old) == 1, f"patch anchor not found exactly once: {old!r}"
    src = src.replace(old, new)

swap('export const feedUrl = "https://openlibrary.org/opds";',
     'export const feedUrl = (typeof window !== "undefined" ? window.location.origin : "http://localhost") + "/v1/api/opds";')
swap("""      return new URL(target, feedUrl).href;""",
     """      // A shared link pasted with the title appended ("…/32930303 A Connecticut…") must still resolve.
      const resolved = new URL(target.trim().split(/\\s/)[0], feedUrl);
      return resolved.origin === new URL(feedUrl).origin ? resolved.href : feedUrl;""")
swap("""        if (fetchedLinks.has(link.href)) continue;""",
     """        if (fetchedLinks.has(link.href)) continue;
        try {
          if (new URL(link.href, feedUrl).origin !== new URL(feedUrl).origin) continue;
        } catch {
          continue;
        }""")
open(path, "w").write(src)

# Sign-in only ever redirects within this node. Lenny advertises relative
# authenticate hrefs, so resolve against our origin and refuse anything else.
path = "src/lib/auth.ts"
src = open(path).read()
swap("  const parsed = new URL(authenticateHref);",
     """  const parsed = new URL(authenticateHref, window.location.origin);
  if (parsed.origin !== window.location.origin) throw new Error("Refusing off-node sign-in");""")
open(path, "w").write(src)

# Lenny publishes Authorization Code + PKCE; the client only knows archive.org's
# client ids. Our own origin signs in as the node's built-in `lenny-opds-client`
# (registered by lenny.core.oauth2.ensure_default_clients).
src = open("src/lib/auth.ts").read()
swap("  const clientId = PKCE_CLIENT_IDS[window.location.origin];",
     '  const clientId = PKCE_CLIENT_IDS[window.location.origin] ?? "lenny-opds-client";')
open("src/lib/auth.ts", "w").write(src)

# The share sheet appends `text` to `url` in many apps, which corrupts the link
# (the title ends up inside the URL). Share the title and url only.
path = "src/lib/components/views/Publication.svelte"
src = open(path).read()
swap("                        text: `${metadata.title}`,\n", "")
open(path, "w").write(src)

# Lenny returns the implicit-flow token in the URL fragment (`#access_token=...&state=...`, per the
# OAuth spec); the client only reads the query string, so the token was dropped and the borrow that
# triggered sign-in was never retried. Read the fragment as well.
path = "src/lib/auth.ts"
src = open(path).read()
swap('''const initialSearch =
  typeof window !== "undefined" ? window.location.search : "";''',
     '''const initialSearch =
  typeof window !== "undefined"
    ? window.location.search || (window.location.hash.startsWith("#") && window.location.hash.includes("=") ? "?" + window.location.hash.slice(1) : "")
    : "";''')
open(path, "w").write(src)

# Sign out only deleted the client's stored token. Lenny also signs the patron in with a `session`
# cookie, which stayed, so the next request was still authenticated. Clear the cookie on the server too.
path = "src/lib/stores/opdsStore.ts"
src = open(path).read()
swap("""    signOut() {
      const origin = this.getOrigin();
      deleteToken(origin);""",
     """    async signOut() {
      const origin = this.getOrigin();
      deleteToken(origin);
      try {
        await fetch(new URL("/v1/api/logout", feedUrl).href, { method: "POST", credentials: "same-origin" });
      } catch {
        // offline: the stored token is already gone; the cookie expires on its own
      }""")
open(path, "w").write(src)

# Mount-awareness. The catalog is normally served at the origin root "/", but a
# host whose "/" is owned by another site can serve it at /app (the opds image
# emits a second shell with SvelteKit base="/app" — see mount-at-app.py). Two
# hand-authored ABSOLUTE refs would otherwise escape the mount and hit that other
# site: the sign-in-failed "Go home" link and the install-page icon. Resolve both
# against SvelteKit's base ($app/paths), which is "" at / and "/app" at /app.
path = "src/routes/+page.svelte"
src = open(path).read()
swap('import { handleImplicitFlow, handlePkceFlow, handleOAuthError } from "$lib/auth.js";',
     'import { handleImplicitFlow, handlePkceFlow, handleOAuthError } from "$lib/auth.js";\n        import { base } from "$app/paths";')
swap('href="/"', 'href="{base}/"')
open(path, "w").write(src)

path = "src/lib/components/views/InstallPage.svelte"
src = open(path).read()
swap('import { pwa } from "$lib/stores/pwaStore";',
     'import { pwa } from "$lib/stores/pwaStore";\n  import { base } from "$app/paths";')
swap('src="/public/512.png"', 'src="{base}/public/512.png"')
open(path, "w").write(src)

# Borrowing cannot COMPLETE at a non-root mount, and must say so rather than
# stranding the patron. All three OAuth redirect_uri sites in src/lib/auth.ts
# send the bare `window.location.origin` (no path), and the server pins
# lenny-opds-client's redirect list to exactly that origin by exact-string
# membership, so sign-in returns the browser to "/" with the token in a
# fragment. On the host this mount exists for, "/" is a different site
# altogether. Until the redirect is made mount-aware (ArchiveLabs/lenny#272),
# replace the sign-in control at a non-root mount with an explanation. Browsing
# is unaffected; `base` is "" at / so a default install sees no change at all.
path = "src/lib/components/views/Auth.svelte"
src = open(path).read()
swap('import { opds } from "$lib/stores/opdsStore.js";',
     'import { opds } from "$lib/stores/opdsStore.js";\n\timport { base } from "$app/paths";')
swap("""\t\t{#if pkceEntry}""",
     """\t\t{#if base}
\t\t\t<p role="alert" class="error-text">
\t\t\t\tSigning in isn't available at this address yet, so books can't be
\t\t\t\tborrowed here. You can still browse the whole catalog.
\t\t\t</p>
\t\t{:else if pkceEntry}""")
open(path, "w").write(src)
