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

---

## Database backends

| Backend | Collection size | Concurrent users | Notes |
|---|---|---|---|
| SQLite | up to ~10k items | 1–2 writers | Home, classroom. Simple file-based setup. |
| PostgreSQL | up to ~500k items | 10–100 | Schools, institutions. Set `DATABASE_URL=postgresql+psycopg2://user:pass@host/db`. |

SQLite is the default and requires no additional setup. PostgreSQL requires `psycopg2` or `psycopg` to be installed.

---

## Scheduled maintenance

The following command should run periodically. It expires holds whose `expires_at` has passed.

```bash
compendium maintenance expire-holds
```

See [`crontab.sample`](crontab.sample) for a ready-made cron entry and [`compendium.service.sample`](compendium.service.sample) for running the daemon under systemd.

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
