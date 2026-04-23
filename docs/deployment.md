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

# 2. Create the first Librarian account
compendium user add --username admin --role Librarian

# 3. (Optional) Seed a patron for testing
compendium patron add --name "Test Patron"

# 4. Start the server
compendium serve
```

---

## Production configuration

All settings are read from environment variables (prefix `COMPENDIUM_`) or a `.env` file in the working directory.

```dotenv
# .env — example production settings

COMPENDIUM_DATABASE_URL=sqlite:////var/lib/compendium/compendium.db
COMPENDIUM_JWT_SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
COMPENDIUM_JWT_EXPIRE_MINUTES=480
COMPENDIUM_GUEST_SEARCH_ENABLED=true
COMPENDIUM_HOLD_EXPIRY_DAYS=30
COMPENDIUM_HOLD_PICKUP_DAYS=3
```

**`JWT_SECRET_KEY` must be set to a strong random value in any non-development deployment.** The built-in default is intentionally weak and will produce a startup warning in a future release.

**Settings are loaded once per process.** `get_settings()` is `@lru_cache`-wrapped, so changes to env vars or `.env` (SMTP credentials, fine thresholds, currency symbol, search flags, etc.) don't take effect until you restart `compendium serve`. CLI invocations re-read on each run. When the planned `site_setting` table lands, runtime-editable knobs will move there.

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

#### 5. Create the first Librarian

```bash
compendium user add --username admin --role Librarian
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

- `maintenance expire-holds` — expires holds whose pickup window has passed.
- `maintenance assess-overdue-fines` — materializes outstanding overdue fines (idempotent).
- `maintenance queue-due-soon-notices` + `queue-overdue-notices` — queue reminder emails.
- `maintenance send-queued-notifications` — drain the outbox via SMTP.
- `maintenance prune-notifications` / `prune-audit-log` — retention cleanup.

See [`crontab.sample`](crontab.sample) for a ready-made schedule and [`compendium.service.sample`](compendium.service.sample) for running the daemon under systemd.

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
