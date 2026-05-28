# Deployment

## Deployment modes

Compendium has one installed command (`compendium`) that supports three usage patterns:

### 1. Library mode (CLI only)

The CLI imports services directly and exits. No daemon required. Suitable for home use and scripting.

```bash
uv run compendium item add --isbn 9780441013593
uv run compendium loan checkout --barcode BC001 --card C0001
```

### 2. Daemon mode (web + API)

`compendium serve` starts a Uvicorn/FastAPI server. Both the REST API and the web UI are served from the same process.

```bash
uv run compendium serve
# Web UI:  http://localhost:8000/ui/catalog
# API:     http://localhost:8000/
# API docs: http://localhost:8000/docs
```

Default port is 8000. Set `--host` and `--port` as needed.

### 3. Both

Run the daemon for the web UI and use CLI commands for admin tasks or scripts — they share the same database file.

---

## First-run checklist

```bash
# 1. Initialise database (creates compendium.db in the current directory for SQLite)
compendium db init

# 2. Create the first administrator account
compendium user add --username admin --role Administrator

# 3. (Optional) Seed a patron for testing
compendium patron add --name "Test Patron"

# 4. Start the server
compendium serve
```

---

## Production configuration

Compendium reads configuration from three layers, in order: env var → `site_setting` DB row → registered default. Most knobs (library name, fine thresholds, hold/overdue defaults, kiosk timeout, SMTP host/port/from, retention, etc.) are now **DB-editable** at runtime via `/ui/admin/settings/*`, the CLI (`compendium settings ...`), or the API. Env vars still win when set — useful as a deployment-time pin or break-glass override.

The `.env` file is for things that *must* be set before the DB is available, plus secrets:

```dotenv
# .env — example production settings

COMPENDIUM_DATABASE_URL=sqlite:////var/lib/compendium/compendium.db
COMPENDIUM_JWT_SECRET_KEY=<generate with: compendium keygen --jwt>
COMPENDIUM_JWT_EXPIRE_MINUTES=480
COMPENDIUM_SECURE_COOKIES=true            # default; set `false` for plain-HTTP LAN deploys

# Optional: enable the encrypted-secrets UI (/ui/admin/system/secrets).
# Generate with: compendium keygen --secret
# COMPENDIUM_SECRET_KEY=<Fernet key>

# SMTP password, TMDb key, Google Books key — set these via the Secrets page
# when COMPENDIUM_SECRET_KEY is configured, OR override here (env wins on read).
# COMPENDIUM_SMTP_PASSWORD=
# COMPENDIUM_TMDB_API_KEY=
# COMPENDIUM_GOOGLE_BOOKS_API_KEY=          # enables GB as primary book metadata source
# COMPENDIUM_BOOK_METADATA_SOURCE_PREFERENCE=googlebooks  # or 'openlibrary'
# COMPENDIUM_BOOK_METADATA_FALLBACK_ENABLED=true  # set 'false' to disable secondary-source fallback
# COMPENDIUM_METADATA_CACHE_TTL_DAYS=30  # positive-hit TTL for metadata cache (days)
# COMPENDIUM_LIBRARY_TIMEZONE=America/New_York  # IANA timezone for due-date rolling (default: UTC)
```

**`JWT_SECRET_KEY` must be set to a strong random value.** The built-in default is intentionally weak — the server refuses to start when it's detected. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"` (or `openssl rand -base64 48`). For first-run / dev work you may set `COMPENDIUM_ALLOW_INSECURE_JWT=1` to bypass the check, which downgrades it to a startup warning; do not use that in production.

**`SECURE_COOKIES` defaults to `true`.** Auth and CSRF cookies ship with the `Secure` flag, which browsers only return over HTTPS. This is correct for HTTPS deployments (in-process TLS or a reverse proxy terminating TLS) and for localhost dev (browsers treat localhost as a secure context). Set `COMPENDIUM_SECURE_COOKIES=false` only if you are intentionally serving plain HTTP to non-localhost browsers — e.g., a small classroom or home deployment on a trusted LAN with no TLS anywhere. Without that opt-out, login state will collapse on plain-HTTP origins because the browser refuses to send the cookie back.

**`MAX_UPLOAD_BYTES` defaults to 100 MB.** Bulk-import endpoints (`/import/csv`, `/import/marc`, `/ui/admin/import`) reject any upload larger than this with `413 Content Too Large`. Env-only on purpose — making it DB-editable would let a compromised admin token raise the cap to bypass the protection. Adjust at deploy time if you regularly import larger catalogs (`COMPENDIUM_MAX_UPLOAD_BYTES=209715200` for 200 MB, etc.). The CLI import commands are not subject to this cap (different trust boundary; the operator already has shell access).

