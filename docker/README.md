# Compendium — Docker deployment

Runs Compendium as three containers: the app, a PostgreSQL database, and an
nginx reverse proxy that terminates HTTPS. Only the nginx container is exposed
to the host; the REST API is reachable only inside the Docker network.

## Layout

```
docker/
├── Dockerfile              # app image (multi-stage, Python 3.11 + Postgres driver; used by build override)
├── docker-compose.yml      # db + compendium + nginx (pulls published image by default)
├── docker-compose.build.yml  # build-from-source override (see "Build from source" below)
├── .env.example            # copy to .env and edit before first run
├── crontab.sample          # scheduled maintenance lines for host cron
├── install-cron.sh         # one-shot installer for the above (--log-file flag)
├── backups/                # nightly backups land here (created on first cron tick)
├── logs/                   # maintenance.log (default destination; install-cron.sh creates)
├── certs/                  # (optional) drop fullchain.pem + privkey.pem here
├── nginx/
│   ├── nginx.conf          # TLS + /ui/* reverse proxy; everything else → /ui/catalog
│   └── entrypoint.sh       # generates a self-signed cert on first start
└── compendium/
    └── entrypoint.sh       # migrates DB + bootstraps admin user + starts serve
```

## Quick start

Easiest — scaffold the bundle with generated secrets, then start it:

```bash
curl -fsSL https://raw.githubusercontent.com/statyk/compendium/master/install.sh | sh
# or, if you already have the package installed:
compendium init ./compendium && cd compendium && docker compose up -d
```

`compendium init` writes this same `docker-compose.yml`, the nginx config, the
cron helpers, and a `.env` with a freshly generated JWT key, encryption key, and
admin/DB passwords (the admin password is printed once). Use `--cert-cn`,
`--image`, or `--tls-cert/--tls-key` to customise; `--help` lists all flags.

To assemble it by hand instead, copy `.env.example` to `.env` and edit it:

```bash
cd docker
cp .env.example .env
$EDITOR .env                       # change POSTGRES_PASSWORD, JWT secret (required), admin password
                                   # optionally add COMPENDIUM_SECRET_KEY for encrypted-secrets UI

docker compose pull                # pull the published image from ghcr.io/statyk/compendium
docker compose up -d
```

The compose stack uses the published image from GHCR by default
(`ghcr.io/statyk/compendium:latest`). To pin a specific version, set
`COMPENDIUM_IMAGE=ghcr.io/statyk/compendium:1.0.2` in `.env`.

### Build from source

If you want to build the image locally (e.g. for local development or an
air-gapped environment), use the build override:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
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
database), it creates an Administrator account using `COMPENDIUM_ADMIN_USERNAME`
and `COMPENDIUM_ADMIN_PASSWORD` from `.env`.

**Changing the password post-bootstrap:** editing `.env` has no effect once the
user exists. Run:

```bash
docker compose exec compendium compendium user set-password --username admin
```

(A password-change flow in the web UI is a planned follow-up.)

## Persistence

- `db_data` (Docker named volume) — PostgreSQL data.
- `cover_cache` (Docker named volume) — proxy cache of cover images.
- `./certs/` (host bind mount) — TLS material.
- `./backups/` (host bind mount) — application backups (see below).

To wipe everything and start over:

```bash
docker compose down -v     # -v deletes the db_data + cover_cache volumes
rm -rf certs/*             # if you also want a fresh self-signed cert
rm -rf backups/*           # if you also want to discard backups
```

## Backups

Compendium ships its own portable backup format — gzipped tarballs with
JSONL-encoded rows that restore cleanly into either SQLite or PostgreSQL.
Use it instead of `pg_dump` so a backup taken today isn't tied to the
PostgreSQL version that produced it.

The nightly cron job (see "Scheduled maintenance" below) writes one tarball
per day to `./backups/` on the host, mounted into the container at
`/var/backups/compendium`. Filenames are `YYYY-MM-DD.tar.gz`.

To take an ad-hoc backup:

```bash
docker compose exec -T compendium compendium backup -o - > backup.tar.gz
```

To restore (replaces the running database):

```bash
docker compose exec -T compendium compendium restore --force - < backup.tar.gz
```

The restore process auto-migrates the target DB to the backup's schema
revision, restores rows, then re-migrates to the current head — so a
backup from an older Compendium version restores cleanly.

