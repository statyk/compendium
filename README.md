# Compendium

A library card catalog system for physical items — books, vinyl records, DVDs, CDs.

WARNING NOTICE CAVEAT EMPTOR:
This project is 100% vibe coded.  Not only did I not write the code, I've barely even looked at it.
I guided the models (Sonnet and Opus, mostly) on design decisions and the like, but it's pretty much all AI-generated code and documentation.
This paragraph here is about the only part of the project written by a human.


**Status:** Active development. Core circulation, holds, patron management, and a full web UI are complete. See [`docs/architecture.md`](docs/architecture.md) for architecture and design decisions.

## Features

- **Catalog** — add items by ISBN / UPC / MusicBrainz ID / TMDb ID / title search (Open Library, MusicBrainz, TMDb) or manually for obscure items; search and browse works and copies; faceted discovery (media type, decade, availability)
- **Circulation** — checkout, checkin, loan renewal with category-aware per-media-type loan policies; lost / damaged / claims-returned states; self-checkout kiosk mode
- **Holds** — patron reservation queue; immediate promotion when a copy is available; suspend/resume; auto-expiry via maintenance command
- **Fines** — configurable per-policy overdue rates with caps and grace periods; lost/damaged fees; threshold-based checkout/hold blocking; pay/waive workflow; per-patron and bulk overdue assessment
- **Notifications** — outbox-pattern email delivery (hold-ready, due-soon, overdue) drained by a cron-invoked CLI; admin viewer + retry; per-patron opt-out; configurable retention
- **Reports** — checkouts/month, popular works, dormant items (weeding list), current overdues; CSV export; Chart.js trendlines
- **Patrons & cards** — patron categories (Adult/Child/Staff/Teacher seeded), card expiry with maintenance auto-deactivation, optional 1:1 patron↔user link for self-service
- **Bulk import/export** — round-trippable CSV; MARC21 binary + MARCXML import/export
- **Backup/restore** — portable JSONL tarballs; backend-agnostic (SQLite ↔ Postgres); doubles as a DB migration path
- **Labels** — Avery-template item labels (spine / pocket) and patron cards (full / sticker) as PDFs
- **Auth** — five preset roles (ReadOnly, Patron, Librarian, SystemAdmin, Administrator) plus custom roles via the admin UI; JWT for API, cookie-based for web UI
- **Audit log** — synchronous trail of administrative mutations (Librarian + system tier); queryable via web UI, CLI, or REST
- **DB-editable settings** — most configuration knobs (library name, fines, kiosk timeout, SMTP, retention, etc.) editable from the UI / CLI / API; env vars still win as a break-glass
- **Web UI** — HTMX + Jinja2 browser interface with catalog search, circulation desk (camera-based barcode scanning), patron self-service, light/dark/auto theme
- **REST API** — FastAPI; consumed by the web UI and available for integrations
- **CLI** — full librarian + sysadmin workflow without running a server, including stdin/stdout (`-`) for backup, import/export, and labels

## Quick start

### Prerequisites

**Debian / Ubuntu**
```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-dev build-essential
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

**RedHat / CentOS / Fedora**
```bash
sudo dnf install -y python3 python3-devel gcc
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

**macOS**
```bash
brew install python uv
```

### Run

```bash
# Install dependencies
uv sync --extra dev

# Initialise the database (creates ./compendium.db with SQLite by default)
uv run compendium db init

# Create an Administrator account
uv run compendium user add --username admin --role Administrator

# Add a book by ISBN (looks up metadata from Open Library)
uv run compendium item add --isbn 9780441013593

# Add a patron
uv run compendium patron add --name "Alice Example"

# Start the server (web UI at http://localhost:8000/ui/catalog)
uv run compendium serve
```

Log in at `http://localhost:8000/ui/login` with the username and password you set above.

## CLI reference

Run `compendium --help` for the full command tree, or `compendium <group> --help` for the subcommands of a specific group.

