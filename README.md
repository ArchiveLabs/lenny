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
</p>


## 📖 Table of Contents

- [About the Project](#about-the-project)
- [Features](#features)
- [OPDS 2.0 Feed](#opds-20-feed)
- [Technologies](#technologies)
- [Endpoints](#endpoints)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Importing Test Books](#importing-test-books)
- [Testing Readium Server](#testing-readium-server)
- [Rebuilding](#rebuilding)
- [FAQs](#faqs)
- [Tests](#tests)
- [Project Structure](#project-structure)
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

## Features

- **Full Lending Workflow**: Borrow, return, and manage digital books.
- **API-first**: RESTful endpoints for integration and automation.
- **Containerized**: Simple Docker deployment and robust Makefile for scripts.
- **Book Importer**: Quickly load hundreds of test books for demos or pilots.
- **Readium Integration**: Secure, browser-based reading experience.
- **Flexible Storage**: S3, Internet Archive, or local file support.
- **Database-backed**: Uses PostgreSQL and SQLAlchemy.

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

---

## Getting Started

To install and run Lenny as a production application:

```sh
curl -fsSL https://raw.githubusercontent.com/ArchiveLabs/lenny/refs/heads/main/install.sh | sudo sh
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

---

## Importing Test Books

```sh
docker exec -it lenny_api python scripts/load_open_books.py 
```

## Making a Book Borrowable

If you'd like to change the encrypted state of an existing book (e.g. with `id=1`) to test borrowing, run the following:

First, exec into the api docker container and start a python session:
```sh
docker compose exec -it api python
```

Second, fetch the db item and set its `encrypted` state to:
```python
from lenny.core.models import Item, db
i = db.query(Item).filter(Item.id == 1).first()
i.encrypted = True
db.add(i)
db.commit()
```

---

## Testing Readium Server

```sh
BOOK=$(echo -n "s3://bookshelf/32941311.epub" |  base64 | tr '/+' '_-' | tr -d '=')
echo "http://localhost:15080/$BOOK/manifest.json"
curl "http://localhost:15080/$BOOK/manifest.json"
```

---

## Rebuilding

```sh
docker compose -p lenny down
docker compose -p lenny up -d --build
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
│   └── routes/           # API route definitions and docs
├── scripts/              # Utility scripts (e.g. load_open_books.py)
├── tests/                # Automated tests
├── docker/               # Docker configuration
├── Makefile              # Make commands for setup/maintenance
├── install.sh            # Production install script
├── .env                  # Environment variables (generated)
└── README.md             # Project documentation
```

---

## Contributing

Contributions are welcome! Please open an issue or pull request if you have suggestions or improvements.

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
