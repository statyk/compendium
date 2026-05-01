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
| `CatalogService` | Add works from ISBN/UPC/MBID/TMDb/title search, add manual items, withdraw items, refresh metadata for existing works |
| `CirculationService` | Checkout, checkin, renew, declare-lost, mark-damaged, claims-returned |
| `HoldService` | Place, cancel, expire, suspend/resume holds; immediate promotion |
| `FineService` | Assess/pay/waive fines; overdue materialization; threshold-based blocking |
| `PatronService` | Patron CRUD, user linking, category assignment, expiry handling |
| `PatronCategoryService` | Patron category CRUD |
| `AuthService` | Login, JWT issuance, password hashing, user/role management |
| `PolicyService` | Loan policy CRUD (per media type ± patron category) |
| `RoleService` | Role CRUD, clone (preset roles are `is_system=true` and clone-only) |
| `AuditService` | Append audit log entries; queryable via web/CLI/API |
| `NotificationService` | Outbox-pattern email queue + drainer (hold-ready/due-soon/overdue) |
| `ReportsService` | Checkouts/popular/dormant/overdues queries with CSV + chart data shaping |
| `LabelsService` | PDF generation (Avery item labels + patron cards via reportlab) |
| `BackupService` | Portable JSONL tarballs, backend-agnostic restore (SQLite ↔ Postgres) |
| `CoversService` | On-disk cover proxy cache with allowlist + LRU eviction |
| `SettingsRegistry` + `site_settings` | DB-editable settings with env-wins-on-read overrides |
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

Engine factory (`make_engine()`) and session lifecycle (`session_scope()`, `get_session()` FastAPI dependency). The engine is dialect-aware:

- **SQLite** gets `check_same_thread=False` and a `connect`-time pragma listener that sets `journal_mode=WAL`, `busy_timeout=5000`, and `synchronous=NORMAL`. The pragmas are necessary for multi-process safety: cron-driven maintenance commands run as separate Python processes against the same DB file as the daemon, and the default rollback-journal mode + zero `busy_timeout` would surface as `database is locked` errors on contention. WAL is silently a no-op on `:memory:` databases (used in tests) — applies to file-backed SQLite only.
- **Postgres** gets connection pool tuning (`pool_size=5`, `max_overflow=10`) and `pool_pre_ping=True`.

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

Use `compendium backup --output backup.tar.gz` on the SQLite source, then `compendium restore backup.tar.gz` against a fresh Postgres instance — the JSONL backup format is backend-agnostic, so this doubles as the official SQLite→Postgres migration path. (Or stream directly: `compendium backup -o - | COMPENDIUM_DATABASE_URL=postgresql://... compendium restore -`.)

---

## Authentication & authorization

Hand-rolled JWT auth using PyJWT + bcrypt. `fastapi-users` was considered but would fight the custom permission model.

Tokens carry `sub` (user ID), `username`, `role`, and `permissions` (full permission list). The permission list is embedded so routes don't need a DB round-trip per request.

Permission strings follow `entity.action[.scope]` convention: `item.view`, `loan.renew.self`, `loan.renew.any`. The `.self` vs `.any` scope handles patron self-service.

### Preset roles

Five roles seed at startup; they are all `is_system=true` and can't be edited in place (clone to customize):

| Role | Permissions | Use case |
|---|---|---|
| **Administrator** | `["*"]` (wildcard) | Single-person deployments where one person does everything |
| **SystemAdmin** | `system.manage`, `user.manage`, `role.manage`, `audit.view`, plus minimal view perms (`item.view`, `work.view`) | Multi-person shops — IT/sysadmin seat. Manages users, roles, infra settings |
| **Librarian** | All catalog/circ/holds/fines/notifications/reports/labels/audit perms + `patron.manage`, `policy.edit`, `branch.edit`. **No** `user.manage` / `role.manage` / `system.manage`. | Multi-person shops — day-to-day operational seat |
| **Patron** | Self-service: `loan.view.self`, `loan.renew.self`, `hold.place.self`, `hold.view.self`, `fine.view.self`, `loan.claim.self` | Library members with a login account |
| **ReadOnly** | `item.view`, `work.view` | Anonymous catalog browsing or read-only auditors |

