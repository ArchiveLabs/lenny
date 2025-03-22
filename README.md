# Lenny

Lenny is a free, open source Lending System for Libraries.

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

First, copy the file `lenny_TEMPLATE.env` to `lenny.env` (gitignored) and edit it to have the correct values (such as desired psql credentials).

Second, run docker compose:

```
docker compose -p lenny up -d --build
```

Finally, navigate to localhost:8080 or whatever `$LENNY_PORT` you specified in your `lenny.env`

## Pilot

We're seeking partnerships with libraries who would like to try lending digital resources to their patrons. 

## Open Topics

* Authentication - How does your library perform authentication currently?
