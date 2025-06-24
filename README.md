# Lenny

[Lenny](https://lennyforlibraries.org/) is a free, open source, Library-in-a-Box for libraries to preserve and lend digital books.

<img width="175" src="https://github.com/user-attachments/assets/b7d70bf8-d795-419c-97b1-5cf4f9bad3f9">

## Technologies

* [`docker`](https://www.docker.com/) for deployment and containerization
* [`nginx`](https://nginx.org/) handles patron requests (reverse proxy to app)
* [`FastAPI`](https://fastapi.tiangolo.com/) (python) as the web & API microframework
* [`Minio`](https://min.io/docs/minio/linux/developers/minio-drivers.html#python-sdk) API for storing digital assets (Amazon, Internet Archive, or local flat-file)
* [`yaml`](https://en.wikipedia.org/wiki/YAML/) for configuring library-level rules
* [`postgres`](https://www.postgresql.org/) for the database
* [`SQLAlchemy`](https://www.sqlalchemy.org/) for the database python [`ORM`](https://en.wikipedia.org/wiki/Object%E2%80%93relational_mapping)
* [Readium `LCP`](https://readium.org/lcp-specs/) for [DRM](https://en.wikipedia.org/wiki/Digital_rights_management); see [LCP Server](https://github.com/readium/readium-lcp-server)
* [Readium Web SDK](https://www.edrlab.org/software/readium-web/) for a secure web reading experience
* [`OPDS`](https://en.wikipedia.org/wiki/Open_Publication_Distribution_System) RSS-like standard for syndicating holdings

## Endpoints

* `/v{1}/api`
* `/v{1}/manage`
* `/v{1}/read`
* `/v{1}/opds`
* `/v{1}/stats`

## Installation

To install and run Lenny as a production application:

```
curl -fsSL https://raw.githubusercontent.com/ArchiveLabs/lenny/refs/heads/main/install.sh | sudo sh
```

## Development Setup

If you have `git` and `docker` installed on your system, the following commands will set up Lenny:

```
git clone git@github.com:ArchiveLabs/lenny.git
cd lenny
./run.sh
```

This process will run `docker/configure.sh` & `docker/lcpserverconfig.sh` to generate a gitignored `.env` & `lcpserver.yaml` file with reasonable default values, if not present.

Navigate to localhost:8080 or whatever `$LENNY_PORT` is specified in your `.env`

You may enter the API container via:

```
docker exec -it lenny_api bash
```

### Importing Test Books

```
# Run the importer: you can Ctrl+c after a few books are loaded (will load ~800)
docker exec -it lenny_api python scripts/load_open_books.py 
```

### Testing Readium Server

```
# Load a manifest URL
BOOK=$(echo -n "s3://bookshelf/32941311.epub" |  base64 | tr '/+' '_-' | tr -d '=')
# Should be http://localhost:15080/czM6Ly9ib29rc2hlbGYvMzI5NDEzMTEuZXB1Yg/manifest.json
echo "http://localhost:15080/$BOOK/manifest.json"
curl "http://localhost:15080/$BOOK/manifest.json"
```

### Rebuilding

```
docker compose -p lenny down
docker compose -p lenny up -d --build
```

## Pilot

We're seeking partnerships with libraries who would like to try lending digital resources to their patrons. 

## Open Topics

* Authentication - How does your library perform authentication currently?
