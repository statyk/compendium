# Architecture

## Overview

Compendium is a layered Python application with a strict one-way dependency direction. Each layer imports only from layers below it; no upward or sideways imports are allowed.

```
cli ─┐             web ─┐
     ├── services ──────┼── domain
api ─┘                  config
                    db ──── repositories ── domain
```

---

## Layers

### domain/

Plain Python models, enums, domain exceptions, and permission strings. No framework dependencies.

The SQLAlchemy ORM models **are** the domain models (pragmatic v1 choice to avoid a translation layer). If a non-SQL repository is ever needed, domain models can be split from ORM models at that point.

Key files:
- `models.py` — all SQLAlchemy `Mapped[]` model classes
- `enums.py` — `ItemStatus`, `HoldStatus`
- `errors.py` — `DomainError`, `NotFoundError`, `BusinessRuleError`, `AuthError`, `ExternalLookupError`

### repositories/

Data access interfaces (protocols) and implementations.

- `base.py` — `@runtime_checkable` Protocol classes for every repository type
- `sql/` — SQLAlchemy implementations shared by Postgres and SQLite
- `search/` — placeholder for future dedicated search backend classes

The SQL implementations handle both Postgres and SQLite. Dialect-specific behavior (FTS query style, JSON operators) is handled with `session.connection().dialect.name` checks inside the repository.

### services/

Business logic. Services are plain classes whose constructors accept repository protocol types — easy to test with mocks and easy to wire in any context (FastAPI DI, CLI, tests).

| Service | Responsibility |
|---------|---------------|
| `CatalogService` | Add works from ISBN/UPC/MBID/TMDb/title search, add manual items, withdraw items |
| `CirculationService` | Checkout, checkin, renew |
| `HoldService` | Place, cancel, expire holds |
| `PatronService` | Patron CRUD, user linking |
| `AuthService` | Login, JWT issuance, password hashing, user/role management |
| `PolicyService` | Loan policy CRUD |
| `RoleService` | Role CRUD, clone |
| `AuditService` | Append audit log entries |
| `metadata` module | External lookup (Open Library, MusicBrainz, TMDb) |

Services **never** import from `api/`, `web/`, or `cli/`. Config access in services is limited to `os.getenv()` for external API keys (documented architectural compromise).

### api/

FastAPI routes, Pydantic request/response schemas, JWT middleware, and dependency injection wiring. Mounts at `/` (API routes) alongside the web UI.

### web/

HTMX + Jinja2 templates. Routes mount at `/ui/*`. Uses the same FastAPI app instance as the API layer via `create_app()`. The scanner uses the browser's BarcodeDetector API with a ZXing-js fallback — no video stream is sent to the server.

### cli/

Typer-based CLI. Always uses services directly (no remote-daemon mode in v1). Entry point: `compendium`.

### config/

Pydantic `Settings` (reads from env vars with `COMPENDIUM_` prefix and `.env` file) and seed data applied at startup.

### db/

Engine factory (`make_engine()`) and session lifecycle (`session_scope()`, `get_session()` FastAPI dependency). The engine is dialect-aware: SQLite gets `check_same_thread=False`; Postgres gets connection pool tuning and `pool_pre_ping`.

---

## Dependency rules

| Layer | May import from |
|-------|----------------|
| domain | (nothing internal) |
| repositories | domain |
| services | domain, repositories |
| api | services, domain |
| web | services, domain |
| cli | services, domain |
| config | (nothing internal) |
| db | config, domain |

Violations are caught in code review. An `import-linter` rule can be added if drift becomes a concern.

---

## Deployment modes

One installed command (`compendium`), three behaviors:

**Library mode** — `compendium <subcommand>` imports services directly, hits the DB, exits. No daemon required. Good for home use and scripting.

**Daemon mode** — `compendium serve` starts uvicorn with FastAPI (both API routes and the web UI). Required for browser access.

**Both** — daemon for the web UI, CLI for admin scripts. Common in practice.

### Scheduled maintenance

Maintenance tasks are CLI subcommands invoked externally by cron, systemd timers, or Windows Task Scheduler — not run inside the daemon. This means tasks run even when the daemon is down, are manually runnable, and work for CLI-only deployments. See `docs/crontab.sample` and `docs/compendium.service.sample`.

---

## Database backends

| Backend | Fit | Notes |
|---------|-----|-------|
| SQLite | Home, classroom, small school (up to ~10k items, 1–2 concurrent writers) | Default; zero config |
| Postgres | Schools, mid-size institutional (up to ~500k items, 10–100 concurrent writers) | Optional dep: `psycopg[binary]` |