A startup warning logs if no active user holds `system.manage` — best-effort, doesn't block startup.

Guest catalog search is controlled by the `guest_search_enabled` site setting (env: `COMPENDIUM_GUEST_SEARCH_ENABLED`, default `true`).

### CSP and inline scripts

`_SecurityHeadersMiddleware` (`api/app.py`) generates a fresh CSP nonce per request via `secrets.token_urlsafe(16)`, stamps it on `request.state.csp_nonce`, and emits `script-src 'self' 'nonce-{nonce}' 'strict-dynamic'`. There is no `'unsafe-inline'` for scripts — a comment-field XSS that slips past output sanitization can't smuggle a `<script>` block because nonces are unguessable per request.

**Convention for inline scripts in templates.** Every `<script>...</script>` block in `web/templates/` must include `nonce="{{ csp_nonce(request) }}"`. External `<script src="/ui/static/...">` tags don't need a nonce — they match `'self'`. The `csp_nonce()` Jinja global is registered in `web/jinja.py` and reads from `request.state`.

A pytest test (`tests/integration/test_csp_nonce.py::test_every_inline_script_in_templates_has_nonce`) walks the templates directory and fails if any inline script is missing a nonce. Miss one in a future template and the test catches it before the page silently breaks in a browser.

**Style-src** still allows `'unsafe-inline'` because templates use `style="..."` attributes throughout. CSS-based attacks (data exfil via crafted selectors) are real but much lower impact than script execution; tightening that would be a separate, larger refactor.

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

## Fines & fees

`services/fines.py` manages the Fine lifecycle (assess → outstanding → paid/waived), and `services/circulation.py` is extended with `declare_lost`, `mark_damaged`, `clear_damage`, `clear_lost` that couple item-status transitions to fee assessment.

### Model

- `Fine` table with FK to `patron` (required), optional `loan_id` / `item_id`, `kind` (enum), `amount_cents`, `status` (enum), timestamps, free-text `reason` and `note`, and `resolved_by_user_id`. Partial unique index ensures at most one outstanding `overdue` fine per loan.
- `LoanPolicy` gained nullable columns: `overdue_fine_per_day_cents`, `overdue_fine_cap_cents`, `grace_period_days` (default 0), `lost_item_default_cents`, `lost_item_processing_fee_cents`. None/0 on the per-day rate means "no overdue fines for this policy," which preserves pre-slice behavior for un-configured deployments.
- `ItemStatus` extended with `lost` and `damaged`.

### Assessment model

Hybrid: Fine rows are materialized only at moments of truth — checkin (if late), `declare_lost`, `mark_damaged`, or manual `assess`. For active overdue loans that haven't been booked, the patron fines page and circ desk show a **projected** amount computed on demand. A librarian can materialize pre-return:

- **Per-patron** via the UI (`Book overdue fines` button on patron fines page), API (`POST /patrons/{card}/fines/assess-overdue`), or CLI (`compendium fine assess-overdue --patron CARD`).
- **Bulk** via CLI only (`compendium maintenance assess-overdue-fines` — cron/systemd-friendly, CLI-only by our parity exception for maintenance commands).

`FineService.assess_overdue_fines(patron_id=None)` is idempotent: creates the outstanding `overdue` Fine if absent, updates its amount if stale, never touches paid/waived fines.

### Days-overdue calculation

Whole elapsed days in UTC: `max(0, (now - due_at).days)`. Sub-day portions don't count, so a patron returning within 24h of `due_at` owes 0 days. This is predictable and avoids a timezone-configuration rabbit hole; a future slice can add library-local-tz rounding if needed.

### Threshold-based blocking

Two site-settings (DB-editable via `/ui/admin/settings/circulation`; env vars still win):

- `fine_block_threshold_cents` (int, nullable; default None = no block).
- `fine_block_holds` (bool; default false).