**Catalog & cataloging**
| Group | Common subcommands |
|---|---|
| `item` | `add` (--isbn / --upc / --mbid / --tmdb-id / --title), `add-manual`, `show`, `list`, `withdraw`, `set-loanable` |
| `work` | `search`, `show`, `new-arrivals`, `recently-returned` |
| `creator` | `list`, `show`, `merge` |
| `branch` | `list`, `set` |
| `import` | `csv`, `marc` (use `-` for stdin) |
| `export` | `csv`, `marc` (use `-` for stdout) |

**Circulation**
| Group | Common subcommands |
|---|---|
| `loan` | `checkout`, `checkin`, `renew`, `active`, `list` (system-wide), `history`, `item-history`, `declare-lost`, `mark-damaged`, `clear-lost`, `clear-damage`, `claim-returned`, `verify-returned`, `write-off-claim`, `list-claims` |
| `hold` | `place`, `cancel`, `list`, `queue`, `suspend`, `resume`, `list-suspended` |
| `fine` | `list`, `pay`, `waive`, `assess`, `assess-overdue` |

**Patrons & accounts**
| Group | Common subcommands |
|---|---|
| `patron` | `add`, `list`, `set`, `link-user`, `unlink-user`, `deactivate` |
| `patron-category` | `list`, `create`, `update`, `delete` |
| `user` | `add` (default --role Administrator), `set-role`, `set-password`, `deactivate` |
| `role` | `list`, `create`, `update`, `clone` |
| `policy` | `list`, `create`, `set` (configures fines, category-aware) |

**Reporting & labels**
| Group | Common subcommands |
|---|---|
| `reports` | `checkouts`, `popular`, `dormant`, `overdues` (each supports `--format csv`) |
| `labels` | `templates`, `items`, `patrons` (output `-o -` writes PDF to stdout) |
| `audit` | `list` (filters: `--entity`, `--id`, `--user-id`, `--limit`) |

**Operations**
| Command | Description |
|---|---|
| `compendium db init` / `db upgrade` / `db history` | Migrate the database |
| `compendium serve` | Start the API + web UI server |
| `compendium backup --output <path-or->` | Write a portable JSONL tarball backup |
| `compendium restore <path-or->` | Restore from a backup tarball (lenient — auto-migrates) |
| `compendium settings list/get/set/reset` | Inspect & edit DB-backed site settings |
| `compendium maintenance ...` | Cron-invoked tasks: `expire-holds`, `resume-expired-suspends`, `assess-overdue-fines`, `queue-due-soon-notices`, `queue-overdue-notices`, `send-queued-notifications`, `prune-notifications`, `prune-audit-log`, `deactivate-expired-patrons`, `prune-cover-cache` |

File-argument commands (`backup`, `restore`, `import`, `export`, `labels items/patrons`) accept `-` for stdin/stdout. Status messages are routed to stderr in stdio mode so they don't corrupt binary output.

## Web UI

Start the server with `compendium serve` and open your browser to `http://localhost:8000/ui/catalog`.

"Audience" lists the *minimum* preset that can access each route. Administrator can access everything. SystemAdmin gets the System-tier rows; slimmed Librarian gets librarian-tier rows but not System-tier.