For off-machine archival, point your existing tool (rsync, borg, restic,
your provider's snapshot service) at `./backups/`.

## Scheduled maintenance

Compendium runs a number of cron-driven maintenance commands — most
importantly **the email outbox drain**, without which queued
notifications never get sent. The schedule lives in the host's crontab and
invokes the CLI inside the running container via `docker compose exec`.

### One-shot setup

```bash
docker/install-cron.sh
```

This appends `docker/crontab.sample` to the current user's crontab,
substituting the absolute path to `docker/`. The user must be in the
`docker` group (or otherwise able to run `docker compose` without
`sudo`). Re-running the script is a no-op — it refuses to install twice.

By default the script writes maintenance output to
`docker/logs/maintenance.log` (auto-created). Override with `--log-file`:

```bash
docker/install-cron.sh --log-file journal
docker/install-cron.sh --log-file /var/log/compendium/maintenance.log
```

`--log-file journal` routes output to the systemd journal (view with
`journalctl -t compendium-maintenance -f`). For paths the installer can't
create unprivileged (e.g. `/var/log/...`), it prints the one-time `sudo`
command needed to create the directory and exits without modifying the
crontab.

### What's scheduled

Cadences taken from `docker/crontab.sample`:

| Cadence              | Command                                  | Why |
|----------------------|------------------------------------------|-----|
| every 5 min          | `send-queued-notifications`              | drains the email outbox |
| daily 02:00          | `expire-holds`                           | cancels holds whose pickup window passed |
| daily 02:05          | `resume-expired-suspends`                | unfreezes patron-suspended holds whose date passed |
| daily 02:10          | `deactivate-expired-patrons`             | flips `is_active=false` on expired cards |
| daily 02:15          | `compendium backup`                      | nightly tarball to `./backups/` |
| daily 02:20          | `find ... -mtime +30 -delete`            | prunes backups older than 30 days |
| daily 02:30          | `assess-overdue-fines`                   | materializes overdue fines for reports |
| daily 08:00 / 08:15  | `queue-due-soon-notices` / `queue-overdue-notices` | enqueues reminder emails |

Optional (commented out in the sample): `prune-audit-log`,
`prune-notifications`, `prune-cover-cache`, `prune-metadata-cache`,
`prune-scan-pairings`, `purge-trash` (weekly Sun 04:15; permanently deletes
trashed works past `trash_retention_days`, default 90). Uncomment and edit
the retention windows to suit your deployment.

### Caveats

- **Container must be up.** `docker compose exec` against a stopped
  container exits non-zero. If you `docker compose down` for an extended
  window, you'll see error lines in the log; the next tick recovers.
  After a planned downtime you can manually drain the outbox once with:
  ```bash
  docker compose exec -T compendium compendium maintenance send-queued-notifications
  ```
- **The `-T` flag.** Cron has no TTY, so without `-T` every job fails
  with `the input device is not a TTY`. The sample file already includes it.
- **Logs.** Output goes to `/var/log/compendium-maintenance.log` on the
  host. Make sure the user owning the crontab can write there, or edit
  the redirect targets in the sample.
- **Timezone.** Cron honors the host's `/etc/localtime`; jobs fire at
  the host's local time, not UTC.

### Manual maintenance

You can always run a maintenance command ad-hoc:

```bash
docker compose exec compendium compendium maintenance send-queued-notifications
docker compose exec compendium compendium maintenance expire-holds
```

## Remote phone scanner

Compendium supports pairing a phone as a wireless barcode scanner via a QR
code. The phone camera dispatches scans to the desk in real time without
installing an app.

The bundled `nginx.conf` sets `X-Forwarded-Proto https`, so the pairing QR
correctly encodes `https://` without any extra configuration. If you replace
the bundled nginx with a proxy that does **not** set that header, set
`COMPENDIUM_PUBLIC_BASE_URL` to your external `https://` URL in `.env`:

```dotenv
COMPENDIUM_PUBLIC_BASE_URL=https://library.example.org
```

The `docker-compose.yml` passes this through from `.env` automatically.
`COMPENDIUM_SCAN_SESSION_MINUTES` (default 60) controls how long a paired
session stays active; override it the same way. The phone camera requires a
secure context (HTTPS); if the resolved base URL is not secure, the QR is
refused with a warning rather than handing out a dead link.

`prune-scan-pairings` removes terminal (expired or revoked) pairing rows.
Add the commented-out entry from `docker/crontab.sample` to your crontab:

```
35 3 * * * docker compose --project-directory /path/to/docker exec -T compendium \
    compendium maintenance prune-scan-pairings --older-than-days 7
```

## Secret management

By default secrets are passed as plain env vars via `.env`. This is
convenient for quick-start and development, but the values are visible to
anything that can run `docker inspect` or read `/proc/<pid>/environ` on the
host.

For production deployments Compendium supports the **`*_FILE`** pattern used
by official Docker images (postgres, redis, etc.): set the env var to a
**file path** and Compendium reads the secret from the file at startup. This
allows you to use [Docker Swarm secrets](https://docs.docker.com/engine/swarm/secrets/)
or simply world-unreadable files on disk.

### Supported `*_FILE` variables

| `*_FILE` env var | Populates setting |
|---|---|
| `COMPENDIUM_JWT_SECRET_KEY_FILE` | JWT signing key |
| `COMPENDIUM_SECRET_KEY_FILE` | Fernet encryption key |
| `COMPENDIUM_SMTP_PASSWORD_FILE` | SMTP password |
| `COMPENDIUM_TMDB_API_KEY_FILE` | TMDb API key |
| `COMPENDIUM_GOOGLE_BOOKS_API_KEY_FILE` | Google Books API key |
| `POSTGRES_PASSWORD_FILE` | Postgres password (assembled into DATABASE_URL by entrypoint) |

The direct env var always wins if both are set.

### Quick setup with file-based secrets

```bash
mkdir -p docker/secrets
chmod 700 docker/secrets
printf 'my-strong-db-password'     > docker/secrets/postgres_password
printf 'my-long-random-jwt-secret' > docker/secrets/compendium_jwt
chmod 400 docker/secrets/*
```

Then edit `docker/docker-compose.yml`: uncomment the `secrets:` top-level
block and the `*_FILE` env vars in each service, comment out the plain env
var alternatives. The compose file contains inline comments showing exactly
what to change.

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