When threshold is set and outstanding total exceeds it:
- Checkouts always raise `BlockedByFinesError`.
- Holds raise only if `FINE_BLOCK_HOLDS=true`. Default-off means the "place hold now, pay at pickup" flow works out of the box.
- Patron-facing hold UI (`/ui/me/holds`, future catalog hold forms) show a "pay at pickup" warning banner when the patron is in this state.

### Currency display

`services/formatting.format_currency(cents)` reads the `currency_symbol` (default `$`) and `currency_symbol_position` (`before` | `after`, default `before`) site settings. Registered as a Jinja filter `| currency` for templates; CLI imports the helper directly. **API responses always use `amount_cents: int`**; clients format client-side. Decimal separator is hardcoded `.` — full locale-aware number formatting is out of v1 scope.

### Lost vs damaged semantics

- **`declare_lost`** closes any active loan, cancels pending holds on the work, assesses a lost-cost Fine (from `replacement_cost_cents` or the policy default), plus a processing Fine if configured. Item status becomes `lost`.
- **`mark_damaged`** same side effects on loan/holds, plus a damaged Fine; item status becomes `damaged` (item retained in catalog, non-loanable).
- **`clear_damage` / `clear_lost`** return item to `available`. Associated Fine rows are **not** modified — the librarian waives or keeps them as a separate decision.

### Permissions

- `fine.manage` (new) — assess / pay / waive fines; declare lost; mark damaged; clear damage/lost. Gated on all librarian-facing fine UI and actions.
- `fine.view.self` (new) — patron self-service view of their own fines. Added to the Patron preset role.
- Librarian preset covers `fine.manage` (in the slimmed-Librarian explicit list); Administrator covers via `*`.

### API surface (summary)

```
GET    /patrons/{card}/fines
POST   /patrons/{card}/fines/assess-overdue
GET    /me/fines
POST   /fines                                 (manual assess)
POST   /fines/{id}/pay
POST   /fines/{id}/waive                      body: {note}
POST   /items/{barcode}/lost                  body: {replacement_cost_cents?, note?}
POST   /items/{barcode}/damaged               body: {amount_cents, note}
POST   /items/{barcode}/clear-damage
POST   /items/{barcode}/clear-lost
```

---

## Notifications

`services/notifications/` provides an outbox-pattern email notification pipeline. Triggers write `Notification` rows synchronously; a cron-invoked drainer (`compendium maintenance send-queued-notifications`) renders pending rows and delivers them via SMTP (stdlib `smtplib`). No extra runtime deps.

### Model