| URL | Audience | Description |
|---|---|---|
| `/ui/catalog` | Anyone | Search catalog with facets (media type, decade, availability) |
| `/ui/catalog/{work_id}` | Anyone | Work detail, items, place-hold button |
| `/ui/login` | Anyone | Login form |
| `/ui/me/loans` | Patron | Active loans with inline renew + "I returned this" claim |
| `/ui/me/holds` | Patron | Active holds with inline cancel + suspend/resume |
| `/ui/me/fines` | Patron | Outstanding and historical fines |
| `/ui/me/preferences` | Patron | Notification opt-out |
| `/ui/me/password` | Patron | Self-service password change |
| `/ui/circ` | Librarian | Circulation desk — checkout / checkin / renew |
| `/ui/kiosk` | Librarian | Self-checkout kiosk landing/session (patron card-number-only auth) |
| `/ui/items/new` | Librarian | Add item by ISBN / UPC / MBID / TMDb ID / title search (with barcode scanner) |
| `/ui/items/new/manual` | Librarian | Manually add an item not found in external sources |
| `/ui/items/{barcode}` | Librarian | Item detail, loan history, withdraw, set-loanable, lost/damaged/claims actions |
| `/ui/patrons` | Librarian | Patron list |
| `/ui/patrons/new` | Librarian | Create patron (with category + expiry) |
| `/ui/patrons/{card}` | Librarian | Patron detail with active loans, holds, link/unlink user, deactivate |
| `/ui/patrons/{card}/loans` | Librarian | Patron loan history (active / returned / all) |
| `/ui/patrons/{card}/fines` | Librarian | Patron fines with pay/waive |
| `/ui/admin/loans` | Librarian | All active loans (system-wide) with overdue/due-soon filters |
| `/ui/admin/fines` | Librarian | All outstanding fines (system-wide) with running total |
| `/ui/admin/holds` | Librarian | All active holds with status/branch/work filters |
| `/ui/admin/claims` | Librarian | Outstanding claims-returned investigations |
| `/ui/admin/notifications` | Librarian | Notification log + manual retry |
| `/ui/admin/import` | Librarian | Bulk CSV/MARC import |
| `/ui/admin/export` | Librarian | Bulk CSV/MARC export |
| `/ui/admin/patron-categories` | Librarian | Manage patron categories |
| `/ui/admin/settings/general` | Librarian | Library name, default theme, guest search |
| `/ui/admin/settings/circulation` | Librarian | Currency, fine thresholds, hold/overdue/due-soon defaults |
| `/ui/admin/settings/kiosk` | Librarian | Kiosk idle timeout |
| `/ui/policies` | Librarian | Loan policy list with inline edit |
| `/ui/policies/new` | Librarian | Create loan policy (per media type + patron category) |
| `/ui/branches` | Librarian | Branch list with classification scheme |
| `/ui/branches/{id}/edit` | Librarian | Edit a branch's default classification scheme |
| `/ui/reports` | Librarian | Reports landing — checkouts, popular, dormant, overdues |
| `/ui/labels` | Librarian | Generate item-label and patron-card PDFs (Avery templates) |
| `/ui/audit` | Librarian | Audit log viewer |
| `/ui/users` | SystemAdmin | User list |
| `/ui/users/new` | SystemAdmin | Create user |
| `/ui/users/{username}` | SystemAdmin | User detail — change role, deactivate, reset password |
| `/ui/roles` | SystemAdmin | Role list |
| `/ui/roles/new` | SystemAdmin | Create custom role |
| `/ui/roles/{id}` | SystemAdmin | Role detail — edit permissions, clone |
| `/ui/admin/system/smtp` | SystemAdmin | SMTP host/port/from settings (password env-only) |
| `/ui/admin/system/retention` | SystemAdmin | Notification + audit retention, batch sizes |

Guest catalog search is enabled by default (toggle via `/ui/admin/settings/general` or `COMPENDIUM_GUEST_SEARCH_ENABLED=false`).

## REST API endpoints

The API is mounted at the root. Authenticate with `POST /auth/login` and pass the result as `Authorization: Bearer <token>`. Interactive docs live at `http://localhost:8000/docs` when the server is running.

Below is a high-level inventory grouped by concern; the OpenAPI document is the source of truth for parameters and bodies.

