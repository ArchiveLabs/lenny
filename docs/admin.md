# Admin guide

The admin UI is at `/admin` on your node, e.g. http://localhost:8080/admin. It is a separate app, [lenny-app](https://github.com/ArchiveLabs/lenny-app), served by Lenny's nginx. The API routes it uses (`/v1/api/admin/*`) are blocked from outside; only the admin container can call them.

- [Staff accounts](#staff-accounts)
- [Adding books](#adding-books)
- [Lending modes](#lending-modes)
- [Patron sign-in](#patron-sign-in)
- [Loans](#loans)
- [Apps that act for patrons](#apps-that-act-for-patrons)
- [The catalog and the reader](#the-catalog-and-the-reader)

## Staff accounts

1. Sign in as `admin` with `ADMIN_PASSWORD` from `auth.env`. On first start Lenny stores that account in its database, hashed, as the **super user**. From then on the database is the only source: editing `auth.env` does not change the password.
2. Under **My account**, set your name and email and change the password. You can then sign in with your email or your username.
3. Under **Team**, add people (name, email, password). Each gets a username derived from the email.

There is exactly one super user. It manages the team and instance settings. **Make super user** hands the role to someone else (you confirm with your password) and you become a normal member. The super user cannot be demoted, disabled or deleted, only replaced. It can disable other accounts (reversible) or delete them (confirmed with its password).

From the shell:

```sh
make admin-create NAME="Ada Lovelace" EMAIL=ada@example.org   # super user if none exists, else a member
make admin-passwd USERNAME=admin                              # set a new password
```

If `auth.env` had no `ADMIN_PASSWORD` on first start, no account exists yet. Run `make admin-create` once.

## Adding books

Every book is tied to an [Open Library](https://openlibrary.org/) edition ID (OLID, e.g. `OL60638966M`). Lenny takes its title, author and cover from that record. If the edition is missing, [add it to Open Library](https://openlibrary.org/books/add) first. Files must be EPUB, at most 100 MB each.

Ways to add books:

| How | Use for |
|---|---|
| Admin UI upload | one book at a time |
| Admin UI bulk import | many files at once. Lenny matches each row to an Open Library edition; you review the matches, then import. |
| Admin UI or `make preload items=N` | sample public domain books from Standard Ebooks |
| `make briet-redeem code=…` | a book bundle bought on the BRIET marketplace |
| `make addbook olid=… filepath=… [encrypted=true]` | scripts |

```sh
make addbook olid=OL60638966M filepath=~/books/mybook.epub                  # open access
make addbook olid=OL60638966M filepath=~/books/mybook.epub encrypted=true   # needs a loan
make addbook olid=60638966 filepath=~/books/mybook.epub                     # numeric OLID also works
```

The file can be anywhere the shell user can read; it is copied into the container. Keep book files outside the Lenny checkout, or `make update` will refuse to run (it needs a clean git tree). If the edition is already in the library, `make addbook` skips it but still prints "uploaded successfully".

To remove books: the admin UI, or `make delete-book olid="OL51008637M 37044623"`.

### Open access and lendable books

The **encrypted** flag decides how a book is read:

| Flag | Patron needs | Copies |
|---|---|---|
| off | nothing; anyone can read it | unlimited |
| on | to sign in and borrow it | one loan at a time per copy |

Both kinds are read only in the browser reader. Lenny does not offer a file download in any mode. You can change the flag of a book in the admin UI later.

## Lending modes

The lending mode decides how patrons sign in to borrow. Set it in the admin UI, or in `ol.env` as `LENNY_LENDING_MODE`.

| Mode | Patrons sign in with | Needs |
|---|---|---|
| `none` (default) | nobody can borrow | nothing. Open access books are still readable. |
| `ol` | a one-time password emailed by Open Library | an Internet Archive account connected to Lenny (below) |
| `external` | your own OIDC provider | provider settings; see [OAUTH.md](OAUTH.md) |

### Connecting an Internet Archive account (mode `ol`)

In the admin UI: **Settings → Open Library**, enter the Internet Archive email and password, **Log in**. Lending starts at once. **Log out** on the same page stops it.

From the shell:

```sh
make ol-login     # prompts for email and password, writes keys to ol.env, sets mode ol, restarts the API
make ol-logout    # clears the keys, sets mode none, restarts the API
```

For scripts: `OL_EMAIL=you@example.org LENNY_NONINTERACTIVE=1 make ol-login`. The password is still prompted for on a terminal. Without a terminal it must come from `OL_PASSWORD`, which then shows up in shell history and `ps`.

## Patron sign-in

| Method | How to turn it on |
|---|---|
| Open Library one-time password | lending mode `ol` |
| Your OIDC provider (Keycloak, Auth0, Okta, Google, Clerk, ...) | **Settings → Auth** in the admin UI, or `auth.env`; lending mode becomes `external`. Guide: [OAUTH.md](OAUTH.md). |
| Internet Archive S3 keys (`Authorization: LOW <access>:<secret>`) | `IA_AUTH_ENABLED=true` in `auth.env`, or the toggle in the admin UI. Independent of the lending mode. For IA-aware clients and automation. |
| Direct token link | append `?auth_mode=direct` to an OPDS URL, for clients that cannot run the OPDS sign-in popup. |

## Loans

The admin UI lists active loans, can lend a book to a patron, and can return a loan early. Loan policy is under loan settings and is stored in `loan.env`:

- **Loan limit**: most books one patron may hold at once (default 10).
- **Loan length**: days until a loan returns itself (default 7; 0 means never). A single book can have its own length.

Changes apply to all API workers at once, without a restart.

## Apps that act for patrons

Lenny is also an OAuth 2.0 server. Apps such as Open Library or reader.archive.org can sign your patrons in and borrow for them, if you allow it. The reader.archive.org app is registered on every node automatically. Manage apps in **Settings → App Access** or with the `make oauth2-*` and `make ol-connect` targets.

This is separate from patron sign-in above: that is how Lenny identifies a patron; this is which apps may use Lenny. Full guide: [OAUTH2.md](OAUTH2.md).

## The catalog and the reader

**Catalog.** The node root (`/`) serves [opds-client](https://github.com/ArchiveLabs/opds-client), locked to this node's own feed at `/v1/api/opds`. Feeds from other sites are refused. Sign-in only works from the node's configured public address (`LENNY_PROXY`, else host and port).

**Reader.** Borrowing a book opens it in [Thorium Web](https://github.com/edrlab/thorium-web) at `/read`, on your node. The admin library page has an **Open in Lenny reader** button. A book's **Share** button copies a link of the form `<your address>/?opds=<book feed>`. Signing out ends the session on the server too.

Reading apps such as Thorium Reader or KOReader can use the OPDS feed directly: `/v1/api/opds` for the catalog, `/v1/api/opds/<id>` for one book.

The reader only opens books from the hosts in `MANIFEST_ALLOWED_DOMAINS` in `reader.env`. If you serve Lenny under your own domain, add it there and run `make start`. `make tunnel` adds the tunnel host for you.
