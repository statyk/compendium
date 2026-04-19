# Compendium

A library card catalog system for physical items, built in Python.

**Status:** Design phase. This document captures decisions made before any code was written. Update it as the project evolves.

**Working title:** Compendium. The parent directory is named `vibecat` (original working title); this was kept to preserve Claude Code's per-project memory store. The *project name* (package, pyproject.toml, README, GitHub repo) is Compendium.

---

## Project overview

- **Purpose:** Manage a physical collection and track item loans.
- **Target users:** Small-to-medium libraries (home, classroom, school, club). Flexible enough for mid-size institutions.
- **Media scope:** Books (primary), vinyl records, DVDs, CDs. Pure-electronic items (e-books, streaming) are out of scope, but the schema accommodates adding them later.
- **Scale target:** middle tier — 10k–500k items, 10–100 concurrent users.
- **License:** MIT (see LICENSE).
- **OS support:** Linux and macOS required. Windows is nice-to-have (pure-Python stack + browser-side scanning should make it work with zero extra effort).

## Directory layout

- `~/project/vibecat/` — Claude Code working directory (unchanged to preserve memory store).
- `~/project/vibecat/compendium/` — project root; will become the GitHub repo root.
- `~/project/vibecat/CLAUDE.md` — symlink to `compendium/CLAUDE.md` so Claude Code loads it from cwd.

Run `claude` from `vibecat/`. The code lives in `compendium/`.

---

## Architecture

Layered, with strict dependency direction. Each layer imports only from layers below it.

```
cli ─┐             web ─┐
     ├── services ──────┼── domain
api ─┘                  config
                    db ──── repositories ── domain
```