| Group | Common endpoints | Min permission |
|---|---|---|
| **Auth** | `POST /auth/login` | none |
| **Catalog** | `GET /works/search`, `/works/new-arrivals`, `/works/recently-returned`; `GET /items/{barcode}`; `POST /items/{barcode}/{withdraw,loanable,verify-returned,write-off-claim,lost,damaged,clear-lost,clear-damage}` | guest / `item.view` / `item.delete` |
| **Patrons** | `GET/POST /patrons`, `PATCH /patrons/{card}`, `POST /patrons/{card}/deactivate`, `GET/POST /patron-categories`, `PATCH/DELETE /patron-categories/{id}` | `patron.manage` |
| **Loans** | `POST /loans/checkout`, `/loans/{id}/{checkin,renew,claim-returned}`, `POST /loans/{id}/declare-lost`/`mark-damaged`; `GET /loans` (system-wide), `/loans/patron/{card}`, `/loans/item/{barcode}`, `/loans/claims` | `loan.*` (see below) |
| **Holds** | `GET /holds`, `/holds/queue/{work_id}`; `POST/DELETE /holds`, `/holds/{id}`; `POST /holds/{id}/{suspend,resume}` | `hold.*` |
| **Fines** | `GET /fines`, `GET/POST /patrons/{card}/fines`, `POST /fines/{id}/{pay,waive}`, `POST /patrons/{card}/fines/assess-overdue` | `fine.manage` / `fine.view.self` |
| **Self-service** | `GET /me/loans`, `/me/holds`; `POST /me/holds`, `/me/holds/{id}/{suspend,resume}`; `DELETE /me/holds/{id}`; `POST /me/loans/{id}/{renew,claim-returned}` | `*.self` permissions |
| **Notifications** | `GET /notifications`, `POST /notifications/{id}/retry` | `notification.manage` |
| **Reports** | `GET /reports/{checkouts,popular,dormant,overdues}` | `report.view` |
| **Bulk import/export** | `POST /import/{csv,marc}` (multipart), `GET /export/{csv,marc}` (streaming) | `catalog.import` / `item.view` |
| **Labels** | `GET /labels/items`, `/labels/patrons` (PDF) | `labels.generate` |
| **Policies** | `GET/POST /policies` | `item.view` / `policy.edit` |
| **Users** | `POST /users/{username}/deactivate` | `user.manage` |
| **Settings** | `GET /settings/`, `GET/PATCH/DELETE /settings/{key}` | `patron.manage` (librarian-tier) / `system.manage` (system-tier) |
| **Audit** | `GET /audit/` | `audit.view` |

The "min permission" column lists the lowest preset role that's allowed. Administrator (wildcard) covers everything. Slimmed Librarian covers librarian-tier endpoints; SystemAdmin covers user/role/system-tier endpoints.

## Configuration

Compendium settings come from three layers, in order of precedence:

1. **Environment variable** (`COMPENDIUM_<KEY>`) — break-glass override, wins over everything.
2. **`site_setting` table** — DB-backed, editable from `/ui/admin/settings/*`, the CLI (`compendium settings ...`), or the API (`PATCH /settings/{key}`). Changes take effect on the next page render — no restart.
3. **Registry default** — fallback hard-coded in `services/settings_registry.py`.

Most settings (library name, theme, fine thresholds, hold/overdue defaults, kiosk timeout, SMTP host/port/from, retention) are **DB-editable**. A handful stay env-only because they're either secrets or required before the DB is reachable:

