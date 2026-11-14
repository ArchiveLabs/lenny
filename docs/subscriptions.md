# Members-only books with Ghost

A book can have one of three access modes:

| Mode | Who can read it |
|---|---|
| `open` | Anyone. |
| `borrowable` | A patron with an active loan. One copy, one reader at a time. |
| `subscription` | A paying member of your Ghost site, while their lease is active. No loans and no limit on copies. |

Every mode is read in the browser with Thorium Web. Lenny never offers a download link.

Members-only books are left out of the OPDS feed and search, so the only way in is the Read button on your Ghost site.

## How a member gets in

1. On your Ghost site, a signed-in member presses a **Read** button in a post.
2. A small script on your site asks Ghost for the member's entitlement token: a signed, 5-minute proof of who they are and whether they pay.
3. The script posts that token to Lenny. Lenny checks Ghost's signature, stores only a keyed hash of the email (the same `LENNY_SEED` hash used for loans), and opens the reader.

The member never signs in to Lenny. A member who isn't signed in to Ghost sees Ghost's sign-in window first.

Any member Ghost marks as paid gets in, and that includes comped and gift memberships. You can limit access to certain tiers.

## The 24-hour lease

Each Read press gives the member access for **24 hours**. Pressing Read again renews it while they still pay.

If a member cancels, they can keep reading for up to 24 hours unless webhooks are set up (below).

## Set it up

You need a Ghost site with members turned on. Lenny never asks for a Ghost Admin API key.

1. **Connect your site.** In Lenny admin, open **Settings → Ghost** and enter your site's address, for example `https://example.com`. Lenny checks that it can read the site's public signing keys.
2. **Add the script.** Copy the snippet Lenny shows. In Ghost Admin, open **Settings → Code injection** and paste it into **Site footer**.
3. **Add Read buttons.** In a post, add an HTML card with:
   ```html
   <button data-lenny-book="OL12345M">Read</button>
   ```
   Use the book's Open Library edition ID, as shown in Lenny's library.
4. **Mark books as members-only.** Set the book's access mode to **subscription**. To make it the default for new books, set **Default access mode**.

## Webhooks: end access when a member cancels

Without webhooks, a cancelled member keeps access until their lease ends (at most 24 hours). With webhooks, access ends within seconds.

1. In Ghost Admin, open **Settings → Integrations → Add custom integration** and name it "Lenny".
2. Add two webhooks, both pointing at the **Webhook URL** shown in Lenny's Ghost settings, with the **Secret** shown there:
   - event **Member updated**
   - event **Member deleted**
3. **Last webhook** in Lenny's settings shows the time of the most recent delivery.

Lenny checks every delivery's `X-Ghost-Signature` and ignores unsigned or stale ones.

To change the secret, use **Rotate secret** in Lenny. The old secret stops working at once, so paste the new one into both webhooks in Ghost.

> **Ghost(Pro) Starter** doesn't send custom-integration webhooks. On that plan the 24-hour lease is the only limit.

## Stats

**Stats → Subscriptions** in Lenny admin shows:

- books read
- distinct readers
- readers active in the last 15 minutes
- opens per day and per device type

Lenny records only the keyed hash, the book, the day and a device type (desktop, mobile or tablet). It never records the email.

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/api/subscriptions/ghost/launch` | Form fields `token`, `book`. Called by the snippet. |
| POST | `/v1/api/subscriptions/ghost/webhook` | Ghost member webhooks. |
| GET / PUT | `/v1/api/admin/settings/ghost` | `site_url`, `allowed_tier_ids`, `default_access_mode`. |
| POST | `/v1/api/admin/settings/ghost/webhook-secret` | Rotate the secret. |
| GET | `/v1/api/admin/stats/subscriptions` | `?from=YYYY-MM-DD&to=YYYY-MM-DD` (default: last 30 days). |
| GET | `/v1/api/admin/stats/subscriptions/books` | Adds `page`, `page_size` and `sort=opens\|patrons\|last_read`. |

Items accept and return `access_mode`. `encrypted` still works for older clients, and `encrypted: true` means `borrowable`.
