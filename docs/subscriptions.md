# Members-only books with Ghost

Lenny can lend books, give them away, or keep them for the paying members of a [Ghost](https://ghost.org) site. A book has one of three access modes:

| Mode | Who can read it |
|---|---|
| `open` | Anyone. |
| `borrowable` | A patron with an active loan. One copy, one reader at a time. |
| `subscription` | A paying member of your Ghost site, while their pass is active. No loans and no limit on copies. |

Every mode is read in the browser with Thorium Web. Lenny never offers a download link. Members-only books are left out of the OPDS feed, its search and the public items list, so the way in is a Read link on your Ghost site or in your newsletter.

## How a member gets in

1. A member clicks **Read** in a post on your site or in a newsletter.
2. A small script on your site, the snippet, asks Ghost for the member's entitlement token: a signed, 5-minute proof of who they are and whether they pay. Meanwhile the page shows "Opening your book…" on your site's own background.
3. The snippet posts that token to Lenny. Lenny checks Ghost's signature, stores only a keyed hash of the email (the same `LENNY_SEED` hash used for loans), gives the member a 24-hour pass and opens the reader.

The member never signs in to Lenny. If they aren't signed in to Ghost in that browser, Ghost's sign-in window opens first, and the book opens when Ghost brings them back (within 30 minutes).

Any member Ghost marks as paid gets in, including comped and gift memberships. You can limit access to certain paid tiers.

### The 24-hour pass

Each Read click gives the member a pass for **24 hours**, and clicking Read again renews it while they still pay. If a member cancels, they can keep reading until the pass ends, unless webhooks are set up (below), which end it within seconds.

A pass opens members-only books and nothing else. It is not a Lenny login: it can't borrow, and it leaves any Lenny sign-in in the same browser alone.

## Set it up

You need a Ghost site with members turned on. Lenny never asks for a Ghost Admin API key.

1. **Connect your site.** In Lenny admin, open **Settings → Ghost** and enter the address members visit, for example `https://example.com`. Use your own domain if the site has one, not `*.ghost.io`. Lenny checks that it can read the site's public signing keys.
2. **Paste the snippet.** Copy the snippet Lenny shows. In Ghost Admin, open **Settings → Code injection** and paste it into **Site header**. Remove any older Lenny snippet from both Site header and Site footer.
3. **Add Read links.** A Read link is your site's address plus `#lenny-book=` and the book's Open Library edition ID, for example `https://example.com/#lenny-book=OL12345M`. Lenny's book sheet gives you both forms ready to copy. In a post, either:
   - add a **Button** card with that URL, or
   - add an **HTML** card with:
     ```html
     <a href="https://example.com/#lenny-book=OL12345M">Read</a>
     ```
   Both work on your site and in newsletters. Emails can't run scripts, so the link opens your site and the snippet takes it from there.
4. **Mark books as members-only.** Set the book's access mode to **subscription**. If it was borrowable, its active loans are returned when you save.
5. **Set up webhooks** (optional, recommended). See below.

**Check your setup.** Settings → Ghost shows:
- whether the current snippet is on your site (Lenny looks at your home page)
- whether an older copy is still there
- when a Read link last worked
- when the last webhook arrived

Connecting a different site starts these checks over. Lenny also drops the old site's passes and makes a new webhook secret, so paste the new secret into the new site's webhooks.

**Paste the snippet again whenever Lenny's address changes or you update Lenny.** An old copy keeps sending members' tokens to the old address. If someone later gets that address, they could use those tokens, and each token carries a member's email.

**Pause it.** Turning members-only reading off in Settings → Ghost stops new Read clicks and refuses reads on existing passes. The site, tiers, secret and passes are kept, and webhooks still arrive, so turning it back on picks up where it left off.

## Webhooks: end access when a member cancels

1. In Ghost Admin, open **Settings → Integrations → Add custom integration** and name it "Lenny".
2. Add two webhooks, both pointing at the **Webhook URL** shown in Lenny's Ghost settings, with the **Secret** shown there:
   - event **Member updated**
   - event **Member deleted**

A deleted member, a downgrade to free, a move off the allowed tiers, or an email change ends the pass. Lenny checks every delivery's `X-Ghost-Signature` and ignores unsigned or stale ones.

To change the secret, use **Rotate secret** in Lenny. The old secret stops working at once, so paste the new one into both webhooks in Ghost.

> **Ghost(Pro) Starter** doesn't send custom-integration webhooks. On that plan the 24-hour pass is the only limit.

## Members and stats

**Ghost members** in Lenny admin lists active passes: a short anonymous ID (Lenny never keeps the email), the tier, when the pass ends and when it was last renewed. **Sign out now** ends a pass at once. It doesn't change their Ghost membership: a member who still pays gets a new pass the next time they click Read. To stop someone for good, cancel or remove them in Ghost.

**Stats → Subscriptions** shows, for a date range:
- books read, readers and opens, compared with the period just before
- new readers and returning readers
- readers active in the last 15 minutes
- opens per day and per device type
- the most-read books

Lenny records only the keyed hash, the book, the day and a device type (desktop, mobile or tablet).

## Security

- Lenny accepts only Ghost's RS512-signed entitlement tokens for your site. Each token works once and must expire within 10 minutes; Ghost's expire after 5.
- Reading checks a pass stored in Lenny's own database. It never waits on Ghost or Open Library, so members keep reading if either is down.
- No other website can read members-only content through a member's browser.
- The member's pass cookie is sent on every request to Lenny, including from other sites. iPhone browsers otherwise leave it off a book's images and styles. Another site can't read anything with it, but it could tell whether a visitor holds a pass.
- What it can't stop: a paying member can save pages while their pass lasts. That comes with reading in a browser, without DRM. Any script running on your Ghost site can also get a signed-in member's token, which is how Ghost works, so only run scripts you trust there.

## Settings in `auth.env`

Lenny admin writes these, and you rarely need to touch them by hand.

| Key | Meaning |
|---|---|
| `LENNY_GHOST_URL` | Your Ghost site's address. Empty means not connected. |
| `LENNY_GHOST_TIERS` | Comma-separated Ghost tier IDs allowed to read. Empty means any paid member. |
| `LENNY_GHOST_WEBHOOK_SECRET` | The secret Ghost signs webhooks with. |
| `LENNY_GHOST_ENABLED` | `false` pauses members-only reading. Anything else, or unset, means on. |

## Troubleshooting

| What you see | Why, and the fix |
|---|---|
| Read opens an old Lenny address | An older snippet is still on the site, often in Site footer. Remove every old copy and keep the current one in Site header. Settings → Ghost warns about this. |
| An HTML card Read button does nothing in emails | It's a `<button>`, and emails don't run scripts. Use the link form above. |
| The homepage flashes before the book opens | The snippet is in Site footer. Move it to Site header. |
| "This book is for members" | No pass in this browser yet. Click Read on the publisher's site while signed in. |
| "Your reading pass has ended" | 24 hours have passed. Click Read again. |
| "Members-only reading is paused" | Someone turned it off in Settings → Ghost. |
| Free members can't read | That's intended. Only paid members get passes. |

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/api/subscriptions/ghost/launch` | Form fields `token`, `book`. Called by the snippet. |
| POST | `/v1/api/subscriptions/ghost/webhook` | Ghost member webhooks. |
| GET / PUT | `/v1/api/admin/settings/ghost` | `site_url`, `allowed_tier_ids`, `enabled`. GET also returns `tier_names`, `seen_tiers`, the snippet, webhook URL and secret, `snippet_installed`, `snippet_stale`, `last_launch_at` and `last_webhook_at`. |
| POST | `/v1/api/admin/settings/ghost/webhook-secret` | Rotate the secret. |
| GET | `/v1/api/admin/subscriptions` | Active passes, `?page&page_size`. |
| DELETE | `/v1/api/admin/subscriptions/{id}` | End one pass now. |
| GET | `/v1/api/admin/stats/subscriptions` | `?from=YYYY-MM-DD&to=YYYY-MM-DD` (default: last 30 days). Returns `totals`, `previous`, `readers {new, returning}`, `by_day` and `devices`. |
| GET | `/v1/api/admin/stats/subscriptions/books` | Adds `page`, `page_size` and `sort=opens\|patrons\|last_read`. |
| GET | `/v1/api/items/{id}/access` | 204 or 403. nginx asks it before opening the reader. |

Items accept and return `access_mode`. `encrypted` still works for older clients, and `encrypted: true` means `borrowable`. `PATCH /v1/api/admin/items/{id}` reports `loans_returned` when a change ends loans, and `GET /v1/api/admin/loans?item=<OLID>` counts a book's loans first.

Errors from these endpoints are `{detail, code}`.
