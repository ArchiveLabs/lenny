<!-- PROJECT LOGO -->
<p align="center">
  <a href="https://lennyforlibraries.org/">
    <img width="175" src="https://github.com/user-attachments/assets/b7d70bf8-d795-419c-97b1-5cf4f9bad3f9" alt="Lenny Logo"/>
  </a>
</p>

<h3 align="center">Lenny</h3>
<p align="center">
  The open source Library-in-a-Box to preserve and lend digital books.<br/>
  <a href="https://lennyforlibraries.org/"><strong>Learn more »</strong></a>
  <br/><br/>
  <a href="https://github.com/ArchiveLabs/lenny/issues">Issues</a>
  ·
  <a href="https://github.com/ArchiveLabs/lenny/pulls">Pull Requests</a>
  ·
  <a href="https://github.com/ArchiveLabs/lenny/blob/main/LICENSE">License</a>
</p>

<p align="center">
  <a href="https://github.com/ArchiveLabs/lenny/stargazers"><img src="https://img.shields.io/github/stars/ArchiveLabs/lenny?style=social" alt="Stars"></a>
  <a href="https://github.com/ArchiveLabs/lenny/network/members"><img src="https://img.shields.io/github/forks/ArchiveLabs/lenny?style=social" alt="Forks"></a>
  <a href="https://github.com/ArchiveLabs/lenny/issues"><img src="https://img.shields.io/github/issues/ArchiveLabs/lenny?color=blue" alt="Open Issues"></a>
  <a href="https://github.com/ArchiveLabs/lenny/pulls"><img src="https://img.shields.io/github/issues-pr/ArchiveLabs/lenny?color=purple" alt="Pull Requests"></a>
  <a href="https://github.com/ArchiveLabs/lenny/commits/main"><img src="https://img.shields.io/github/last-commit/ArchiveLabs/lenny/main" alt="Last Commit"></a>
  <a href="https://github.com/ArchiveLabs/lenny/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-AGPLv3-purple" alt="License"></a>
  <a href="https://deepwiki.com/ArchiveLabs/lenny"><img src="https://deepwiki.com/badge.svg" alt="Ask DeepWiki"></a>
</p>


## 📖 Table of Contents