The same repository code targets both backends. Dialect-specific features:
- **FTS:** SQLite uses an FTS5 virtual table with triggers; Postgres uses a GIN index on `to_tsvector()`
- **JSON:** `sa.JSON` maps to SQLite TEXT and Postgres JSONB
- **Timestamps:** stored as tz-aware UTC end-to-end. A `UtcDateTime` type decorator normalizes the SQLite round-trip (SQLite's SQLAlchemy dialect otherwise strips tzinfo); Postgres uses native `timestamptz` and is unaffected

### Search behavior

The catalog search box has two modes, and they match differently:

- **All Fields** (default) runs full-text search — whole tokens only, with stemming. `"civil"` matches `"Civil War"`; `"civ"` does **not**.
- **Field-scoped** (Title, Author, Publisher, …) uses substring matching. `"civ"` does match `"Civil"`.

If a partial-word query returns nothing from the default box, switch to a field-scoped search. This asymmetry is a known quirk; we're leaving it as-is until real-world feedback says otherwise.

### Migrating from SQLite to Postgres

Currently a manual data migration (dump/restore). A `compendium db migrate-to-postgres` command is deferred.

---

## Authentication

Hand-rolled JWT auth using PyJWT + bcrypt. `fastapi-users` was considered but would fight the custom permission model.

Tokens carry `sub` (user ID), `username`, `role`, and `permissions` (full permission list). The permission list is embedded so routes don't need a DB round-trip per request.

Permission strings follow `entity.action[.scope]` convention: `item.view`, `loan.renew.self`, `loan.renew.any`. The `.self` vs `.any` scope handles patron self-service.

Guest search is controlled by `COMPENDIUM_GUEST_SEARCH_ENABLED` (default `true`).

---

## External metadata

When adding items by identifier, Compendium looks up metadata from external sources:

| Media type | Primary source | Identifier |
|-----------|---------------|------------|
| book | Open Library (CC0, no key) | ISBN or title search |
| vinyl, cd | MusicBrainz (CC0, no key) | UPC, MBID, or title search (format-filtered, artist+title fuzzy) |
| dvd, bluray, vhs | TMDb (requires key) | TMDb ID or title search |

For items that external sources can't find (zines, obscure self-releases, out-of-print rarities), `CatalogService.add_manual()` accepts user-supplied metadata directly. Exposed via the web UI at `/ui/items/new/manual` and the CLI `compendium item add-manual`.

Adapters are registered in `services/metadata.py` via `_ADAPTERS: dict[str, MetadataAdapter]`. Adding a new media type requires implementing the `MetadataAdapter` protocol and registering it.

Note: TMDb does not index physical-disc UPCs, so film items are added via a title-search candidate picker rather than direct barcode scan. A UPC→title bridge is a deferred enhancement.

---

## Bulk import & export

`services/import_export.py` provides bulk ingest and extract for CSV, MARC21 binary (`.mrc`), and MARCXML. Surfaced on all three interfaces:

- CLI: `compendium import {csv|marc} <file>` and `compendium export {csv|marc} <out>` (with `--xml` for MARCXML on export).
- API: `POST /import/{csv,marc}` (multipart) and `GET /export/{csv,marc}?xml=…` (streaming). Import requires the `catalog.import` permission; export is gated on `item.view`.
- Web: `/ui/admin/import` (upload with dry-run preview + apply) and `/ui/admin/export` (filter form → download).

**Semantics:**

- **Dedup** by ISBN/UPC only; no fuzzy title matching. Conflict modes: `append` (add a copy to the existing work — default), `skip-duplicates`, `error-on-conflict`.
- **Barcode generation** uses the same sequential `_next_accession()` as regular adds; an optional `--barcode-prefix` tags auto-generated barcodes for a batch (prefix applies only to barcode, not accession_number).
- **Transaction ownership**: the importer flushes but never commits — the caller's session scope (or test fixture) controls commit/rollback. Dry-run rolls back. Per-row errors are collected into an `ImportReport`; barcode/accession uniqueness is pre-validated at the application layer to avoid IntegrityError rollbacks mid-batch.
- **Audit**: one summary `BULK_IMPORT` AuditLog entry per run (not per row), carrying counts and filename.

### CSV schema (item-centric)

One row per physical copy. Work metadata repeats across copies. Authors are `;`-delimited; a `:role` suffix per name overrides the default role for the media type (e.g. `Ridley Scott:director`). Columns:

```
media_type, title, subtitle, authors, publisher, publication_year,
isbn, upc, classification_scheme, classification_code, description, language,
barcode, accession_number, branch, call_number, condition, location,
is_loanable, loan_restriction_reason, loan_restriction_note
```

Only `title` is required on import. `media_type` is required unless `--default-media-type` is supplied. Unknown columns on import are ignored (so CSVs from other tools work); unknown-but-expected fields default to empty on export.

### MARC mapping

- `245$a/b` ↔ title / subtitle (ISBD punctuation stripped on read, re-added on write).
- `020$a` ↔ ISBN; `024$a` (ind1=1) ↔ UPC.
- `050$a` ↔ LCC classification; `082$a` ↔ DDC classification. Import prefers 050 over 082 when both present.
- `100/110/111` → main creator (default role `author`); `700/710/711` → added entries (default role `contributor`); `$e` subfield overrides the role.
- Leader position 6 + field 007 → `media_type`: `a/t`→book, `j` + 007 disambiguation→cd/vinyl, `g` + 007 disambiguation→dvd/bluray/vhs. Unmappable records fall back to `--default-media-type` or are reported as errors.
- `001/003` round-trip through `work.external_ids["marc_control"]` and `marc_agency`.

**MARC export is standards-compliant**: item-level fields (barcode, branch, loanable state, notes) are **not** written to MARC records. Round-tripping through MARC discards those fields by design; use CSV for lossless round-trip.

---

## Testing strategy

| Level | Location | Scope |
|-------|----------|-------|
| Unit | `tests/unit/` | No DB; pure logic, mock repos |
| Integration (SQLite) | `tests/integration/` | In-memory SQLite; full service + repo stack |
| Integration (Postgres) | `tests/postgres/` | testcontainers Postgres; skipped if Docker unavailable |
| E2E | `tests/e2e/` | Hits a running daemon; not yet written |

Tests use `pytest`. The `session` fixture provides a fresh SQLite session per test (rolled back on teardown). The `pg_session` fixture spins up an ephemeral Postgres container for the duration of the test session.

All API test modules create their own `StaticPool` engine (separate from the integration engine) to avoid SQLite in-memory thread-isolation issues with FastAPI's thread pool.
