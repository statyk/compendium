# Compendium

A library card catalog system for physical items — books, vinyl records, DVDs, CDs.

**Status:** Active development. Core circulation, holds, patron management, and a full web UI are complete. See [`CLAUDE.md`](CLAUDE.md) for architecture and design decisions.

## Features

- **Catalog** — add items by ISBN / UPC / MusicBrainz ID / TMDb ID / title search (Open Library, MusicBrainz, TMDb) or manually for obscure items; search and browse works and copies
- **Circulation** — checkout, checkin, loan renewal with configurable per-media-type loan policies
- **Holds** — patron reservation queue; automatic promotion on checkin; expiry via maintenance command
- **Auth** — role/permission model (ReadOnly, Patron, Librarian); JWT for API, cookie-based for web UI
- **Audit log** — synchronous trail of Librarian mutations (items, works, patrons, users, policies); queryable via CLI
- **Web UI** — HTMX + Jinja2 browser interface: catalog search, circulation desk (with camera-based barcode scanning), patron self-service
- **REST API** — FastAPI; consumed by the web UI and available for integrations
- **CLI** — full librarian workflow without running a server

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

# Create a Librarian account
uv run compendium user add --username admin --role Librarian

# Add a book by ISBN (looks up metadata from Open Library)
uv run compendium item add --isbn 9780441013593

# Add a patron
uv run compendium patron add --name "Alice Example"

# Start the server (web UI at http://localhost:8000/ui/catalog)
uv run compendium serve
```

Log in at `http://localhost:8000/ui/login` with the username and password you set above.

## CLI reference

| Command | Description |
|---|---|
| `compendium db init` | Initialise / migrate the database |
| `compendium item add --isbn <isbn>` | Add a book by ISBN (Open Library lookup) |
| `compendium item add --upc <upc> --media-type <vinyl\|cd>` | Add music by UPC (MusicBrainz lookup) |
| `compendium item add --mbid <uuid> --media-type <vinyl\|cd>` | Add music by MusicBrainz release ID |
| `compendium item add --tmdb-id <id> --media-type <dvd\|bluray\|vhs>` | Add film by TMDb ID (requires `COMPENDIUM_TMDB_API_KEY`) |
| `compendium item add --title "..." --media-type <code>` | Title search + interactive candidate picker (any media type) |
| `compendium item add-manual --title "..." --media-type <code>` | Manually enter metadata for items not in any external source |
| `compendium item show <barcode>` | Show item detail |
| `compendium item list` | List all items |
| `compendium item withdraw --barcode <barcode>` | Withdraw (soft-remove) an item |
| `compendium work search <query>` | Search works (title / author / description) |
| `compendium work show <work_id>` | Show work detail and copies |
| `compendium patron add --name <name>` | Add a patron (optional `--link-user <username>`) |
| `compendium patron list` | List patrons |
| `compendium patron link-user --card <c> --username <u>` | Link an existing patron to a user account |
| `compendium patron unlink-user --card <c>` | Unlink the user account from a patron |
| `compendium patron deactivate --card <card>` | Deactivate a patron account |
| `compendium loan checkout --barcode <b> --card <c>` | Check out an item |
| `compendium loan checkin --barcode <barcode>` | Check in an item |
| `compendium loan renew --barcode <b> --card <c>` | Renew an active loan |
| `compendium loan active --card <card>` | List active loans for a patron |
| `compendium hold place --work-id <id> --card <c>` | Place a hold |
| `compendium hold cancel --id <hold_id> --card <c>` | Cancel a hold |
| `compendium hold list --card <card>` | List active holds for a patron |
| `compendium policy list` | List loan policies |
| `compendium policy create --name <n> --loan-days <d>` | Create a loan policy (`--default` to make it the default) |
| `compendium policy set --id <id> --loan-days <d>` | Update a loan policy (`--default/--no-default` to change default flag) |
| `compendium role list` | List all roles |
| `compendium role create --name <n> --permissions <p,...>` | Create a custom role (`--full-access` for `["*"]`) |
| `compendium role update --id <id> --name <n>` | Rename or change permissions on a custom role |
| `compendium role clone --id <id> --name <n>` | Clone any role (including presets) into a new editable role |
| `compendium branch list` | List branches and their classification schemes |
| `compendium branch set --code <c> --classification <lcc\|ddc\|none>` | Set a branch's auto-populate classification scheme |
| `compendium maintenance expire-holds` | Expire overdue waiting holds (for cron) |
| `compendium user add --username <u> --role <r>` | Create a user account |
| `compendium user set-role --username <u> --role <r>` | Change a user's role |
| `compendium user set-password --username <u>` | Reset a user's password (prompts if `--password` omitted) |
| `compendium user deactivate --username <u>` | Deactivate a user account |
| `compendium audit list` | Browse audit log (supports `--entity`, `--id`, `--user-id`, `--limit`) |
| `compendium serve` | Start the API + web UI server |

## Web UI

Start the server with `compendium serve` and open your browser to `http://localhost:8000/ui/catalog`.