**Login rate limiting (default: 10 failures / 5-minute window).** Compendium enforces a per-identity sliding-window throttle on `/auth/login`, `/ui/login`, and the kiosk card gate. After `login_max_failures` consecutive failures within `login_failure_window_seconds` seconds, further attempts from the same username or kiosk card number return 429 with a `Retry-After` header. Throttling is **strictly per-identity** — there is no IP-based block, so users behind a shared reverse proxy are never collectively locked out. Credential-stuffing protection (one attacker, many usernames) is **not** provided by the in-app guard; configure `limit_req_zone` (nginx) or `rate_limit` (Caddy) at the edge for that. Both settings are DB-editable from **Admin → System → Security** and can also be pinned via `COMPENDIUM_LOGIN_MAX_FAILURES` / `COMPENDIUM_LOGIN_FAILURE_WINDOW_SECONDS`. Set `login_max_failures` to `0` to disable throttling.

**Env-only** (must live in env, cannot be set via the UI): `database_url`, `jwt_secret_key`, `jwt_algorithm`, `jwt_expire_minutes`, `ssl_certfile`, `ssl_keyfile`, `secure_cookies`, `max_upload_bytes`, and `COMPENDIUM_SECRET_KEY` itself (the encryption key can't be stored in the database it protects). `smtp_password`, `tmdb_api_key`, and `google_books_api_key` are now **also** settable via **Admin → System → Secrets** when `COMPENDIUM_SECRET_KEY` is configured — env vars remain a break-glass override (env wins on read). Everything else can flow through the DB-backed `site_setting` table — see `compendium settings list` for the full registered set with current sources.

### Secret storage

`COMPENDIUM_SECRET_KEY` enables encrypted storage of sensitive settings (SMTP password, TMDb API key, Google Books API key) in the database. Without it those settings remain env-only and the Secrets page shows a setup banner.

**Generate keys for a fresh deployment:**

```bash
compendium keygen
# prints COMPENDIUM_JWT_SECRET_KEY and COMPENDIUM_SECRET_KEY — copy both to .env
```

Use `--jwt` or `--secret` to print only one key. `--quiet` omits the comments.

**Manage secrets from the CLI** (useful for headless/scriptable deployments):

```bash
compendium secrets list                 # show all registered secrets + their source
compendium secrets set smtp_password    # prompts for value, encrypts + stores
compendium secrets clear tmdb_api_key   # removes the stored row
```

**Encryption details**: values are stored as `enc:v1:<Fernet ciphertext>` (AES-128-CBC + HMAC-SHA256). A canary row (`_secret_canary`) in the settings table lets the app detect key rotation immediately — the Admin → System → Secrets page shows a "Key mismatch" banner rather than silently decrypting garbage. To rotate the key: set the new key in env, re-paste each secret value via the UI or CLI.

---

## Database backends

| Backend | Collection size | Concurrent users | Notes |
|---|---|---|---|
| SQLite | up to ~10k items | 1–2 writers | Home, classroom. Simple file-based setup. Default. |
| PostgreSQL | up to ~500k items | 10–100 | Schools, institutions. See [PostgreSQL setup](#postgresql-setup). |

SQLite is the default and requires no additional setup — `compendium db init` will create `compendium.db` in the current working directory unless `COMPENDIUM_DATABASE_URL` says otherwise.

### PostgreSQL setup

#### 1. Install Compendium with the `postgres` extra

The psycopg driver is an optional dependency, not bundled by default:

```bash
uv sync --extra postgres
```

#### 2. Create the database and role

On the Postgres host:

```bash
sudo -u postgres createuser --pwprompt compendium
sudo -u postgres createdb --owner=compendium compendium
```

Compendium doesn't require any superuser privileges at runtime. The role only needs ownership of (or full privileges on) its own database.

#### 3. Point Compendium at the database

Set `COMPENDIUM_DATABASE_URL` in your `.env` or environment. The URL uses SQLAlchemy's `postgresql+psycopg://` scheme (psycopg v3):

```dotenv
COMPENDIUM_DATABASE_URL=postgresql+psycopg://compendium:<password>@localhost:5432/compendium
```

URL-encode any special characters in the password (e.g. `@` → `%40`).

For a remote host with TLS, append `?sslmode=require`:

```dotenv
COMPENDIUM_DATABASE_URL=postgresql+psycopg://compendium:<password>@db.example.com:5432/compendium?sslmode=require
```

#### 4. Run migrations and seed defaults

```bash
compendium db init
```

This applies all Alembic migrations and seeds the default branch, media types, roles, and loan policy. It is safe to re-run; subsequent invocations are no-ops once schema and seed rows are in place.

#### 5. Create the first administrator

```bash
compendium user add --username admin --role Administrator
compendium serve
```

#### Backups

Use `pg_dump` / `pg_restore` for backups. A typical nightly dump:

```bash
pg_dump --format=custom --file=/var/backups/compendium-$(date +%F).dump \
        --dbname=postgresql://compendium@localhost/compendium
```

#### Switching an existing SQLite deployment to PostgreSQL

There is no built-in migration tool. For an existing deployment, export data with a manual script (the `compendium` CLI can enumerate works/items/patrons) and replay against a fresh Postgres-backed install. This is easier before the catalog grows — plan your backend up front if you anticipate crossing ~10k items.

---

## Scheduled maintenance

Several maintenance commands should run periodically:

- `maintenance expire-holds` — expires waiting and pickup-shelf holds past their deadline.
- `maintenance resume-expired-suspends` — auto-resumes patron-suspended holds whose end date has passed.
- `maintenance assess-overdue-fines` — materializes outstanding overdue fines (idempotent).
- `maintenance queue-due-soon-notices` + `queue-overdue-notices` — queue reminder emails.
- `maintenance send-queued-notifications` — drain the outbox via SMTP.
- `maintenance prune-notifications` / `prune-audit-log` — retention cleanup.
- `maintenance deactivate-expired-patrons` — flips `is_active=false` for patrons whose `expires_at` has passed.
- `maintenance prune-cover-cache --max-mb N` — bound the on-disk cover-image cache.
- `maintenance prune-metadata-cache` — delete expired metadata cache rows (past positive or negative TTL).
- `metadata cache clear` — delete all metadata cache rows (audited); useful if a metadata source's response shape changes.
- `metadata cache stats` — print cache row counts by adapter and TTL status.

### Installing the schedule

[`crontab.sample`](crontab.sample) holds the full schedule. The fastest way
to wire it up is the bundled installer:

```bash
scripts/install-cron.sh
```

The installer substitutes the project path and the log destination into the
sample, then appends the rendered block to the current user's crontab. Two
flags worth knowing:

- `--project-dir PATH` — defaults to `$(pwd)`. Use when you run the script
  from somewhere other than the repo root.
- `--log-file PATH` — defaults to `$HOME/.local/state/compendium/maintenance.log`
  (auto-created). Pass `journal` to route output to the systemd journal
  (view with `journalctl -t compendium-maintenance -f`). Paths outside the
  user's writable territory (e.g. `/var/log/...`) require a one-time
  `sudo install -d -o $USER ...` — the installer prints the exact command
  and exits without modifying the crontab if the directory isn't ready.

Re-running the script is a no-op — it refuses to install twice and prints
the tag markers to remove if you want to re-install. Edit the crontab
directly with `crontab -e`.

See also [`compendium.service.sample`](compendium.service.sample) for running
the daemon under systemd.

---

## Email (SMTP)

Compendium queues notification rows synchronously but only sends when SMTP is configured. Without `COMPENDIUM_SMTP_HOST`, the drainer runs inertly — rows accumulate until configuration lands, then drain on the next cron.

### Production

Any SMTP service works (Google Workspace, Amazon SES, Mailgun, Postfix relay, etc.). Minimum env vars:

```bash
export COMPENDIUM_SMTP_HOST=smtp.example.com
export COMPENDIUM_SMTP_PORT=587
export COMPENDIUM_SMTP_USERNAME=apikey
export COMPENDIUM_SMTP_PASSWORD=...
export COMPENDIUM_SMTP_USE_STARTTLS=true
export COMPENDIUM_SMTP_FROM_ADDRESS=library@example.com
export COMPENDIUM_SMTP_FROM_NAME="My Library"
```

For implicit-TLS (port 465) providers set `COMPENDIUM_SMTP_USE_SSL=true` and `COMPENDIUM_SMTP_USE_STARTTLS=false`.

### Development — mailpit

[Mailpit](https://github.com/axllent/mailpit) is a local SMTP sink with a web inbox. Perfect for verifying templates without sending real email:

```bash
docker run -d --name mailpit -p 1025:1025 -p 8025:8025 axllent/mailpit

export COMPENDIUM_SMTP_HOST=localhost
export COMPENDIUM_SMTP_PORT=1025
export COMPENDIUM_SMTP_USE_STARTTLS=false
export COMPENDIUM_SMTP_FROM_ADDRESS=noreply@example.test

# Queue something, then drain
compendium maintenance send-queued-notifications

# View captured email at http://localhost:8025
```

`python -m aiosmtpd -n -l localhost:1025` also works as a lightweight alternative that prints received mail to the console.

---

## HTTPS / TLS

Camera-based barcode scanning requires HTTPS (browsers block camera access on plain HTTP, except on `localhost`). Three options:

### Option A — Native TLS (manual cert)

Pass certificate and key directly to the server. No proxy required.

```bash
compendium serve --ssl-certfile /path/to/cert.pem --ssl-keyfile /path/to/key.pem
```

Or set via environment / `.env`:

```dotenv
COMPENDIUM_SSL_CERTFILE=/path/to/cert.pem
COMPENDIUM_SSL_KEYFILE=/path/to/key.pem
```

For LAN-only deployments, generate a self-signed cert with `mkcert` and trust it on each device. For public deployments, obtain a cert from Let's Encrypt with certbot and renew it via cron.

### Option B — Caddy (automatic Let's Encrypt)

Caddy handles ACME negotiation, certificate renewal, and OCSP stapling automatically.

```
# Caddyfile
library.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
caddy run --config /etc/caddy/Caddyfile
```

### Option C — nginx reverse proxy

```nginx
server {
    listen 443 ssl;
    server_name library.example.com;

    ssl_certificate     /etc/letsencrypt/live/library.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/library.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## Upgrading

```bash
git pull
uv sync
compendium db init   # applies any new Alembic migrations
compendium serve
```

### Upgrading to the identifier-revamp release (migration `c3d4e5f6a7b8`)

This migration regenerates every item barcode, accession number, and patron
library card number in the new 10/14-digit format. Physical labels printed
before the migration will no longer scan; plan a relabeling window.

**Before running `compendium db init`**, drain any queued notification
messages. Queued messages may contain legacy barcodes in their rendered
bodies; draining first ensures patrons receive legible messages:

```bash
compendium maintenance send-queued-notifications
compendium db init
```

If you intentionally skip the drain step (e.g., no patrons are configured or
SMTP is not yet set up), the outbox rows will remain and will eventually be
sent with the old barcode values in the body text — harmless, but potentially
confusing. Delete them with `compendium maintenance prune-notifications
--older-than-days 0` if needed.

The migration's `downgrade()` is not implemented — regenerated codes cannot
be reversed. Take a backup before upgrading:

```bash
compendium backup --output compendium-pre-revamp.tar.gz
compendium maintenance send-queued-notifications
compendium db init
```

---

## Backup and restore

Compendium ships portable JSONL dumps that round-trip across SQLite and
Postgres. One backup format works for disaster recovery *and* for migrating
between backends.

### Taking a backup

```bash
compendium backup --output /var/backups/compendium/2026-04-24.tar.gz
```

The tarball contains a `meta.json` manifest, one JSONL file per table under
`data/`, and (by default) the on-disk cover image cache under `covers/`.

Options:

- `--no-covers` — skip the cover cache (covers are fetched on demand, so this
  is safe; use it if the cache is large and you keep backups off-machine).
- `--no-audit` — drop the `audit_log` table (slimmer file; lose forensic
  history).
- `--include-secret-key` — bundle `COMPENDIUM_SECRET_KEY` into the backup
  manifest. Convenient for single-file portability but **defeats
  encryption-at-rest**: anyone with the file can decrypt stored secrets. Prefer
  keeping the backup and key separate. The recommended portable alternative:

  ```bash
  compendium backup -o - | gpg --symmetric > backup-$(date +%F).tar.gz.gpg
  ```

  Restore the GPG bundle with:

  ```bash
  gpg --decrypt backup-2026-05-09.tar.gz.gpg | compendium restore -
  ```

Nightly cron entry:

```
15 2 * * *  compendium backup --output "/var/backups/compendium/$(date +\%Y-\%m-\%d).tar.gz"
```

### Restoring

```bash
compendium restore /var/backups/compendium/2026-04-24.tar.gz
```

Restore refuses if the target database already has real data (rows in any
non-seed table). Pass `--force` to wipe and replace; `--yes` skips the
confirmation prompt for scripted use.

Restore is **lenient** by default: if the backup was taken at an older
Alembic revision than the currently-installed Compendium, the restore
automatically migrates the target to the backup's revision, inserts the
rows, then replays migrations forward to the current head. You can safely
restore a months-old backup into an upgraded deployment.

Restore will refuse when the backup's revision isn't known to the current
code — upgrade Compendium to a version that includes that revision before
restoring.

### Migrating backends

The same format restores into a different backend:

```bash
# On the SQLite source
compendium backup --output compendium-pre-migrate.tar.gz

# Install Postgres, create database (see "PostgreSQL setup" above)
export COMPENDIUM_DATABASE_URL=postgresql://compendium:...@localhost/compendium
compendium restore compendium-pre-migrate.tar.gz
```

The reverse (Postgres → SQLite) works identically, subject to the scale
limits documented in "Database backends."
