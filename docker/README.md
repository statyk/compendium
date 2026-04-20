# Compendium — Docker deployment

Runs Compendium as three containers: the app, a PostgreSQL database, and an
nginx reverse proxy that terminates HTTPS. Only the nginx container is exposed
to the host; the REST API is reachable only inside the Docker network.

## Layout

```
docker/
├── Dockerfile              # app image (multi-stage, Python 3.11 + Postgres driver)
├── docker-compose.yml      # db + compendium + nginx
├── .env.example            # copy to .env and edit before first run
├── certs/                  # (optional) drop fullchain.pem + privkey.pem here
├── nginx/
│   ├── nginx.conf          # TLS + /ui/* reverse proxy; everything else → /ui/catalog
│   └── entrypoint.sh       # generates a self-signed cert on first start
└── compendium/
    └── entrypoint.sh       # migrates DB + bootstraps admin user + starts serve
```

## Quick start

```bash
cd docker
cp .env.example .env
$EDITOR .env                       # change POSTGRES_PASSWORD, JWT secret, admin password

docker compose up -d --build
```

Browse to `https://<host>/` — the nginx server redirects plain HTTP to HTTPS and
any non-`/ui/` path to `/ui/catalog`. Log in with the `COMPENDIUM_ADMIN_USERNAME` /
`COMPENDIUM_ADMIN_PASSWORD` you set in `.env`.

## TLS

By default the nginx container generates a self-signed certificate on first
start and stores it in the host-mounted `./certs/` directory. Browsers will
warn on first visit; that is expected for a self-signed cert.

To use your own certificate (Let's Encrypt, an internal CA, etc.) place the
PEM-encoded files at:

```
docker/certs/fullchain.pem
docker/certs/privkey.pem
```

…before running `docker compose up`. If both files are present the nginx
entrypoint skips self-signing. To force a refresh of the self-signed cert,
delete the two files and restart the nginx container.

For automated Let's Encrypt renewal, front Compendium with Caddy or
Traefik instead of this bundled nginx; see `docs/deployment.md` in the
project root.

## Admin bootstrap

The `compendium` container's entrypoint runs `compendium db init` on every
start. On the first start only (when the username does not yet exist in the
database), it creates a Librarian account using `COMPENDIUM_ADMIN_USERNAME` and
`COMPENDIUM_ADMIN_PASSWORD` from `.env`.

**Changing the password post-bootstrap:** editing `.env` has no effect once the
user exists. Run:

```bash
docker compose exec compendium compendium user set-password --username admin
```

(A password-change flow in the web UI is planned; see the top-level CLAUDE.md
"High-priority follow-ups" section.)

## Persistence

- `db_data` (Docker named volume) — PostgreSQL data.
- `./certs/` (host bind mount) — TLS material.

To back up the database:

```bash
docker compose exec db pg_dump -U compendium compendium > backup.sql
```

To wipe everything and start over:

```bash
docker compose down -v     # -v deletes the db_data volume
rm -rf certs/*             # if you also want a fresh self-signed cert
```

## What's NOT exposed

- The REST API (`/auth`, `/works/search`, `/items`, `/patrons`, …) is only
  reachable from inside the Docker network. nginx returns `302 /ui/catalog`
  for any request that does not start with `/ui/`.
- The FastAPI interactive docs at `/docs` are likewise unreachable from
  outside the container network.
- PostgreSQL is not published to the host. To connect a GUI tool, temporarily
  add a `ports: ["5432:5432"]` entry to the `db` service in `docker-compose.yml`.

## Host ports

The defaults bind `0.0.0.0:80` and `0.0.0.0:443`. If you run Docker rootless
(or something else already uses those ports), change `HTTP_PORT` and
`HTTPS_PORT` in `.env` to non-privileged values (e.g. 8080 / 8443).

## Running the CLI against the running deployment

Any `compendium` command can be invoked inside the container:

```bash
docker compose exec compendium compendium patron list
docker compose exec compendium compendium audit list --limit 20
docker compose exec compendium compendium maintenance expire-holds
```

For scheduled maintenance, run the `expire-holds` command from a host cron
job that invokes `docker compose exec`.