| URL | Audience | Description |
|---|---|---|
| `/ui/catalog` | Anyone | Search catalog (live HTMX results) |
| `/ui/catalog/{work_id}` | Anyone | Work detail, items, place-hold button |
| `/ui/login` | Anyone | Login form |
| `/ui/me/loans` | Patron | Active loans with inline renew |
| `/ui/me/holds` | Patron | Active holds with inline cancel |
| `/ui/circ` | Librarian | Circulation desk — checkout / checkin / renew |
| `/ui/items/new` | Librarian | Add item by ISBN / UPC / MBID / TMDb ID / title search (with barcode scanner) |
| `/ui/items/new/manual` | Librarian | Manually add an item not found in external sources |
| `/ui/items/{barcode}` | Librarian | Item detail and withdraw |
| `/ui/patrons` | Librarian | Patron list |
| `/ui/patrons/new` | Librarian | Create patron |
| `/ui/patrons/{card}` | Librarian | Patron detail with active loans, holds, link/unlink user, deactivate |
| `/ui/patrons/{card}/loans` | Librarian | Full patron loan history |
| `/ui/users` | Librarian | User list |
| `/ui/users/new` | Librarian | Create user |
| `/ui/users/{username}` | Librarian | User detail — change role, deactivate |
| `/ui/policies` | Librarian | Loan policy list with inline edit |
| `/ui/policies/new` | Librarian | Create loan policy |
| `/ui/roles` | Librarian | Role list |
| `/ui/roles/new` | Librarian | Create custom role |
| `/ui/roles/{id}` | Librarian | Role detail — edit permissions, clone |
| `/ui/branches` | Librarian | Branch list with classification scheme |
| `/ui/branches/{id}/edit` | Librarian | Edit a branch's default classification scheme |
| `/ui/audit` | Librarian | Audit log viewer |

Guest catalog search is enabled by default (`COMPENDIUM_GUEST_SEARCH_ENABLED=true`).

## REST API endpoints

The API is also available at the root. Use `Authorization: Bearer <token>` from `POST /auth/login`.

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/audit` | `patron.manage` | Query audit log |
| POST | `/auth/login` | — | Obtain JWT |
| GET | `/works/search?q=` | guest / `item.view` | Search catalog |
| GET | `/items/{barcode}` | `item.view` | Item detail |
| POST | `/items/{barcode}/withdraw` | `item.delete` | Withdraw item |
| POST | `/patrons` | `patron.manage` | Create patron |
| POST | `/patrons/{card}/deactivate` | `patron.manage` | Deactivate patron |
| POST | `/loans/checkout` | `loan.checkout` | Check out |
| POST | `/loans/{id}/checkin` | `loan.checkin` | Check in |
| POST | `/loans/{id}/renew` | `loan.renew.any` | Renew (librarian) |
| POST | `/holds` | `hold.place.self` | Place hold |
| GET | `/holds?card_number=` | `hold.view.self` | List holds |
| DELETE | `/holds/{id}` | `hold.place.self` | Cancel hold |
| GET | `/policies` | `item.view` | List loan policies |
| POST | `/policies` | `policy.edit` | Create loan policy |
| POST | `/users/{username}/deactivate` | `user.manage` | Deactivate user |
| GET | `/me/loans` | `loan.view.self` | Own active loans |
| GET | `/me/holds` | `hold.view.self` | Own active holds |
| POST | `/me/holds` | `hold.place.self` | Place own hold |
| DELETE | `/me/holds/{id}` | `hold.place.self` | Cancel own hold |
| POST | `/me/loans/{id}/renew` | `loan.renew.self` | Renew own loan |

Interactive API docs are at `http://localhost:8000/docs` when the server is running.

## Configuration

Settings are read from environment variables (prefix `COMPENDIUM_`) or a `.env` file:

| Variable | Default | Description |
|---|---|---|
| `COMPENDIUM_DATABASE_URL` | `sqlite:///compendium.db` | SQLAlchemy database URL |
| `COMPENDIUM_JWT_SECRET_KEY` | *(insecure default)* | **Must be changed in production** |
| `COMPENDIUM_JWT_EXPIRE_MINUTES` | `480` | Token lifetime (8 hours) |
| `COMPENDIUM_GUEST_SEARCH_ENABLED` | `true` | Allow unauthenticated catalog search |
| `COMPENDIUM_DEFAULT_LOAN_PERIOD_DAYS` | `14` | Fallback loan period |
| `COMPENDIUM_HOLD_EXPIRY_DAYS` | `30` | Days before a waiting hold expires |
| `COMPENDIUM_HOLD_PICKUP_DAYS` | `3` | Days a patron has to collect an available hold |

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

The `compendium maintenance expire-holds` command should run periodically via cron or a systemd timer. See [`docs/crontab.sample`](docs/crontab.sample) and [`docs/compendium.service.sample`](docs/compendium.service.sample).

## Running tests

```bash
uv run pytest -q
```

Tests are split into `tests/unit/` (no DB, mock repos) and `tests/integration/` (SQLite in-memory). 311 tests as of the current build.

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
