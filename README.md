<p align="center">
  <a href="https://lennyforlibraries.org/">
    <img width="160" src="https://github.com/user-attachments/assets/b7d70bf8-d795-419c-97b1-5cf4f9bad3f9" alt="Lenny logo"/>
  </a>
</p>

# Lenny

**A self-hosted, open source digital library for ebook lending.** Lenny lets a library keep its EPUB collection on its own server, lend each copy to one patron at a time (controlled digital lending), and publish the catalog as an OPDS feed. Patrons read in the browser through Readium and Thorium Web.

[![CI](https://github.com/ArchiveLabs/lenny/actions/workflows/ci.yml/badge.svg)](https://github.com/ArchiveLabs/lenny/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

[Website](https://lennyforlibraries.org/) · [Install](docs/install.md) · [Admin guide](docs/admin.md) · [Configuration](docs/configuration.md) · [API](docs/api.md) · [Issues](https://github.com/ArchiveLabs/lenny/issues)

## Why

Most libraries lend ebooks through a vendor's platform, on the vendor's terms. Lenny is the other option: the library runs the server, keeps the files and sets the loan rules. Lenny is made by [Archive Labs](https://github.com/ArchiveLabs) and is free software under the AGPL.

## What it does

- **Lending.** Each copy goes to one patron at a time. Loans return themselves after a set number of days. You set the per-patron loan limit and loan length in the admin UI.
- **Reading in the browser.** Patrons read in [Thorium Web](https://github.com/edrlab/thorium-web), served by your own node. Lenny never offers a download link.
- **Open access and lendable books side by side.** A book marked *encrypted* needs a loan; any other book can be read by anyone.
- **OPDS 2.0 catalog** at `/v1/api/opds`, with search and incremental harvesting, for reading apps and aggregators such as Open Library.
- **A catalog site** at `/` where patrons browse, sign in and borrow.
- **Admin UI** at `/admin`: books, bulk import, loans, staff accounts, lending and sign-in settings.
- **Patron sign-in** through Open Library one-time passwords, your own OIDC provider (Keycloak, Auth0, Okta and others), or Internet Archive keys.
- **OAuth 2.0 server**, so other apps (Open Library, reader.archive.org) can borrow on a patron's behalf.
- **Metadata from Open Library.** Every book is keyed to an Open Library edition.
- **One-command updates** with prebaked images for amd64 and arm64, and a database backup before each update.

## Quick start

You need Linux or macOS, Docker with Compose v2, `git`, `make` and `bc`. See [Install](docs/install.md) for details.

```sh
git clone https://github.com/ArchiveLabs/lenny.git
cd lenny
make start
```

`make start` writes the config files with fresh secrets, pulls the images and starts the stack. Then:

| Open | For |
|---|---|
| http://localhost:8080/ | the catalog patrons use |
| http://localhost:8080/admin | the admin UI. Sign in as `admin` with `ADMIN_PASSWORD` from `auth.env` |
| http://localhost:8080/v1/api/opds | the OPDS feed |
| http://localhost:8080/v1/api/docs | the API reference |

Add some public domain books from Standard Ebooks to try it out:

```sh
make preload items=10
```

Add your own EPUB, keyed to its Open Library edition:

```sh
make addbook olid=OL60638966M filepath=~/books/mybook.epub encrypted=true
```

To put the node on the internet, see [Public access and HTTPS](docs/install.md#public-access-and-https). To turn on lending, see [Lending modes](docs/admin.md#lending-modes).

Update later with:

```sh
make update
```

## Documentation

| Guide | Covers |
|---|---|
| [Install and operate](docs/install.md) | requirements, install, updates, backups, ARM, tunnels and HTTPS |
| [Configuration](docs/configuration.md) | every config file, environment variable and `make` target |
| [Admin guide](docs/admin.md) | staff accounts, adding books, lending modes, patron sign-in, the reader |
| [API](docs/api.md) | REST and OPDS endpoints, harvesting, health check |
| [Development](docs/development.md) | local builds, tests, migrations, the admin app, contributing |
| [Troubleshooting](docs/troubleshooting.md) | common failures and how to recover |
| [OAuth 2.0 app access](docs/OAUTH2.md) | letting other apps sign patrons in and borrow |
| [External OIDC sign-in](docs/OAUTH.md) | using your own identity provider for patrons |
| [Database migrations](docs/MIGRATIONS.md) | Alembic workflow |

## Getting help

- Bugs and questions: [GitHub issues](https://github.com/ArchiveLabs/lenny/issues).
- Chat: [request an invite to the Lenny Slack channel](https://forms.gle/b4HDcWVRhT3fvqcQ6).
- Email: mek@archive.org. Libraries interested in a pilot are welcome to write.
- Open Library holds a weekly [community call](https://github.com/internetarchive/openlibrary/wiki/Community-Call) that covers Lenny too.

## Contributing

Issues and pull requests are welcome. Start with [Development](docs/development.md). It covers the local setup, tests, and the `VERSION` bump CI asks for when a change ships to servers.

## Security

Please report vulnerabilities privately through [GitHub's vulnerability reporting](https://github.com/ArchiveLabs/lenny/security/advisories/new), not in a public issue.

## Credits

Lenny is built on:

- [Readium](https://readium.org/) for serving publications, and [Thorium Web](https://github.com/edrlab/thorium-web) by EDRLab as the reader
- [Garage](https://garagehq.deuxfleurs.fr/) by Deuxfleurs for object storage
- [Open Library](https://openlibrary.org/) and the [Internet Archive](https://archive.org/) for metadata, patron sign-in and lending accounts
- [Standard Ebooks](https://standardebooks.org/) for the sample collection
- [FastAPI](https://fastapi.tiangolo.com/), [PostgreSQL](https://www.postgresql.org/), [SQLAlchemy](https://www.sqlalchemy.org/), [Alembic](https://alembic.sqlalchemy.org/) and [nginx](https://nginx.org/)
- Archive Labs' own [opds-client](https://github.com/ArchiveLabs/opds-client), [lenny-app](https://github.com/ArchiveLabs/lenny-app) and [pyopds2](https://github.com/ArchiveLabs/pyopds2) libraries

## License

[GNU Affero General Public License v3.0](LICENSE). If you run a modified Lenny for others, the AGPL requires you to offer them your source.
