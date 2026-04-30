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
`prune-notifications`, `prune-cover-cache`. Uncomment and edit the
retention windows to suit your deployment.

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