- **domain/** — plain-Python models, enums, permissions, domain exceptions. *SQLAlchemy ORM models ARE the domain models for v1* (pragmatic choice to avoid a translation layer; revisit if non-SQL repositories are ever needed).
- **repositories/** — data access interfaces and implementations.
  - `sql/` holds SQLAlchemy-based impls shared by Postgres and SQLite.
  - `search/` holds full-text search backends (Postgres tsvector/GIN; SQLite FTS5).
- **services/** — business logic. Services are classes that take repositories as constructor args. Easy to test with mock repos; easy to wire with FastAPI DI or directly from the CLI.
- **api/** — FastAPI routes, Pydantic schemas, JWT auth.
- **web/** — HTMX + Jinja templates mounted on the same FastAPI app as the API.
- **cli/** — Typer-based CLI. In v1, always uses services directly (no remote-daemon mode).
- **config/**, **db/** — wiring; compose layers into a running application.

### Dependency rules (strict)

- `domain` has no internal deps.
- `repositories` depends on `domain` (+ SQLAlchemy).
- `services` depends on `domain` and `repositories`.
- `api`, `web`, `cli` depend on `services` and `domain`; never cross-import each other.
- `config` and `db` are wiring — only imported by composition code.

Enforced by review in v1. Add an `import-linter` rule later if drift becomes a concern.

---

## Deployment modes

One installed command (`compendium`), three behaviors:

1. **Library mode** — `compendium <subcommand>` runs the CLI, imports services directly, hits the DB, exits. No daemon required. Good for home use and scripting.
2. **Daemon mode** — `compendium serve` starts FastAPI (both API routes and web UI). Required whenever a non-CLI UI is in use.
3. **Both** — daemon for the web UI, CLI in library mode for admin scripts. Common in practice.

### Scheduled tasks

Scheduled maintenance is implemented as **CLI subcommands invoked externally by cron, systemd timers, or Task Scheduler** — *not* run inside the daemon.

Examples: `compendium maintenance prune-loan-history`, `compendium loans mark-overdue`, `compendium holds expire`.

Rationale:
- Tasks run even when the daemon is down or absent.
- Each task is manually runnable and inspectable by a librarian.
- CLI-only deployments still get maintenance.
- cron/systemd/Task Scheduler are universal and already understood by sysadmins.

Ship sample crontab entries and a systemd unit file in `docs/`.

### CLI / Web UI feature parity

Feature parity between the CLI and Web UI is a **design goal**. Any operation a
librarian or patron can perform in one interface should be available in the
other, so that CLI-only and daemon deployments are both fully usable.

Approved exceptions (CLI-only by design):
- `db init` / `db upgrade` / `db history` — deployment-time migrations.
- `maintenance *` subcommands — invoked externally by cron/systemd, not from
  the running daemon.

Approved exceptions (Web-only by design):
- `/ui/login` / `/ui/logout` — CLI trust is OS-level; no login flow needed.

Any other gap should be closed or explicitly added to this list with a
rationale. When adding a new feature, implement it on both interfaces (or
open the parity exception explicitly) in the same slice.

---

## Tech stack

- **Python 3.11+**
- **FastAPI** — web API framework
- **SQLAlchemy 2.0** (with `Mapped[...]` style) — DB access (Postgres, SQLite)
- **Alembic** — migrations
- **Typer** — CLI
- **HTMX + Jinja** — web UI (v1)
- **Browser BarcodeDetector API + ZXing-js fallback** — barcode scanner; runs client-side, no video stream to server
- **Auth:** leaning on `fastapi-users` with JWT; may hand-roll if scope lets us
- **uv** — packaging / dependency management
- **pytest** — testing

### Scale-driven backend choice (documented for users)

| Backend | Collection | Concurrent writers | Fit |
|---|---|---|---|
| SQLite | up to ~10k items | 1–2 | Home, classroom, very small school |
| Postgres | up to ~500k items | 10–100 | Schools, mid-size institutional |
| Postgres + external search (e.g. Meilisearch) | beyond | — | Large institutional (future) |

Repository abstraction means scaling up later doesn't require rewriting service code.

---

## Data model

### Entities

- **Work** — abstract title. One row per ISBN/UPC (practical simplification; FRBR Work/Expression/Manifestation are collapsed here). Holds title, subtitle, publisher, classification, language, description, external IDs (JSONB), media-specific metadata (JSONB).
- **Item** — physical copy. Barcode, accession_number, call_number, location, condition, status, branch.
- **Creator** — author, director, artist, etc. Linked to Work via `work_creator` junction with a `role` column (same person can be author of one work and editor of another).
- **Patron** — borrower. `user_id` is **nullable** → card-only patrons supported for home/classroom flows where kids/guests borrow without accounts.
- **User** (`app_user` in DB; `user` is a reserved word) — auth identity with a role.
- **Role** — named preset; permissions stored as a JSONB string array (e.g., `["item.view", "loan.checkout"]`). `is_system` flag locks preset roles from edits.
- **Loan** — item → patron, with due date. Active = `returned_at IS NULL`. Single table; history lives in the same table with a partial index on the active subset.
- **Hold** — reservation on a Work (any physical copy satisfies; "hold specific copy" is not supported in v1). Note: different editions have different ISBNs and land in different Work rows, so a Work-level hold is effectively an *edition-level* hold.
- **Branch** — included from day one even for single-branch deployments (every Item/Loan/Hold carries `branch_id`). UI hides the picker in single-branch mode. Multi-branch *features* (transfers, inter-branch holds, per-branch policies) are deferred.
- **MediaType** — reference table (`book`, `vinyl`, `dvd`, `cd`, ...). Drives metadata schema expectations and which external lookup source to use.
- **LoanPolicy** — loan period, renewal limits, per-media-type rules.
- **AuditLog** — simple synchronous audit trail for Librarian-level mutations (create / update / delete of items, works, policies, roles, patron records). Routine circulation is NOT logged — loan rows already carry that history. Stores `user_id`, `entity_type`, `entity_id`, `action`, `details` (JSONB), `occurred_at`.

### ID strategy

- Integer primary keys (`bigserial`) throughout.
- Users see external-facing identifiers only: barcode, accession_number, library_card_number, ISBN/UPC. Internal row IDs are never exposed.

### Key indexes

- Partial indexes on active loans (`WHERE returned_at IS NULL`) for item, patron, and due_at lookups.
- Partial index on waiting holds (`WHERE status = 'waiting'`).
- GIN index on Work full-text (Postgres); FTS5 virtual table (SQLite).
- Unique constraints on `item.barcode`, `item.accession_number`, `patron.library_card_number`, `app_user.username`, `role.name`.
- Conditional indexes on `work.isbn` and `work.upc` where non-null.

Full DDL lives in `docs/schema.md` (to be written alongside initial migrations).

---

## Permissions model

Permissions are atomic strings stored in a JSONB array on each Role. Roles are named bundles.

### Example permission strings

`item.view`, `item.create`, `item.edit`, `item.delete`, `loan.checkout`, `loan.checkin`, `loan.renew.self`, `loan.renew.any`, `hold.place.self`, `hold.place.any`, `policy.edit`, `patron.manage`, `user.manage`, `role.manage`.

The `.self` vs `.any` scope convention handles patron self-service (a patron can renew their own loan but not anyone else's).

### Preset roles shipped

- **ReadOnly** — search and view the catalog only.
- **Patron** — ReadOnly + self-service (renew own loans, place own holds, view own loan history).
- **Librarian** — full access.

Librarians can create and edit custom roles via the admin UI. Preset roles are marked `is_system = true` and cannot be edited (but can be cloned).

### Guest access

Unauthenticated read-only search is **optional**, controlled by a config setting (`COMPENDIUM_GUEST_SEARCH_ENABLED`, default `true`). When enabled, search endpoints accept anonymous requests. When disabled, all search endpoints require authentication.

Implementation: search routes use a single FastAPI dependency (`search_request_user`) that consults the config and either requires a valid token or returns `None` for anonymous callers. No per-route changes are needed when the setting flips.

Future: when we add a `site_setting` table for runtime-adjustable knobs (loan-history retention, default pickup branch, SMTP config, etc.), this setting moves from env/file to DB so a librarian can flip it from the admin UI without restarting the daemon.

---

## External metadata sources

Used to populate Work records when adding new items by ISBN/UPC/other identifier.

- **Books:** Open Library (primary, CC0, no API key required). Google Books (fallback, free tier, requires key).
- **Music:** MusicBrainz (CC0). Discogs (vinyl/CD detail, requires key).
- **Film/TV:** TMDb (requires key). OMDb (limited free tier).

All return permissively-licensed data; no usage restrictions relevant to an open-source distribution.

---

## Classification systems

The `Work` schema is deliberately classification-neutral: `classification_scheme` and `classification_code` are free-form strings. Compendium does not bundle or distribute any classification tables.

- **LCC (Library of Congress Classification)** — the project's recommended default. US federal work, public domain (17 U.S.C. § 105). Free to use, redistribute, and build tools against.
- **DDC (Dewey Decimal Classification)** — copyrighted and trademarked by OCLC. Individual classification numbers assigned to specific books are bibliographic facts and safe to store; the DDC *tables*, *relative index*, and *branded tooling* are not. Institutional users with an OCLC subscription can enter DDC numbers via the free-form field; Compendium will never ship DDC reference data or a "Dewey picker."
- **Other schemes** — UDC (licensed), BISAC (licensed), custom/in-house labels all work because the field accepts any string.

Rule of thumb: fetching and storing a classification number for a specific book is fine. Shipping a classification *system* (lookup tables, categorizers, editorial content) is not — unless it's LCC or user-contributed.

---

## Licensing conventions

- Prefer permissive licenses (MIT, Apache-2.0, BSD) for dependencies.
- **GPL-family dependencies require explicit user approval before adding.** This includes LGPL — dynamic linking is usable in principle but the interaction with an open-source distribution plan still deserves a conversation.
- Known-fine deps: FastAPI, SQLAlchemy, Typer, Jinja, HTMX, ZXing-js, Alembic (MIT/BSD/Apache-2.0).

---

## Scope boundaries (v1)

### In scope
- Core catalog CRUD, circulation (checkout/checkin/renew), holds, loan policies.
- Authentication + flexible permission model.
- Guest read-only search.
- Barcode scanning (browser-side).
- External metadata lookup (Open Library primary).
- Multi-media-type support (books, vinyl, DVDs, CDs).
- SQLite + Postgres backends.
- CLI + web UI.
- Simple synchronous audit log (Librarian mutations only).
- Cron-invoked maintenance commands.

### Out of scope for v1, but accommodated by schema / design
- Fines (data fields may be reserved; feature deferred).
- Notifications (e.g., overdue emails) — data fields reserved; feature deferred.
- MARC / CSV bulk import/export.
- Multi-branch *features* (transfers, inter-branch holds, per-branch policies — schema is ready).
- Tags, series (e.g. "Book 3 of Foundation"), item-level images, patron reviews.
- E-books and streaming media.
- Third-party plugin discovery for backends/UIs (later via Python `entry_points`).
- CLI-to-remote-daemon REST mode (currently CLI always uses services directly).
- **Barcode label / patron card PDF generation** — feasible with `python-barcode` (MIT) + `reportlab` (BSD), both permissive. New `services/labels.py`, CLI `compendium item labels --template <avery-5160|...> --output labels.pdf` and equivalent for patron cards; web UI form later. Ship dimensions for a few common sheet layouts (no vendor trademarks bundled); let users add custom templates via config. Include a "start at label N" option for partial sheets. Code128 for internal barcodes, EAN-13 if ever printing ISBN labels. Make the PDF libs an optional extra (`uv sync --extra labels`) — reportlab is ~3MB. No printer integration — PDF-to-printer is the OS's job. Revisit after core (Postgres, admin UI, or bulk import) is nailed down.
- **UPC-to-title bridge for film metadata** — TMDb does not index physical-disc UPCs, so slice 10b ships with title-search + candidate-picker for DVD/Blu-ray/VHS. A future enhancement: scan UPC → hit a bridge service (e.g., UPCitemdb free tier) → extract product name → strip format/edition cruft ("[Blu-ray]", "Steelbook", "4K UHD") via heuristics → feed cleaned title into TMDb search. Restores barcode-scan parity with books/music. Costs: third-party dependency with its own rate limits and reliability, fragile title-cleaning heuristics, spotty coverage on older VHS barcodes. Same bridge pattern could later help Discogs fallback for obscure music releases.

---

## Package layout

```
compendium/
├── pyproject.toml
├── README.md
├── LICENSE
├── CLAUDE.md
├── docs/
│   ├── architecture.md
│   ├── schema.md
│   ├── deployment.md
│   ├── crontab.sample
│   └── compendium.service.sample
├── migrations/                      # Alembic
├── tests/
│   ├── unit/                        # no DB; mock repos
│   ├── integration/                 # SQLite in-memory
│   └── e2e/                         # hits a running daemon
└── src/
    └── compendium/
        ├── __init__.py
        ├── __main__.py
        ├── domain/                  # models, enums, permissions, errors
        ├── repositories/
        │   ├── base.py
        │   ├── sql/                 # SQLAlchemy impls (PG + SQLite)
        │   └── search/              # tsvector (PG) / FTS5 (SQLite)
        ├── services/                # catalog, circulation, patrons, users, auth,
        │                            # policies, metadata, maintenance
        ├── api/                     # FastAPI app + routes + schemas + security
        ├── web/                     # HTMX + Jinja templates + scanner JS
        ├── cli/                     # Typer commands (catalog, circulation,
        │                            # patrons, users, maintenance, serve, db)
        ├── config/                  # Pydantic settings + seed data
        └── db/                      # engine factory + session lifecycle
```

---

## Conventions

- **Testing:** unit (no DB, mock repos), integration (SQLite in-memory), e2e (hits daemon). `pytest`.
- **Style:** ruff + black formatting; type hints throughout. Domain and services should pass strict mypy.
- **Commits:** conventional commits preferred, not enforced. Never rewrite pushed history without explicit approval.
- **Editing preference:** edit existing files over creating new ones when possible; don't add abstractions or error handling for scenarios that can't happen.
- **Dependencies:** don't introduce new runtime deps casually — flag them in chat before adding to `pyproject.toml`.

---

## Open decisions / later

- Package manager: `uv` (decided — in use).
- Auth implementation: hand-rolled (decided in slice 2: PyJWT + bcrypt; `fastapi-users` would fight our custom permission model).
- Whether to split domain models out of SQLAlchemy later.
- Default loan-history retention policy (opt-in via config setting; the maintenance prune command honors it).
- Whether to add `import-linter` for dependency-rule enforcement.
- Whether to add entry-point-based plugin discovery for backends (deferred until someone asks for it).

### Known technical debt (slices 1–2)

- Datetimes stored as naive UTC (SQLite limitation). When Postgres support is added, revisit with timezone-aware storage and a consistent conversion layer.
- The API test fixtures use a module-scoped `StaticPool` engine (separate from the integration test engine) to avoid SQLite in-memory thread-isolation issues with FastAPI's thread pool. This is correct but means API tests accumulate committed data within the module; tests are written to be independent despite this.

### Deferred from AuditLog slice (slice 7)

- **Audit log viewer (web/API)** — web page at `/ui/audit` and REST endpoint `GET /audit` both complete.
- **Audit log retention/prune** — log grows without bound. Add `compendium maintenance prune-audit-log --older-than-days N` when a retention policy is decided.
- **Login/auth event logging** — excluded per spec ("Librarian mutations only"). Revisit if security requirements change.
- **Role CRUD** — wired in slice 9b.4.
- **Audit CLI viewer filters** — `compendium audit list` supports `--entity`, `--id`, `--user`, `--limit` for now. Date-range filtering, CSV export, and paging deferred.

### Deferred from barcode-scanning slice (slice 8)

- **Automated Let's Encrypt / ACME** — `compendium serve` accepts `--ssl-certfile` / `--ssl-keyfile` (or `COMPENDIUM_SSL_*` env vars) for manually-provisioned certs. For automated renewal the deployment docs recommend Caddy. Building an in-process ACME client is deferred indefinitely.
- **Scanner settings UI** — beep-on-scan is a per-browser localStorage toggle inside the scanner dialog. If we later add a `site_setting` table, a server-side default could layer in under the same contract.
- **Scanning elsewhere** — wired on circ desk and item-add page (`/ui/items/new`). Catalog-search-by-scan remains deferred.
- **Scanner testing** — no automated tests for the JS. Manually verified on iOS Safari (ZXing path), Android Chrome (native `BarcodeDetector`). USB keyboard-wedge scanners are untested but expected to work since they're keyboard input.

---

## How to pick up this project (future sessions)

If you're a future Claude session joining this project:

1. Read this file first.
2. Run `git log --oneline` to see recent progress.
3. Run `uv run pytest` — all tests should pass before making changes.
4. **Current status (last updated 2026-04-19):** slices 1–11c complete (v1 feature-complete). Slice 9b.1 added patron↔user linking: `PatronService.link_user/unlink_user` enforce 1:1 at service level; web dropdown on patron create form; link/unlink actions on patron detail; CLI `patron --link-user`, `patron link-user`, `patron unlink-user`. Slice 9b.2 added user management web: `/ui/users` list, `/ui/users/new` create form, `/ui/users/{username}` detail with role change (form POST + redirect) and HTMX deactivate; `AuthService.update_role/list_users`; `SqlUserRepository.list()`; `SqlRoleRepository.list()`. `Patron.user` lazy-loaded relationship added. Slice 9b.3 added policy management web: `/ui/policies` list with inline per-row edit form, `/ui/policies/new`; `PolicyService.update()` accepts `is_default` with atomic swap via `LoanPolicyRepository.clear_defaults()`; enforces at-least-one-default invariant; CLI `policy create` and `policy set --default/--no-default`. Slice 9b.4 added role management web: `/ui/roles` list, `/ui/roles/new`, `/ui/roles/{id}` with grouped permission picker (Catalog/Loans/Holds/Administration) and Full access checkbox storing `["*"]`; preset roles (`is_system=True`) read-only enforced at service level; Clone button creates editable copy; role detail shows assigned users; new `RoleService` in `services/roles.py`; `SqlRoleRepository.update()` added; `AuditEntityType.ROLE` wired; nav refactored to per-permission `has_permission()` Jinja global; CLI `role list/create/update/clone` and `user set-role`. Cleanup slice added: `MediaTypeRepository` protocol + `SqlMediaTypeRepository` — `CatalogService._create_work()` no longer reaches into `item_repo._s`; `GET /audit` REST endpoint (permission: `patron.manage`, filters: entity_type/entity_id/user_id/limit); `user_id` filter added to web audit viewer; `COMPENDIUM_JWT_SECRET_KEY` insecure-default warning emitted at startup via `logging.warning("compendium")`. CLI covers: `audit list`, `db init`, `item add --isbn`, `item show/list`, `item withdraw --barcode`, `patron add [--link-user]`, `patron link-user`, `patron unlink-user`, `patron list`, `patron deactivate --card`, `loan checkout/checkin/renew/active`, `hold place/cancel/list`, `policy list/create/set`, `role list/create/update/clone`, `maintenance expire-holds`, `user add`, `user set-role`, `user deactivate --username`, `serve`. FastAPI routes: `GET /audit`, `POST /auth/login`, `GET /works/search`, `GET /items/{barcode}`, `POST /items/{barcode}/withdraw`, `POST /patrons`, `POST /patrons/{card_number}/deactivate`, `POST /loans/checkout`, `POST /loans/{id}/checkin`, `POST /loans/{id}/renew`, `POST /holds`, `GET /holds`, `DELETE /holds/{id}`, `GET /policies`, `POST /policies`, `POST /users/{username}/deactivate`. Self-service: `GET /me/loans`, `GET /me/holds`, `POST /me/holds`, `DELETE /me/holds/{id}`, `POST /me/loans/{id}/renew`. Web UI at `/ui/*`: login/logout, catalog search, work detail + place-hold + add-copy, item detail + withdraw, add item by ISBN + scanner, circ desk + scanner, patron list + create (with user link dropdown) + detail (with link/unlink) + deactivate, patron self-service loans/holds, user list + create + detail (role change + deactivate), policy list (inline edit) + new, role list + new + detail (edit/clone), audit log viewer. Key notes: `Patron.user` is lazy-loaded (no back_populates). Detail pages accept `?message=`/`?error=` query params for post-redirect feedback. `PERMISSION_GROUPS` constant in `web/routes/roles.py` is the canonical permission list for the picker. SQLite + Postgres backends. FTS search via SQLite FTS5 and Postgres tsvector/GIN. 224 tests passing (210 SQLite + 14 Postgres via testcontainers).
5. Logical next steps (discuss with user before starting): additional media types / metadata sources, MARC/CSV bulk import, barcode label PDF generation.
6. Design decisions on this page are settled unless the user opens them again. When a prior decision seems wrong, raise it for discussion rather than quietly overriding it.
