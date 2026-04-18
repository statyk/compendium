# Compendium

A library card catalog system for physical items — books, vinyl records, DVDs, CDs.

**Status:** Active development. See [`CLAUDE.md`](CLAUDE.md) for the full design and architecture document.

## Features (current)

- **Catalog** — add items by ISBN (Open Library lookup), search, list works and items
- **Circulation** — checkout, checkin, loan renewal with configurable loan policies
- **Holds** — patron reservation queue; automatic promotion on checkin
- **Auth** — JWT-based login, role/permission model (ReadOnly, Patron, Librarian)
- **Patron self-service API** — patrons renew loans and manage holds via their own token
- **CLI** — full librarian workflow without running a server
- **REST API** — FastAPI; run with `compendium serve`

## Quick start

```bash
# Install dependencies into a project-local virtualenv
uv sync --extra dev

# Initialise the database (creates ./compendium.db on SQLite by default)
uv run compendium db init

# Add a user account
uv run compendium user add --username admin --role Librarian

# Add a book by ISBN (looks up metadata from Open Library)
uv run compendium item add --isbn 9780441013593

# Add a patron
uv run compendium patron add --name "A. Patron"

# Check it out (use the barcode reported by `item add` and the card number from `patron add`)
uv run compendium loan checkout --barcode <barcode> --card <card>

# Renew a loan
uv run compendium loan renew --barcode <barcode> --card <card>

# Check it back in
uv run compendium loan checkin --barcode <barcode>

# Place and list holds
uv run compendium hold place --work-id <id> --card <card>
uv run compendium hold list --card <card>

# View and update loan policies
uv run compendium policy list
uv run compendium policy set --id <id> --loan-days 21

# Start the API server
uv run compendium serve
```

## CLI reference

| Command | Description |
|---|---|
| `compendium db init` | Initialise / migrate the database |
| `compendium item add --isbn <isbn>` | Add an item by ISBN |
| `compendium item show --barcode <barcode>` | Show item detail |
| `compendium item list` | List all items |
| `compendium patron add --name <name>` | Add a patron |
| `compendium patron list` | List patrons |
| `compendium loan checkout` | Check out an item |
| `compendium loan checkin` | Check in an item |
| `compendium loan renew` | Renew an active loan |
| `compendium loan active --card <card>` | List active loans for a patron |
| `compendium hold place` | Place a hold |
| `compendium hold cancel` | Cancel a hold |
| `compendium hold list` | List active holds for a patron |
| `compendium policy list` | List loan policies |
| `compendium policy set` | Update a loan policy |
| `compendium maintenance expire-holds` | Expire overdue waiting holds (for cron) |
| `compendium user add` | Create a user account |
| `compendium serve` | Start the API server |

## API endpoints

| Method | Path | Permission | Description |
|---|---|---|---|
| POST | `/auth/login` | — | Obtain JWT |
| GET | `/works/search?q=` | guest / `item.view` | Search catalog |
| GET | `/items/{barcode}` | `item.view` | Item detail |
| POST | `/patrons` | `patron.manage` | Create patron |
| POST | `/loans/checkout` | `loan.checkout` | Check out |
| POST | `/loans/{id}/checkin` | `loan.checkin` | Check in |
| POST | `/loans/{id}/renew` | `loan.renew.any` | Renew (librarian) |
| POST | `/holds` | `hold.place.self` | Place hold (card number required) |
| GET | `/holds?card_number=` | `hold.view.self` | List holds |
| DELETE | `/holds/{id}` | `hold.place.self` | Cancel hold |
| GET | `/policies` | `item.view` | List loan policies |
| POST | `/policies` | `policy.edit` | Create loan policy |
| GET | `/me/loans` | `loan.view.self` | Own active loans |
| GET | `/me/holds` | `hold.view.self` | Own active holds |
| POST | `/me/holds` | `hold.place.self` | Place own hold |
| DELETE | `/me/holds/{id}` | `hold.place.self` | Cancel own hold |
| POST | `/me/loans/{id}/renew` | `loan.renew.self` | Renew own loan |

## Configuration

Settings are read from environment variables (prefix `COMPENDIUM_`) or a `.env` file:

| Variable | Default | Description |
|---|---|---|
| `COMPENDIUM_DATABASE_URL` | `sqlite:///compendium.db` | SQLAlchemy database URL |
| `COMPENDIUM_JWT_SECRET_KEY` | *(insecure default)* | **Change in production** |
| `COMPENDIUM_JWT_EXPIRE_MINUTES` | `480` | Token lifetime |
| `COMPENDIUM_GUEST_SEARCH_ENABLED` | `true` | Allow unauthenticated search |
| `COMPENDIUM_DEFAULT_LOAN_PERIOD_DAYS` | `14` | Fallback loan period |
| `COMPENDIUM_HOLD_EXPIRY_DAYS` | `30` | Days before a waiting hold expires |
| `COMPENDIUM_HOLD_PICKUP_DAYS` | `3` | Days a patron has to collect an available hold |

## Running tests

```bash
uv run pytest tests/ -q
```

## Layout

- `src/compendium/` — application code (see `CLAUDE.md` for layer breakdown)
- `migrations/` — Alembic migrations
- `tests/unit/` — unit tests (no DB, mock repositories)
- `tests/integration/` — integration tests (SQLite in-memory)

## License

MIT (assumed; finalize before first release).