| Env var | Why env-only |
|---|---|
| `COMPENDIUM_DATABASE_URL` | Bootstrap — needed before any DB read |
| `COMPENDIUM_JWT_SECRET_KEY` | Secret — **required**; server refuses to start with the built-in default. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Set `COMPENDIUM_ALLOW_INSECURE_JWT=1` to bypass for first-run/dev only. |
| `COMPENDIUM_JWT_ALGORITHM` / `COMPENDIUM_JWT_EXPIRE_MINUTES` | Auth deployment knobs |
| `COMPENDIUM_SSL_CERTFILE` / `COMPENDIUM_SSL_KEYFILE` | OS-level paths |
| `COMPENDIUM_SECURE_COOKIES` | Default `true`; set `false` only for plain-HTTP LAN deployments (browsers won't send `Secure` cookies over non-HTTPS, except localhost). |
| `COMPENDIUM_TMDB_API_KEY` | Secret — required only for film metadata (TMDb). |
| `COMPENDIUM_GOOGLE_BOOKS_API_KEY` | Optional — enables Google Books as a cover-image fallback when Open Library has none. Free tier at console.cloud.google.com. |
| `COMPENDIUM_LIBRARYTHING_API_KEY` | Optional — enables LibraryThing as a cover-image fallback and as the MDS classification source. Non-commercial ToS; register at librarything.com/services/keys. |
| `COMPENDIUM_SMTP_PASSWORD` | Secret (host/port/from are DB-editable; password stays env) |
| `COMPENDIUM_MAX_UPLOAD_BYTES` | Hard cap on bulk-import upload size (default 100 MB / 104857600). Env-only so a compromised admin token can't raise it. |
| `COMPENDIUM_LOGIN_MAX_FAILURES` | Max consecutive failed logins before the identity is throttled (default 10). DB-editable at **System → Security**. |
| `COMPENDIUM_LOGIN_FAILURE_WINDOW_SECONDS` | Sliding window for counting failures in seconds (default 300 = 5 min). DB-editable at **System → Security**. |

For everything else, run `compendium settings list` to see the current value, source (env vs db/default), and per-key help text. The web admin UI shows the same with an "⚠ Overridden by env var" indicator on rows where an env var is currently masking the DB value.

See [`docs/deployment.md`](docs/deployment.md) for full deployment guidance, or [`docker/README.md`](docker/README.md) for the bundled Docker Compose setup (app + Postgres + nginx with auto-generated self-signed TLS).

### PostgreSQL

SQLite is the default and fine for home or classroom use (up to ~10k items). For larger collections or multiple concurrent writers, use PostgreSQL:

```bash
uv sync --extra postgres
```

Then set:

```dotenv
COMPENDIUM_DATABASE_URL=postgresql+psycopg://compendium:<password>@localhost:5432/compendium
```

and run `compendium db init` to apply migrations. Full setup (creating the role and database, TLS, backups) is in [`docs/deployment.md`](docs/deployment.md#postgresql-setup).

## Scheduled maintenance

Several maintenance commands need to run on a cadence — most importantly the
email outbox drain (`send-queued-notifications`), without which queued
notifications never go out. Install the bundled crontab via:

```bash
scripts/install-cron.sh
```

By default this writes the project path into the crontab and points logs at
`$HOME/.local/state/compendium/maintenance.log`. Override either with flags:

```bash
scripts/install-cron.sh --project-dir /opt/compendium --log-file journal
scripts/install-cron.sh --log-file /var/log/compendium/maintenance.log
```

`--log-file journal` routes output to the systemd journal (view with
`journalctl -t compendium-maintenance -f`). For paths the installer can't
create unprivileged (e.g. `/var/log/...`), it prints the one-time `sudo`
command and exits without touching the crontab.

See [`docs/crontab.sample`](docs/crontab.sample) for the full schedule and
[`docs/compendium.service.sample`](docs/compendium.service.sample) for
running the daemon under systemd.

## Running tests

```bash
uv run pytest -q
```

Tests are split into `tests/unit/` (no DB, mock repos) and `tests/integration/` (SQLite in-memory).

### Browser tests (E2E)

Browser tests run against a real `compendium serve` subprocess in Chromium. They are excluded from the default `pytest` run and require a one-time install:

```bash
uv sync --extra e2e
playwright install chromium
```

Then:

```bash
uv run pytest -m e2e
```

Expected wall time: 30–60 seconds. Tests live in `tests/e2e/`. The `test_csp_no_console_errors.py` test is the keystone: it navigates to every major page and asserts no console errors, which catches CSP/HTMX loading regressions that unit tests miss.

## Layout

```
src/compendium/
├── domain/        # models, enums, permissions, errors
├── repositories/  # base protocols + SQLAlchemy implementations
├── services/      # business logic (catalog, circulation, holds, patrons, policies, auth, audit)
├── api/           # FastAPI routes + Pydantic schemas + JWT auth
├── web/           # HTMX + Jinja2 web UI + CSRF protection
├── cli/           # Typer CLI commands
├── config/        # settings, seed data
└── db/            # engine factory, session lifecycle
```

## License

MIT (to be finalised before first release).