- [About the Project](#about-the-project)
- [Features](#features)
- [OPDS 2.0 Feed](#opds-20-feed)
- [Internet Archive Bookserver app + Lenny OPDS sync](#bookserver-app-sync)
- [Technologies](#technologies)
- [Endpoints](#endpoints)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Open Library / Internet Archive Auth](#open-library--internet-archive-auth) — enable lending via Admin UI or CLI
- [Updating](#updating)
- [Database Migrations](#database-migrations)
- [Health Check](#health-check)
- [Testing Readium Server](#testing-readium-server)
- [Rebuilding](#rebuilding)
- [FAQs](#faqs)
- [Tests](#tests)
- [Project Structure](#project-structure)
- [Admin Dashboard](#admin-dashboard)
- [Contributing](#contributing)
- [Pilot](#pilot)
- [Open Topics](#open-topics)
- [Community & Support](#community--support)
- [License](#-license)

---

## About the Project

Lenny is a free, open source, Library-in-a-Box for libraries to preserve and lend digital books.

- 📚 Designed for libraries that want control over their digital collections.
- 🔐 Built with modern authentication, DRM, and flexible storage options.
- 🌍 Easy to self-host, customize, and scale for any library size.
- 🚀 Active development and open to contributions!

---

## 🔐 Authentication Modes

Lenny supports two authentication modes for lending:

1.  **OAuth Implicit (Default)**: Standard OPDS authentication flow. Clients like Thorium Reader use this to request a token via a popup/webview.
2.  **Direct Token**: A simpler, link-based authentication flow. Useful for environments where full OAuth support is tricky.
    *   **Browser-Friendly**: Users authenticate via an OTP (One-Time Password) email directly in the browser.
    *   **How to Enable**: This mode is **dynamic** and applies per-session.
    *   **Trigger**: Append `?auth_mode=direct` (or legacy `?beta=true` for backward compatibility) to any OPDS feed URL (e.g. `/v1/api/opds?auth_mode=direct`).
    *   **Sticky Session**: Once entered, the session remembers the mode, and all generated links (navigation, shelf, profile) will automatically keep you in that mode.

To switch back to OAuth mode, simply visit the root feed without the parameter (after clearing cookies/session if necessary).

---

## Features

- **Full Lending Workflow**: Borrow, return, and manage digital books.
- **API-first**: RESTful endpoints for integration and automation.
- **Containerized**: Simple Docker deployment and robust Makefile for scripts.
- **Book Importer**: Quickly load hundreds of test books for demos or pilots.
- **Readium Integration**: Secure, browser-based reading experience.
- **Flexible Storage**: S3, Internet Archive, or local file support.
- **Database-backed**: Uses PostgreSQL and SQLAlchemy.
- **Admin UI**: Secure admin dashboard served at `/admin`, isolated from public API access.
- **Encrypted/Unencrypted Item Filtering**: Filter catalog items by encryption status via API.

---
## OPDS 2.0 Feed
- Lenny is powered by [OPDS 2.0 Specs](https://opds.io).Lenny has its own OPDS 2.0 Package `pyopds2_lenny` more on [pyopds2_lenny](https://github.com/ArchiveLabs/pyopds2_lenny) repo.

---
## Technologies

- **Docker** for deployment and containerization  
- **nginx** as a reverse proxy  
- **FastAPI** (Python) as the web & API framework  
- **Minio** API for storing digital assets  
- **YAML** for configuring library-level rules  
- **PostgreSQL** for the database  
- **SQLAlchemy** as the Python ORM
- **Alembic** for database migrations
- **Readium LCP** for DRM  
- **Readium Web SDK** for a secure web reading experience  
- **OPDS** for syndicating holdings  

---

## Endpoints

- `/v{1}/api`
- `/v{1}/manage`
- `/v{1}/read`
- `/v{1}/opds`
- `/v{1}/stats`
- `/admin` — Admin UI (internal only, proxied to `lenny_admin:4000`)

---

## Getting Started

To install and run Lenny as a production application:

```sh
curl -fsSL https://raw.githubusercontent.com/ArchiveLabs/lenny/refs/heads/main/install.sh | sudo bash
```

---

## Development Setup

```sh
git clone https://github.com/ArchiveLabs/lenny.git
cd lenny
make all
```

- This will generate a `.env` file with reasonable defaults (if not present).
- Navigate to `localhost:8080` (or your `$LENNY_PORT`).
- Enter the API container with:
  `docker exec -it lenny_api bash`

### Dev vs Production Mode

Lenny defaults to **production mode** — uvicorn serves requests without watching for file changes. For development with hot-reload:

1. Set `LENNY_PRODUCTION=false` in your `.env`
2. Restart: `make redeploy`

Now any code change is picked up immediately by uvicorn. To switch back to production mode, set `LENNY_PRODUCTION=true` and run `make redeploy`.

---

## Bookserver app sync

Sync your Lenny OPDS feed with Archive.org's [Bookserver app](https://reader.archive.org). To have a personalized Lenny catalog with a great user interface.
> [!IMPORTANT]
> Bookserver app is Internet Archive's closed product, it doesn't come with lenny instance which you can own

```sh
make url 
```

- Gernerates URl ODPS link & Lenny server + Archive's Book server app sync URL Link.

---


---

## Admin Dashboard

Lenny includes a secure admin interface at `/admin` for managing the library.

### Setup

Change these variables in your `.env` or it will use system generated credentials:

```env
ADMIN_USERNAME=your-username
ADMIN_PASSWORD=your-secure-password
```

## Adding Books encrypted or unencrypted

To add a book to Lenny, you must provide an OpenLibrary Edition ID (OLID). Books without an OLID cannot be uploaded.

### Adding Books Metadata

Sign in to your Openlibrary.org account.

```link
https://openlibrary.org/books/add
```

navigate to the above link and add all the details.

### Usage using CLI 

```sh
make addbook olid=OL123456M filepath=/path/to/book.epub [encrypted=true]
```

### Examples

```sh
# Add an unencrypted book
make addbook olid=OL60638966M filepath=./books/mybook.epub

# Add an encrypted book
make addbook olid=OL60638966M filepath=./books/mybook.epub encrypted=true

# Using numeric OLID format (without OL prefix and M suffix)
make addbook olid=60638966 filepath=./books/mybook.epub
```

### Important Notes

- **File Location**: The EPUB file must be within the project directory (e.g., in `./books/` or project root)
- **OLID Formats**: Accepts both `OL123456M` and `123456` formats
- **Duplicates**: If a book with the same OLID already exists, the upload will fail with a conflict.

### Troubleshooting

If you get a "File not found" or permission error, make sure:
1. The file is copied into your lenny project directory.
2. You're using a relative path from the project root (e.g., `./books/mybook.epub`)

---

## Testing Readium Server

```sh
BOOK=$(echo -n "s3://bookshelf/32941311.epub" |  base64 | tr '/+' '_-' | tr -d '=')
echo "http://localhost:15080/$BOOK/manifest.json"
curl "http://localhost:15080/$BOOK/manifest.json"
```

---

## Open Library / Internet Archive Auth

Lenny must be connected to an [Internet Archive](https://archive.org) account to enable lending. You can do this two ways: through the **Admin UI** or the **CLI**.

### Option 1 — Admin UI (recommended)

Open the admin dashboard at `/admin`, sign in, and navigate to **Settings → Open Library**. Enter your Internet Archive email and password and click **Log in**. Lending is enabled immediately — no restart required.

To disconnect, click **Log out** on the same page. Lending is disabled immediately.

### Option 2 — CLI

```sh
# Log in (interactive — prompts for email and password)
make ol-login

# Log out — clears IA S3 keys from .env and disables lending
make ol-logout
```

**Scripted / non-interactive login** (e.g. CI):
```sh
OL_EMAIL=you@example.com LENNY_NONINTERACTIVE=1 make ol-login
```
> `LENNY_NONINTERACTIVE=1` suppresses all "are you sure?" confirmation prompts so the command can run unattended in scripts or CI pipelines.

> **Security:** avoid passing `OL_PASSWORD` as an environment variable in scripts — it will appear in shell history and `ps` output. Instead, let the interactive prompt handle the password, or pipe it via stdin using a secrets manager.

After logging in, lending is enabled automatically and the API container is restarted so the credentials take effect. After logging out, lending is disabled and the container restarts immediately.

---

## Updating

To update an existing Lenny installation to the latest version:

```sh
make update
```

This single command handles everything automatically:
- Pulls the latest code (`git pull --ff-only`)
- Syncs new environment variables (never overwrites your existing config)
- Pulls updated Docker images
- Backs up your database before rebuilding
- Rebuilds and restarts containers
- Applies database migrations automatically on startup

Your data (database, books, S3 storage) is preserved across updates. A database backup is saved to `backups/` before every update. If anything goes wrong, re-run `make update` — every step is idempotent.

### First-time upgrade (existing installations)

If your Lenny installation predates the update engine (no `make update` command yet), you need a one-time manual bootstrap:

```sh
git pull              # get the update engine code (one-time only)
make update           # from here, the engine takes over
```

After this, all future updates are just `make update` — it handles `git pull` and everything else for you.

> **Note:** Do not run `make configure` during an upgrade — it would overwrite your `.env` with new credentials. The update engine syncs new variables safely without touching your existing configuration.

For details on the update engine architecture, see [docs/plans/update-engine.md](docs/plans/update-engine.md).

---

## Database Migrations

Lenny uses [Alembic](https://alembic.sqlalchemy.org/) for database migrations. Migrations run automatically on container startup — no manual steps needed during normal use.

```sh
make migrate            # Run pending migrations
make migrate-status     # Show current migration state
make migration msg="add new table"  # Generate a new migration (developers only)
make migrate-rollback   # Rollback last migration (use with caution)
```

For full details, see [docs/MIGRATIONS.md](docs/MIGRATIONS.md).

---

## Health Check

Run diagnostics on your Lenny environment:

```sh
make doctor
```

Checks Docker, `.env` configuration, database connectivity, disk space, and version status.

---

## Rebuilding

```sh
# Rebuild API image and restart (preserves data)
make redeploy

# Full rebuild from scratch (WARNING: wipes database)
make rebuild
```

---

## FAQs

<details>
<summary><b>Everything is broken and I need to start from scratch</b></summary>

```sh
make tunnel rebuild start preload items=10 log
```
</details>

<details>
<summary><b>If I disconnect from the internet and tunnel stops working, what do I do?</b></summary>

```sh
make untunnel tunnel start
```
</details>

<details>
<summary><b>I am getting database connection problems</b></summary>

```sh
make resetdb restart preload items=5
```
</details>

<details>
<summary><b>I need to stop services (also kills the tunnel)</b></summary>

```sh
make stop 
```
</details>

<details>
<summary><b>The /v1/api/items/{id}/read endpoint redirects to Nginx default page</b></summary>

This happens when using `docker compose up -d` directly instead of `make start` or `make build`.

**Why it happens**: The Thorium Web reader requires `NEXT_PUBLIC_*` environment variables at build time. When running `docker compose up -d` directly, these variables may not be passed correctly.

**Solution**: Use the Makefile commands which properly source the environment:

```sh
# Fast build (uses cache)
make build

# Full rebuild (no cache)
make rebuild
```

Both commands source `reader.env` before building, ensuring the reader is configured correctly.
</details>

---

## Tests

All automated tests are in the `tests/` directory.

To run tests:

```sh
pytest
```

- Install dependencies:  
  `pip install -r requirements.txt`
- Test configs via `.env.test` if needed.

---

## Project Structure

```text
/
├── lenny/                # Core application code
│   ├── configs/          # App configuration (reads from .env)
│   ├── core/             # Database models, ORM, business logic
│   └── routes/           # API route definitions and docs
├── alembic/              # Database migration scripts
│   └── versions/         # Individual migration files
├── docker/               # Docker configuration
│   └── utils/            # Utility scripts (lenny.sh, update.sh, doctor.sh)
├── scripts/              # Utility scripts (e.g. preload.py)
├── tests/                # Automated tests
├── Makefile              # Make commands for setup/maintenance
├── install.sh            # Production install script
├── VERSION               # Current release version
├── .env                  # Environment variables (generated)
└── README.md             # Project documentation
```

---

## Contributing

There are many ways volunteers can contribute to the Lenny project, from development and design to data management and community engagement. 
Here’s how you can get involved:

### Developers
- Getting Started: Check out our [Development Setup](#development-setup) for instructions on how to set up your development environment, find issues to work on, and submit your contributions.
- Good First Issues: Browse our Good First Issues to find beginner-friendly tasks.

### Community Engagement
- Join our Community Calls: Open Library hosts weekly community [Zoom call for Open Library & Lenny](https://zoom.us/j/369477551#success) and design calls. Check the community call schedule for times and details.
- Ask Questions: If you have any questions, request an invitation to our Slack channel on our volunteers page.

### Lenny Slack Channel 
- If you are a Developer or an library instrested in contributing or trying lenny feel free to join our lenny slack channel from [Here](https://forms.gle/b4HDcWVRhT3fvqcQ6)

For more detailed information on community call. refer to Open Libraries page [Here](https://github.com/internetarchive/openlibrary/wiki/Community-Call)

---

## Pilot

We're seeking partnerships with libraries who would like to try lending digital resources to their patrons.

---

## Open Topics

- Authentication - How does your library perform authentication currently?

---

## Community & Support

- [GitHub Issues](https://github.com/ArchiveLabs/lenny/issues) — File bugs, request features, ask questions
- Email: mek@archive.org

---

## 📄 License

This project is licensed under the [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE).

---

<p align="center">
  <b>Empowering libraries to share digital knowledge.</b>
</p>