- `Notification` table — one row per definitive notification, with pre-rendered `subject` + `body` (snapshot-at-queue semantics so later data edits don't retroactively rewrite pending messages).
- `patron.receive_notifications` bool — default true; patrons without `contact_email` are implicitly opted out.

### Templates

Jinja templates under `services/notifications/templates/<template_key>/{subject.txt, body.txt}`. Three templates ship:

- `hold_ready` — fires synchronously from `CirculationService._promote_hold` when a hold transitions to AVAILABLE.
- `due_soon` — queued by `maintenance queue-due-soon-notices` (daily cron) for loans due within N days.
- `overdue` — queued by `maintenance queue-overdue-notices` for overdue loans, at the highest matching tier (default tiers `3,14,30` days late). The body branches on `tier` to escalate tone.

Template render uses StrictUndefined — missing context keys raise at queue time (caller gets `ValidationError`), so template bugs surface loudly rather than silently sending blanks.

### Dedup

Partial unique indexes prevent duplicate queuing:

- `ix_notification_loan_dedup` unique on `(loan_id, template_key, discriminator)` where `loan_id IS NOT NULL AND status != 'cancelled'`.
- `ix_notification_hold_dedup` unique on `(hold_id, template_key, discriminator)` where `hold_id IS NOT NULL AND status != 'cancelled'`.

The `discriminator` column carries `renewal_count` for `due_soon` (so each renewal cycle gets its own reminder) and `tier` for `overdue` (so each escalation step fires at most once). For `hold_ready` it's always 0 — one notice per hold.

### Drainer behavior

On each run:
1. Fetch up to `COMPENDIUM_NOTIFICATIONS_BATCH_SIZE` pending rows ordered by `scheduled_for`.
2. If SMTP not configured (`SMTP_HOST` unset): bail with every row counted as `skipped`, no state changes. The backlog drains when configuration lands.
3. Per row: missing `recipient_email` → `cancelled` with `last_error=no_email`. Successful send → `sent` + `sent_at`. Exception → `attempts++`, `last_error`; after `COMPENDIUM_NOTIFICATIONS_MAX_ATTEMPTS` → `failed`.
4. One summary `SEND_NOTIFICATIONS` audit entry per run with counts.

### Failure modes

| Situation | Behavior |
|---|---|
| Patron has no email | Row inserted → drainer cancels it |
| Patron `receive_notifications=false` | Row never inserted |
| SMTP unconfigured | Rows accumulate; drainer logs "SMTP not configured" |
| Transient SMTP error | `attempts++`, leave pending; retry next run |
| Template render error | Raised at queue time; caller sees ValidationError |

### SMTP configuration

All non-secret knobs are **DB-editable** via `/ui/admin/system/{smtp,retention}` or `compendium settings set ...`. The env vars below remain a break-glass override (env wins on read). Only `COMPENDIUM_SMTP_PASSWORD` is *exclusively* env-backed (secret).

```
smtp_host                   (unset = inert — rows queue but don't send)
smtp_port                   (default 587)
smtp_username
COMPENDIUM_SMTP_PASSWORD    (env-only — secret)
smtp_use_starttls           (default true)
smtp_use_ssl                (default false; mutually exclusive with STARTTLS)
smtp_from_address           (required when smtp_host is set)
smtp_from_name              (default "Compendium")
notifications_batch_size    (default 50)
notifications_max_attempts  (default 5)
notification_retention_days (optional default for prune)
due_soon_days_before        (default 3)
overdue_tiers               (default [3, 14, 30] — list[int])
```

Each key listed without an env-var prefix can be set via `COMPENDIUM_<KEY>` env var, the web admin form, or `compendium settings set <key> <value>`.

### Dev setup

Run [mailpit](https://github.com/axllent/mailpit) locally:

```
docker run -d -p 1025:1025 -p 8025:8025 axllent/mailpit
export COMPENDIUM_SMTP_HOST=localhost
export COMPENDIUM_SMTP_PORT=1025
export COMPENDIUM_SMTP_USE_STARTTLS=false
export COMPENDIUM_SMTP_FROM_ADDRESS=noreply@example.test
compendium maintenance send-queued-notifications
# view delivered mail at http://localhost:8025
```

Tests mock the sender; no real SMTP is exercised in the suite.

### Retention / kill switch

`compendium maintenance prune-notifications`:
- `--older-than-days N` — delete rows older than N days. Without `--status`, deletes only `sent` + `cancelled` (preserves `failed` so a librarian can triage).
- `--status STATUS` — delete rows in that status. `--status pending` is the queue kill-switch for misfires.
- `--dry-run` — count without deleting.

Refuses to run with no filter. Audit entry (`PRUNE_NOTIFICATIONS`) records the filter and count.

### Permissions

- `notification.manage` — admin log viewer + manual retry. "Notifications" group in `PERMISSION_GROUPS`. Held by the Librarian preset (and Administrator via `*`).
- Patron self-service opt-out toggle on `/ui/me/preferences` needs no explicit permission (it edits the authenticated patron's own record).

### API surface

```
GET    /notifications?status=&template_key=&limit=&offset=
POST   /notifications/{id}/retry
```

---

## Site settings

Most runtime configuration is DB-editable via the `site_setting` table, with environment variables as a break-glass override.

**Read order**: env var → `site_setting` row → registry default. The first non-empty value wins. An unparseable env var raises `SettingValidationError` (fail-loud, matches Pydantic-Settings).

**Registry**: `services/settings_registry.py` is the canonical list of *what settings exist*. Each `SettingDescriptor` declares key, type, default, scope (`librarian` or `system`), human-friendly `display_name`, help text, optional validator, and `nullable` flag. New settings get added here; the table just stores text overrides.

**Cache**: `services/site_settings.py` keeps a process-local cache keyed on `MAX(updated_at)` with a 30s TTL. Writes invalidate immediately. Multi-worker (gunicorn) deployments see writes within 30s without a per-read DB round trip. Resilient to a missing `site_setting` table (logs a warning, returns defaults — covers the pre-`db init` case).

**Surfaces**:
- Web: `/ui/admin/settings/{general,circulation,kiosk}` (librarian-tier) and `/ui/admin/system/{smtp,retention}` (system-tier). Per-row "⚠ Overridden by env var" indicator when the env var is set; inputs disabled in that case.
- CLI: `compendium settings {list,get,set,reset}`. By default `list` shows DB-editable items only; pass `--all` to also include env-only `Settings` fields (DB URL, JWT secret, TLS material, etc.) — sensitive values mask to `********` unless `--show-secrets` is also passed. `--scope env-only` filters to just those. The combined `--all` view is the canonical answer to "what `COMPENDIUM_*` env vars does this app recognize?"
- API: `GET /settings/`, `GET/PATCH/DELETE /settings/{key}`.

**Hybrid for secrets**: `smtp_password` stays env-only (`COMPENDIUM_SMTP_PASSWORD`). Other SMTP knobs (host/port/from/etc.) are DB-editable. Same model for any future secrets — env-only by deliberate exclusion from the registry.

**Audit**: every write emits a `SETTING_UPDATE` (or `SETTING_RESET`) audit entry under `entity_type=site_setting` with `{key, before, after}` details.

---

## Testing strategy

| Level | Location | Scope |
|-------|----------|-------|
| Unit | `tests/unit/` | No DB; pure logic, mock repos |
| Integration (SQLite) | `tests/integration/` | In-memory SQLite; full service + repo stack |
| Integration (Postgres) | `tests/postgres/` | testcontainers Postgres; skipped if Docker unavailable |
| E2E | `tests/e2e/` | Hits a real `compendium serve` subprocess; Playwright/Chromium |

Tests use `pytest`. The `session` fixture provides a fresh SQLite session per test (rolled back on teardown). The `pg_session` fixture spins up an ephemeral Postgres container for the duration of the test session.

All API test modules create their own `StaticPool` engine (separate from the integration engine) to avoid SQLite in-memory thread-isolation issues with FastAPI's thread pool.

### Browser tests

E2E tests live in `tests/e2e/` and are tagged `@pytest.mark.e2e`. They are excluded from the default `pytest` run (`addopts = "-m 'not e2e'"`). Run with:

```bash
uv sync --extra e2e && playwright install chromium
uv run pytest -m e2e
```

The harness in `tests/e2e/conftest.py` boots a real `compendium serve` subprocess against a tmp_path SQLite file, seeds a librarian + patron + two works, and tears down the process on exit. Function-scoped `librarian_page` and `patron_page` fixtures return authenticated Playwright pages.

**Keystone test:** `test_csp_no_console_errors.py` navigates to every major page and asserts no `error`-level console messages. CSP violations (e.g. a `<script src>` tag missing a nonce under `'strict-dynamic'`) surface as console errors — this test would have caught the 2026-04-30 regression where HTMX was blocked on all pages.

**Other tests:**
- `test_login_csrf_roundtrip.py` — login form, auth cookie, logout.
- `test_place_hold_htmx.py` — HTMX partial swap when placing a hold.
- `test_theme_toggle_no_fouc.py` — pre-paint script applies localStorage theme immediately.
- `test_audit_viewer_pagination.py` — audit log filter form.
- `test_inline_policy_edit.py` — policy form submission and persistence.
- `test_scanner_mocked.py` — barcode scanner with mocked BarcodeDetector.
- `test_kiosk_session_flow.py` — kiosk card entry and session page.
