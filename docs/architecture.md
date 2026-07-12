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
| `LabelsService` | PDF generation (Avery item labels + patron cards via reportlab; Code 128 / Code 39 / Codabar barcodes via python-barcode) |
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

### Embedding Compendium as a library

A host application (e.g. LitCat, a desktop GUI) can import Compendium as a Python library and supply its own engine + session_scope via `compendium.db.engine.bind()`. Without a binding, Compendium falls back to server-mode defaults (reads `Settings()`, constructs its own SQLite engine). With a binding, all internal DB access — `site_settings`, `metadata_cache`, GB quota sentinel, backup — routes through the host's engine.

`BackupService` accepts an optional `Settings` argument. When omitted (or `None`), the database URL is derived from the session's bound engine via `session.get_bind().url` — so library-mode callers do not need to construct a Compendium `Settings` object just to run backups. Server-mode callers continue to pass `settings` explicitly.

```python
from compendium.db.engine import bind

# Call once at startup, after constructing your engine.
bind(your_engine, session_scope=your_session_scope)
```

**Contract for host-supplied `session_scope`:** the context manager *must* commit on normal exit and roll back on exception. Compendium's internal writers enter the scope, do writes, and exit normally without calling `session.commit()` themselves. A scope that does not auto-commit will silently lose those writes.

**Import flush contract:** when using `ImportService`, callers must invoke `svc.flush_metadata_cache()` *after* the outer session scope exits (commit or rollback), not before. The `WriteBuffer` opens a parallel session to write cache entries; calling it while the outer transaction is open causes a SQLite write-lock conflict. On Postgres this is not a locking issue, but the post-settle pattern is correct on both backends.

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

**Inactive patron / user filtering:** The patron list (`/ui/patrons`) and user list (`/ui/users`) default to active-only rows (same pattern as the catalog hiding all-withdrawn works). Append `?include_inactive=1` or tick the "Include inactive" checkbox to see deactivated records. The repository `list()` methods on `SqlPatronRepository` and `SqlUserRepository` accept an `include_inactive: bool = False` keyword for the same purpose. The CLI `patron list` and `user list` commands expose `--include-inactive`.

**Catalog ordering** uses `Work.sort_title` (indexed), which strips leading English articles (A, An, The) from the title. "The Great Gatsby" sorts under G, "An Odd Story" under O. The displayed title is unchanged; only the sort key is different. `sort_title` is set automatically on creation and title updates.

The sort order is configurable via the `order_by` parameter on `DiscoveryService.search`, the `GET /works/search?order_by=` API query parameter, the `--sort` flag on `compendium work search`, and the "Sort:" dropdown in the web catalog. Valid values:

| Value | Behaviour |
|---|---|
| `title` (default) | Ascending by `sort_title` then `title`. |
| `author` | Ascending by the primary creator's `sort_name` (`Creator.sort_name`, first by `WorkCreator.display_order`). Works with no creators sort last. |
| `recent` | Descending by `Work.created_at` (when the work record was added to the catalog). |
| `relevance` | When the query takes the FTS path (All Fields + non-empty query), preserves FTS rank order. On all other paths (field-scoped search, empty query) falls back to `title`. |

**Import normalization:** LibraryThing TSV, GoodReads CSV (and any source that calls `CatalogService`) normalizes two conventions on the way in:
- *Trailing-article titles* — `"Information, The"` is stored as `"The Information"` with `sort_title = "Information"`.
- *Last, First author names* — `"Brooks, David"` is stored as `"David Brooks"` (conservative heuristic: exactly one comma, no recognized name suffix like Jr./Sr./II). This ensures deduplification works across sources that use different conventions for the same author.

Existing records already in the database are not rewritten by these rules; only newly imported records are affected.

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
| **Librarian** | All catalog/circ/holds/fines/notifications/reports/labels/audit perms + `patron.manage`, `patron.account.manage`, `policy.edit`, `branch.edit`. **No** `user.manage` / `role.manage` / `system.manage`. | Multi-person shops — day-to-day operational seat |
| **Patron** | Self-service: `loan.view.self`, `loan.renew.self`, `hold.place.self`, `hold.view.self`, `fine.view.self`, `loan.claim.self` | Library members with a login account |
| **ReadOnly** | `item.view`, `work.view` | Anonymous catalog browsing or read-only auditors |

A startup warning logs if no active user holds `system.manage` — best-effort, doesn't block startup.

Guest catalog search is controlled by the `guest_search_enabled` site setting (env: `COMPENDIUM_GUEST_SEARCH_ENABLED`, default `true`).

### patron.account.manage

`patron.account.manage` is narrower than `user.manage`. It gates:
- Creating a Patron-role login inline when registering a new patron (web/API/CLI).
- `POST /patrons/{card}/account` — post-hoc account creation for a card-only patron.
- The "Create new login for this patron" form on the patron detail page.

It does **not** grant access to `/ui/users` (the full user list), role changes, or any account other than a freshly-created Patron-role account. This lets Librarians give self-service access to members without being able to touch staff accounts.

### Role-escalation guardrail

An actor may assign a role R only if every permission in R is also held by the actor — i.e. `set(R.permissions) ⊆ set(actor.permissions)`. An actor with the wildcard `"*"` (Administrator) may assign any role. This rule is enforced uniformly in:

- **Web** — the role dropdown on `/ui/users/new` and `/ui/users/{username}` is filtered to assignable roles; the server re-checks on submit.
- **API** — `POST /users` and `POST /users/{username}/change-role` validate via the same `assignable_roles()` helper.
- **CLI** — `compendium user add` enforces the rule when `COMPENDIUM_ACTOR_USERNAME` is set in the environment. Without that env var, the command warns if an Administrator already exists (bootstrap safety-net) but does not block.

The `assignable_roles(actor_permissions, all_roles)` function lives in `services/auth.py`.

### CSP and inline scripts

`_SecurityHeadersMiddleware` (`api/app.py`) generates a fresh CSP nonce per request via `secrets.token_urlsafe(16)`, stamps it on `request.state.csp_nonce`, and emits `script-src 'self' 'nonce-{nonce}' 'strict-dynamic'`. There is no `'unsafe-inline'` for scripts — a comment-field XSS that slips past output sanitization can't smuggle a `<script>` block because nonces are unguessable per request.

**Convention for inline scripts in templates.** Every `<script>...</script>` block in `web/templates/` must include `nonce="{{ csp_nonce(request) }}"`. External `<script src="/ui/static/...">` tags don't need a nonce — they match `'self'`. The `csp_nonce()` Jinja global is registered in `web/jinja.py` and reads from `request.state`.

A pytest test (`tests/integration/test_csp_nonce.py::test_every_script_in_templates_has_nonce`) walks the templates directory and fails if any inline script is missing a nonce. Miss one in a future template and the test catches it before the page silently breaks in a browser.

**Inline event-handler attributes are blocked too.** Without `'unsafe-inline'`
in script-src, the browser silently discards `onclick=`, `onchange=`,
`onsubmit=`, `onerror=`, `javascript:` URLs, and friends — the handler
appears to wire up but never fires. This is the same root cause as an
un-nonced `<script>` block; the failure mode is just quieter (no console
error, the action just runs without confirmation or the toggle just doesn't
toggle). Canonical fixes in this codebase:

- **HTMX confirmation prompts** — use `hx-confirm="..."` on the form/button.
  HTMX intercepts the click before the browser does; CSP doesn't see it as
  inline JS. Examples: `patrons/detail.html` (Cancel hold, Unlink user),
  `admin/patron_categories.html` (Delete category).
- **HTMX event handlers** — `hx-on::after-request="..."` etc. are parsed by
  HTMX, not the browser. Example: `kiosk/session.html` (re-focus barcode
  input after each scan).
- **Event delegation in a nonced `<script>` block** — for behaviors that
  aren't tied to an HTMX request (checkbox toggles, expand/collapse), drop
  the inline attribute and add an `addEventListener` inside the existing
  `<script nonce="{{ csp_nonce(request) }}">` block. Examples:
  `roles/new.html`, `roles/detail.html` (full-access permission toggle).
- **Server-side confirm page** — for one-way destructive actions where a
  full-page guard reads better than a JS dialog. The pattern: a `GET
  /ui/.../X-confirm` route returns a confirm template with a plain HTML
  `<form method="post">` posting to the existing action endpoint. Examples:
  `items/withdraw_confirm.html`, `fines/verify_returned_confirm.html`.

A second pytest test
(`tests/integration/test_csp_inline_handlers.py::test_no_inline_event_handlers_in_templates`)
walks the templates dir and fails if any inline event-handler attribute or
`javascript:` URL appears, with file:line offenders in the assertion message.
Symmetric with the script-nonce test above; together they cover both halves
of the "must not rely on `'unsafe-inline'`" promise.

**Style-src** still allows `'unsafe-inline'` because templates use `style="..."` attributes throughout. CSS-based attacks (data exfil via crafted selectors) are real but much lower impact than script execution; tightening that would be a separate, larger refactor.

### CORS posture

Compendium ships **no CORS middleware**. This is intentional: today all clients
are same-origin (the HTMX web UI and any API consumer that runs server-side).
The `Access-Control-Allow-*` headers are therefore absent, which means browsers
reject cross-origin requests — the safe default.

If a future slice adds a JavaScript SPA or a third-party integration that
legitimately needs cross-origin API access:

1. Add `CORSMiddleware` from Starlette/FastAPI.
2. **Never combine `allow_credentials=True` with `allow_origins=["*"]`**. That
   combination lets any origin make credentialed requests, effectively removing
   SameSite cookie protection. Enumerate the allowed origins explicitly.
3. Prefer narrowing `allow_methods` and `allow_headers` to exactly what the
   cross-origin client needs.

---

## External metadata

When adding items by identifier, Compendium looks up metadata from external sources:

| Media type | Primary source | Identifier |
|-----------|---------------|------------|
| book | Google Books (when key present; else Open Library) | ISBN or title search |
| vinyl, cd | MusicBrainz (CC0, no key) | UPC, MBID, or title search (format-filtered, artist+title fuzzy) |
| dvd, bluray, vhs | TMDb (requires key) | TMDb ID or title search |

For items that external sources can't find (zines, obscure self-releases, out-of-print rarities), `CatalogService.add_manual()` accepts user-supplied metadata directly. Exposed via the web UI at `/ui/items/new/manual` and the CLI `compendium item add-manual`.

Book adapters are resolved at runtime by `_resolve_book_adapter()` in `services/metadata.py`; non-book adapters are registered in the static `_ADAPTERS` dict. Adding a new media type requires implementing the `MetadataAdapter` protocol and registering it.

Note: TMDb does not index physical-disc UPCs, so film items are added via a title-search candidate picker rather than direct barcode scan. A UPC→title bridge is a deferred enhancement.

### Library hours and closed-date calendar

`CalendarService` (`services/calendar.py`) is the single source of truth for whether a given local date is open. It reads two tables:

- **`library_hours`** — one row per weekday (0=Monday, 6=Sunday) with `is_open`, `open_time`, `close_time`.
- **`closed_date`** — date ranges with optional `recurs_annually` flag.

All datetime arithmetic is done in UTC; local-date conversions use the **`library_timezone`** site setting (IANA name, default `"UTC"`, env `COMPENDIUM_LIBRARY_TIMEZONE`).

**Due-date rolling.** `CirculationService.checkout()` and `renew()` call `calendar.compute_due_at(now_utc, period_days)` instead of `now + timedelta(days=N)`. The helper adds `period_days` as local calendar days, walks forward past closed days, and returns the UTC instant of that open day's `close_time`. Without a CalendarService injected (or with all-days-open default hours), behaviour is identical to before this feature.

**Fine deduction.** `FineService._compute_overdue_amount()` calls `calendar.closed_days_between(due_at, reference)` to subtract closed local-dates from `days_over` before applying grace and the daily rate.

**Hold pickup expiry.** `HoldService._pickup_deadline(now_utc)` delegates to `calendar.compute_due_at()`, so the patron-facing pickup window rolls past closed days.

Admin UI: **Admin → Library Hours** (`/ui/admin/library-hours`) and **Admin → Closed Dates** (`/ui/admin/closed-dates`). Permission: `calendar.manage` (included in the Librarian preset). CLI: `compendium calendar hours show/set` and `compendium calendar closed-date list/add/delete`.

### Book metadata source preference and rate-limit handling

The primary book metadata adapter is chosen at runtime based on four factors:

1. **`book_metadata_source_preference`** site setting (default `"googlebooks"`, DB-editable at **Admin → System → Metadata Sources**, env `COMPENDIUM_BOOK_METADATA_SOURCE_PREFERENCE`). Valid values: `"googlebooks"`, `"openlibrary"`. The Google Books radio button is greyed out with an explanatory note when no API key is configured.
2. **`google_books_api_key`** — if the preference is `"googlebooks"` but no key is configured, Open Library is used silently as the primary.
3. **`book_metadata_fallback_enabled`** site setting (default `true`, DB-editable at **Admin → System → Metadata Sources**, env `COMPENDIUM_BOOK_METADATA_FALLBACK_ENABLED`). When `true`, a miss from the primary adapter automatically tries the secondary. When `false`, only the primary is consulted. See "Cross-source miss fallback" below.
4. **Quota circuit breaker** — Google Books free tier allows 1 000 requests/day. When the daily cap is hit (HTTP 403 `dailyLimitExceeded`), a sentinel row is written to `metadata_cache` under the key `("GoogleBooksAdapter", "_quota", "exhausted")` with a 24-hour TTL. All subsequent book lookups fall back to Open Library until the TTL expires (or use `compendium metadata gb-quota status` / `gb-quota clear`). When GB is secondary (OL primary), an exhausted-quota sentinel suppresses the GB fallback attempt for the same TTL window.

**Cover fallback symmetry:** when the primary adapter returns no cover URL, the *other* source is consulted:
- Open Library primary → Google Books cover (via Volumes API; requires key).
- Google Books primary → Open Library covers-by-ISBN endpoint (no key required; HEAD probe with `?default=false` to detect absence).

**Cross-source miss fallback:** when `book_metadata_fallback_enabled` is `true` (the default), a definitive not-found from the primary adapter automatically tries the secondary. This is now symmetric:
- Google Books primary miss → Open Library secondary (unchanged from before).
- Open Library primary miss → Google Books secondary (new; only when a key is configured and the daily quota is not exhausted).

Both results are cached under their respective adapter namespaces so a preference flip doesn't serve stale cache from the former primary. GB HTTP/transport errors are swallowed and the chain continues to Open Library; OL/non-GB transport errors propagate.

**Per-source manual refresh:** the Work edit page (`/ui/catalog/{id}/edit`) shows three refresh buttons for book items: the default (configured primary), "Refresh from Google Books", and "Refresh from Open Library". Non-book media types show a single default button. The source can also be specified on the API (`?source=googlebooks` or `?source=openlibrary`) and the CLI (`compendium work refresh-metadata --source openlibrary`). Per-source refresh always bypasses the cache (`bypass_cache=True`).

### Cover image (legacy note)

Prior to the Google-Books-primary slice, Open Library was the sole primary book adapter and Google Books was a cover-only fallback. The text above supersedes the old "Cover image fallbacks" section.

### Metadata cache

Successful external lookups (and definitive not-found responses) are persisted in the `metadata_cache` DB table so repeated lookups — especially the dry-run/apply doubling in the web importer — hit the cache instead of the network.

**Cache key:** `(adapter, kind, lookup_value)` — e.g., `("OpenLibraryAdapter", "isbn", "9780441013593")`. The adapter class name is part of the key so a future swap of which adapter is "primary" for a media type writes new entries under a new namespace without polluting or reading existing ones.

**TTLs:**
- Positive (found) entries: `metadata_cache_ttl_days` site setting (default 30 days, DB-editable at **Admin → System**, env `COMPENDIUM_METADATA_CACHE_TTL_DAYS`).
- Negative (not-found) entries: hardcoded 24 hours.
- Transport errors (`httpx` exceptions) are **not** cached — they propagate so the caller can retry.

**Write path:** during bulk import, cache writes are buffered in-process (`WriteBuffer`) and flushed to the DB *after* the import session commits or rolls back. This avoids a SQLite write-lock conflict (two active writers on the same connection) and ensures dry-run rollbacks don't swallow cache entries. For single-item add and refresh, writes go directly to the caller's session.

**`bypass_cache=True`:** user-clicked "Refresh from upstream" passes this flag so the explicit refresh intent is honored — the adapter is always called — while still writing the fresh response back to the cache for subsequent imports.

**Maintenance:** `compendium maintenance prune-metadata-cache` deletes rows past their TTL. `compendium metadata cache clear` deletes all rows (audited). `compendium metadata cache stats` prints counts by adapter.

---

## Recoverable work deletion (trash)

Deleting a Work (and its copies) is recoverable rather than a plain SQL `DELETE`. `TrashService` (`services/trash.py`) snapshots the whole work graph into a `deleted_entity` row, then hard-deletes the live rows. Restore re-inserts the snapshot under fresh primary keys.

### Snapshot vs. `deleted_at` — why not a soft-delete flag

The alternative design — a `deleted_at` column on `work` (and cascading soft-delete semantics onto `item`, `loan`, `hold`, …) — was rejected because it would touch **every** query path that reads those tables: catalog search, circulation, holds, fines, reports, audit, the FTS triggers. Each would need a `WHERE deleted_at IS NULL` clause (or a filtered view), and it's easy to miss one and leak a "deleted" row into a report or a barcode lookup.

The snapshot approach keeps the live schema and every existing query untouched: a deleted Work's rows are genuinely gone from `work`/`item`/`loan`/`hold`/etc., and the entire graph lives instead as one JSON blob in a single new table (`deleted_entity`) that no existing query path reads from. Zero query-path changes was the deciding factor, at the cost of restore doing more work (re-inserting rows with new ids and remapping internal FKs) than a soft-delete's `UPDATE ... SET deleted_at = NULL` would have.

### Payload shape and versioning

`SqlTrashRepository.snapshot_work_graph(work)` builds a single JSON-safe dict, stored verbatim in `deleted_entity.payload`:

```python
{
    "version": 1,                 # PAYLOAD_VERSION in repositories/sql/trash_repository.py
    "work": {...},                 # full work row (scalar columns only)
    "creators": [{"display_name", "sort_name", "role", "display_order"}, ...],
    "items": [{...}, ...],         # full item rows
    "loans": [{...}, ...],         # full loan rows for those items
    "holds": [{...}, ...],         # full hold rows for the work
    "item_notes": [{...}, ...],    # full item_note rows for those items
    "curated_lists": [{"slug", "annotation", "display_order"}, ...],
}
```

Datetimes/dates are serialized to ISO strings on the way in and parsed back on the way out (`_row_dict` / `_build_kwargs`), so the payload round-trips through the DB's native JSON column on both SQLite and Postgres. `restore_work` refuses (`BusinessRuleError`) if `payload["version"] != PAYLOAD_VERSION` — this guards against a downgrade reading a snapshot written by a newer Compendium with a payload shape it doesn't understand. There is no migration path between payload versions today; bumping `PAYLOAD_VERSION` is a breaking change for any trash rows written before the bump (they become permanently un-restorable, though still visible in `trash list` and still purgeable).

### Deletability rules

`delete_work(work_id)` refuses (`BusinessRuleError`, surfaced as `409` on the API) when:
- Any copy has an **active loan** (`returned_at IS NULL`) — check in first.
- Any copy has an **outstanding fine** (by `item_id` or via one of its loans) — collect or waive first.

Otherwise it proceeds: every **waiting/available hold** on the work is cancelled (mirrors `CatalogService._cancel_work_holds`; cancelled hold ids are recorded in the audit details), the graph is snapshotted, then hard-deleted.

### SET NULL, not relinked

Rows that reference the deleted work's items/loans/holds but aren't part of the snapshot — because they're independently meaningful history, not part of the work's own graph — have their FK nulled rather than being deleted or snapshotted: `fine.loan_id`, `fine.item_id`, `notification.loan_id`, `notification.hold_id`, `scan_event.item_id`, `scan_pending_item.created_item_id`. A settled fine or a sent notification survives the deletion (so financial and audit history isn't destroyed), but it's a permanent orphan with respect to the deleted work — **restore does not relink these rows**, even though it mints fresh ids for everything in the snapshot. Restoring a work does not undo the `SET NULL`s that happened at delete time.

### Restore semantics

`restore_work(trash_id)`:
1. Rejects an unknown trash id or a version mismatch (see above).
2. Runs `find_restore_collisions(payload)` — checks the *live* catalog for the snapshot's ISBN, UPC, item barcodes, and accession numbers — **before any write**, so restore is all-or-nothing: either the whole graph comes back, or nothing does and the trash row is untouched.
3. Re-inserts everything under **fresh primary keys** (`restore_work_graph`): a new `Work` row, creators matched-or-created by `sort_name` and re-linked via `WorkCreator`, items with a new id, loans/holds/item-notes remapped through an `item_id → new_item_id` map built during item re-insertion, and curated-list memberships re-attached to lists that still exist by `slug` (a list deleted in the meantime is silently skipped — the annotation is lost, but the payload doesn't error).
4. On success, the trash row is deleted (a restored work no longer appears in `trash list`).

### Retention and purge

`trash_retention_days` site setting (int, default `90`, env `COMPENDIUM_TRASH_RETENTION_DAYS`, DB-editable via `compendium settings set` / `PATCH /settings/trash_retention_days`). `0` disables time-based purging — trash rows then only go away via an explicit `purge(trash_id=...)` call.

`compendium maintenance purge-trash [--older-than-days N]` is the cron-invoked entry point (CLI-only by the existing maintenance parity exception): with no flag it reads `trash_retention_days`; a negative value (from either the flag or the setting) is rejected; `0` prints a "disabled" message and exits 0 without purging. Purging by id (`compendium work trash purge TRASH_ID`, `DELETE /trash/{id}`) works regardless of the retention setting — it's independent of the age-based sweep.

### Permission

`work.delete` gates delete, restore, list, and purge on all three interfaces. It was added to the Librarian preset by the same migration that creates `deleted_entity` (`0c0bf7eed591_work_trash.py`) so existing Librarian-role deployments pick it up on upgrade without a manual role edit; Administrator already covers it via the `*` wildcard.

---

## Bulk import & export

`services/import_export.py` provides bulk ingest and extract for CSV, MARC21 binary (`.mrc`), MARCXML, LibraryThing TSV, and GoodReads CSV exports. Surfaced on all three interfaces:

- CLI: `compendium import {csv|goodreads|librarything|marc} <file>` and `compendium export {csv|marc} <out>` (with `--xml` for MARCXML on export).
- API: `POST /import/{csv,goodreads,librarything,marc}` (multipart) and `GET /export/{csv,marc}?xml=…` (streaming). Import requires the `catalog.import` permission; export is gated on `item.view`.
- Web: `/ui/admin/import` (upload with dry-run preview + apply; format dropdown selects CSV / GoodReads CSV / LibraryThing TSV / MARC21 / MARCXML) and `/ui/admin/export` (filter form → download).

**Semantics:**

- **Dedup** by ISBN/UPC only; no fuzzy title matching. Conflict modes: `append` (add a copy to the existing work — default), `skip-duplicates`, `error-on-conflict`.
- **Barcode generation**: by default, any barcode/accession_number supplied in a CSV row is discarded and a fresh conformant 10/14-digit code is minted via `format_item_barcode()`. Pass `--preserve-barcodes` (CLI/API) or check the web checkbox to instead validate and keep the supplied codes (requires valid Compendium 10/14-digit format with Luhn check).
- **Encoding** (CSV + GoodReads CSV + LibraryThing TSV): UTF-8 is preferred, but stray non-UTF-8 bytes are tolerated by default — invalid bytes are replaced with U+FFFD and a warning is added to the report. Pass `--strict-encoding` (CLI), `strict_encoding=true` (API), or check "strict encoding" (Web) to fail the whole import on any bad byte. MARC has its own encoding semantics in the leader and is unaffected. Real-world LibraryThing exports often contain a handful of stray Latin-1/cp1252 bytes — the lenient default lets them in.
- **Transaction ownership**: the importer flushes but never commits — the caller's session scope (or test fixture) controls commit/rollback. Dry-run rolls back. Per-row errors are collected into an `ImportReport`; barcode/accession uniqueness is pre-validated at the application layer to avoid IntegrityError rollbacks mid-batch. After the outer session scope exits, callers must call `svc.flush_metadata_cache()` to write buffered metadata-cache entries — see "Embedding Compendium as a library" above for details.
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

### LibraryThing TSV mapping

LibraryThing exports a 53-column tab-separated file. The importer translates each row into the Compendium CSV-row contract and runs it through the same `_process_csv_row` pipeline (so dedup, barcode mint, branch defaults, and enrichment all behave the same):

- `Title` → `title`. No automatic title/subtitle split.
- `Primary Author` + `Secondary Author` → `authors`, semicolon-joined; LT's "Last, First" format is preserved.
- `Publication` is parsed for publisher and (fallback) year via `^(.+?)\s*\((\d{4})\)`. `Date` wins for the year if it's a clean 4-digit value; LT's `?` placeholder and partial dates fall back to the Publication-derived year.
- `Media` maps Hardcover/Paperback/Mass Market Paperback/Library Binding/Trade Paperback/Ebook → `book`; CD/Audiobook (CD) → `cd`; Vinyl/LP → `vinyl`; DVD/Blu-ray → `dvd`. Unrecognized values fall back to `--default-media-type`. Many LT exports have rows with empty Media — pass `--default-media-type book` for typical book-heavy libraries.
- `Languages` (English-language names) → ISO 639-1 via a small lookup; the first comma-separated value wins. Unknown ≤8-char values pass through; longer unknowns are dropped.
- `LC Classification` → `classification_scheme="LCC"`. `Dewey Decimal` is used only when LCC is empty (CLAUDE.md note: storing DDC values is fine; shipping DDC reference data is not).
- `ISBN` is unwrapped from LT's `[…]` brackets; `[]` decodes to no ISBN. Normalized to ISBN-13 by `normalize_isbn`.
- `Other Call Number` → item-level `call_number`.
- `Barcode` is honored when `--preserve-barcodes` is set. Most LT exports leave it blank.
- `Book Id`, `Work id`, `OCLC`, `LCCN`, `BCID` round-trip through `Work.external_ids["librarything"]`.
- User-attached fields with no Compendium home today (`Tags`, `Collections`, `Rating`, `Review`, `Comment`, `Private Comment`, `Page Count`, `Physical Description`, `Original Languages`, `Subjects`) are preserved in `Work.extra_metadata["librarything"]` so a future tags slice can lift them out — they aren't silently dropped.
- `Copies > 1` mints additional Items via the same Work. Copies 2..N always run in `append` mode and skip enrichment regardless of the user-selected mode (warnings surface this).

Fields not listed above (LT's `Sort Character`, `Acquired`, `Date Started`, `Date Read`, `Source`, `Entry Date`, `From Where`, lending history, etc.) are dropped.

### GoodReads CSV mapping

GoodReads exports a 23-column CSV. The importer runs through the same `_process_csv_row` pipeline as LibraryThing:

- `Title` → `title` (via `normalize_title`).
- `Author` → primary creator with role `author`. `Additional Authors` (comma-separated) → each with role `contributor` (GoodReads carries no role data for additional authors).
- `ISBN13` is preferred over `ISBN` when both are present. Both columns use GoodReads' Excel-style `="..."` wrapper (a common anti-coercion trick for spreadsheets) which is stripped on import.
- `Publisher` → `publisher`.
- `Year Published` → `publication_year`. Must be a 4-digit integer; otherwise left empty.
- `Binding` → always `book` media type (GoodReads is a books-only service); the raw binding value (Paperback / Hardcover / Kindle Edition / etc.) is preserved in `extra_metadata["goodreads"]["binding"]`.
- Language and classification are absent from GoodReads exports; both fields are left empty. Pass `--enrich` to fill them from Open Library after import.
- `Owned Copies = 0` (or empty) → 1 copy. GoodReads is a reading log, not an inventory; users rarely populate this field. A value > 0 is honored and mints that many Items (same append-mode and no-re-enrich behavior as LT's `Copies` column).
- `Book Id` round-trips through `Work.external_ids["goodreads"]["book_id"]`.
- `My Rating`, `My Review`, `Spoiler`, `Private Notes`, `Read Count`, `Date Read`, `Date Added`, `Bookshelves`, `Exclusive Shelf`, `Original Publication Year`, `Number of Pages`, and the raw `Binding` value are preserved in `Work.extra_metadata["goodreads"]` for a future reading-history or tags slice.

`Author l-f` (last-first format) and `Bookshelves with positions` (redundant with `Bookshelves`) are dropped.

### Bulk metadata enrichment

Per-Work refresh has surfaces on CLI / API / Web (`compendium work refresh-metadata <id>`, `POST /works/{id}/refresh-metadata`, `/ui/works/{id}/refresh-metadata`). For catch-up after a bulk import — or as a low-cadence cron task — there's also `compendium maintenance refresh-metadata`, which loops `CatalogService.refresh_metadata_bulk` over Works with an ISBN/UPC and (by default) at least one missing core field (description / cover_image_url / publisher / language).

- `--missing-only` (default) keeps cron runs cheap once the catalog is clean. `--all` re-fetches every eligible Work (use after an upstream data improvement).
- `--limit N` caps the batch — combined with `--missing-only` and the `Work.id` ascending scan, repeat runs make forward progress without revisiting completed Works.
- `--media-type` and `--branch` scope the run.
- Errors are counted, not raised — exit code is always 0 so cron schedules don't break.
- One `BULK_REFRESH_METADATA` audit entry per apply-mode run (counts + filters).

Why CLI-only: a synchronous HTTP request that loops Open Library / TMDb lookups for hundreds of Works would either reproduce the original 504-from-nginx problem or block a request thread for minutes. A Web/API surface for bulk refresh waits until the codebase has a generic background-jobs framework.

---

## Label templates and spine layout

### Supported templates

| Template key | Dimensions | Kind(s) |
|---|---|---|
| `avery-5167` | ½" × 1¾" (4×20) | pocket, barcode-only |
| `avery-5167-spine` | ½" × 1¾" rotated (4×20) | **spine** — narrow face |
| `avery-5160` | 1" × 2⅝" (3×10) | pocket, barcode-only, **spine** (flat, wraps around) |
| `avery-5160-spine` | 1" × 2⅝" rotated (3×10) | **spine** — medium face |
| `avery-5871` | 2" × 3½" (2×5) | pocket, patron-full |
| `avery-22805` | 1½" × 1½" square (4×6) | pocket, **spine** |
| `avery-22806` | 2" × 2" square (3×4) | pocket, patron-full, **spine** |

### Spine label layout

There are two rendering modes for spine labels, determined by `template.orientation`:

**Rotated** (`orientation="rotated"`, e.g. `avery-5167-spine`, `avery-5160-spine`): the canvas is translated and rotated 90° CCW inside `_draw_item_label`, so downstream drawing code sees a tall narrow cell. Text runs *along* the spine's physical long axis. Left-alignment is used so text anchors to the label's leading edge.

**Flat** (`orientation="landscape"`, e.g. `avery-5160`, `avery-22805`, `avery-22806`): the label is wider than most book spines so it wraps around to the covers. Text is drawn with `drawCentredString` at `x + lw/2` so it lands on the visible spine face regardless of spine width. Branch and location run across the full label width, centered.

### Bottom-up spine layout algorithm

Cutter and year are reserved from the bottom of the cell upward (above the optional barcode strip) before the call-number block is drawn top-down. This guarantees they always have distinct, non-overlapping baselines:

```
  y + lh - pad  ┐
                │  branch (optional, top)
                │  location (optional)
                │  call_number lines (fills remaining gap)
                │
  cutter_base  ─┤  cutter (bold, size 10)
  year_base    ─┤  year (regular, size 9)
  text_bottom  ─┤  (above barcode strip)
  bc_strip     ─┤  barcode (optional)
  y + pad      ─┘
```

`max_cn_lines` is computed dynamically from the space between the top-down cursor (after branch/location) and `cutter_base`, so long call numbers on short labels truncate cleanly instead of colliding with cutter/year.

---

## Live label preview

The labels UI at `/ui/labels/items` includes a live SVG preview pane that updates within ~150ms of any form change (kind, template, fields). It uses the same layout code as PDF generation — no separate preview renderer.

### Canvas protocol

Internal drawing helpers (`_draw_item_label`, `_draw_item_label_content`, `_draw_barcode`, etc.) accept a `LabelCanvas` structural protocol defined in `services/labels.py` rather than a concrete `canvas.Canvas`. Both backends satisfy the protocol:

- **`canvas.Canvas` (reportlab)** — PDF backend, used by `generate_item_labels`.
- **`SVGLabelCanvas`** (`services/label_canvas_svg.py`) — SVG backend, used by the preview. Accumulates drawing operations and serializes via `to_svg()`.

### Coordinate conventions

PDF uses y-up (origin bottom-left); SVG uses y-down (origin top-left). `SVGLabelCanvas.to_svg()` wraps all content in a root `<g transform="translate(0,H) scale(1,-1)">` group so drawing code can use PDF coordinates unchanged. Each text element also carries a per-element `scale(1,-1)` counter-flip so glyph outlines render right-side-up.

Transform frames (`saveState`/`translate`/`rotate`/`restoreState`) are represented as a stack of `_Frame` objects whose accumulated transforms become a `<g transform="...">` wrapper around the frame's content on `restoreState`.

### EAN-13 limitation

The EAN-13 path uses reportlab's `Drawing.drawOn(c, ...)` which calls private reportlab-graphics primitives not in `LabelCanvas`. The preview always uses the configured barcode symbology (Code 128 / Code 39 / Codabar) via the rect-based path. The printed PDF is unaffected. The sample barcode ("SAMPLE-001") is alphanumeric, so EAN-13 validation fails and the fallback activates naturally even without explicit gating.

### Preview route

`GET /ui/labels/items/preview` (requires `labels.generate` permission) reads `kind`, `template`, and `field_*` query params — identical to what `POST /ui/labels/items` reads from form data — and returns an HTML fragment containing inline SVG rendered from a hardcoded placeholder row. HTMX on the form fires `hx-get` on every change with `hx-include="#item-labels-form"`.

---

## Label barcode symbology

Item labels and patron cards encode the Compendium barcode value in one of three symbologies, chosen via the `barcode_symbology` site setting (Code 128 / Code 39 / Codabar, default Code 128). The setting is read once per render call inside `generate_item_labels` / `generate_patron_cards` — there's no per-render override, on the assumption that operators set it once to match their scanner hardware and don't toggle per batch.

`reportlab.graphics.barcode` doesn't ship a Codabar renderer, so the bar/space module pattern comes from `python-barcode` (MIT) and the bars are drawn directly onto the reportlab canvas with `Canvas.rect`. Codabar requires explicit start/stop characters around the data; the helper wraps the value with `A...A` and strips them from the human-readable text below the bars. Code 128 is recommended for compact spine labels because it produces shorter barcodes than Codabar or Code 39.

If the chosen symbology can't encode a value (Codabar rejects letters, Code 39 rejects most punctuation), the renderer silently falls back to Code 128 for that label. Compendium-minted barcodes are always decimal digits and encode cleanly under all three; the fallback handles legacy / imported barcodes that contain other characters. Switching the setting affects only newly rendered PDFs — the underlying barcode string in the DB is symbology-neutral.

The optional ISBN-as-barcode flow (`--use-isbn-barcode` / `?use_isbn_barcode=true`) renders EAN-13 (still via reportlab) when the row has a valid 12- or 13-digit ISBN; on a malformed ISBN it falls through to the operator's chosen symbology.

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

## Item notes / condition history

The `item_note` table holds a dated per-item history trail. Each row carries `item_id`, `kind` (enum), `note` (free text), optional `event_date`, `is_system` (bool), and optional actor attribution (`user_id`, `actor_label`).

**Auto-logging.** System entries (`is_system=True`) are written automatically by:
- `CatalogService.update_item` — whenever `condition` changes.
- `CatalogService.withdraw_item` — on withdrawal.
- `CirculationService` — on `declare_lost`, `mark_damaged`, `clear_damage`, `clear_lost`, `claim_returned`, `verify_returned`, and `write_off_claim`.

Routine circulation (checkout / checkin / renew / hold-fill) is **deliberately excluded** — loan history already records those transitions.

**Immutability.** `is_system=True` entries cannot be deleted via any interface.

**Permissions.** `item.view` is required to read the note trail; `item.edit` is required to add or delete manual entries. No new permission string was introduced.

**Wiring.** Pass `item_note_repo=SqlItemNoteRepository(session)` to `CatalogService` and `CirculationService` factory calls. When `None`, auto-logging is silently skipped (backward-compatible for callers that don't inject the repo).

**Interfaces.** Web UI: note trail shown on `/ui/items/{barcode}` with add/delete forms. API: `GET /items/{barcode}/notes`, `POST /items/{barcode}/notes`, `DELETE /items/{barcode}/notes/{note_id}`. CLI: `compendium item note add/list/delete`.

---

## Curated lists

`CuratedList` and `CuratedListEntry` are lightweight catalog feature models for librarian-curated collections ("Staff picks", "Summer reads").

### Models

- **`CuratedList`** — `id`, `slug` (unique, URL-safe, 96-char), `name`, `description`, `is_public`, `is_featured`, `display_order`, `created_at`, `updated_at`, plus a `entries` relationship.
- **`CuratedListEntry`** — composite PK `(list_id, work_id)`, `display_order`, `annotation` (free-text per-work note). Cascades delete when the parent list is deleted.

### External identifier

`slug` is the only external-facing identifier — internal integer PKs are never exposed. `CuratedListService._unique_slug(name)` slugifies the list name (lowercase, hyphens) and appends a short random suffix on collision, ensuring globally unique, human-readable URLs.

### Public vs featured

- **`is_public`** — controls whether the list appears in the OPAC. Non-public lists are visible only to users with `curatedlist.manage`.
- **`is_featured`** — when `True` and `is_public=True`, the list appears as a shelf on the OPAC landing page (`/ui/catalog`). Shelves render in `display_order` ascending order; works within each shelf render in entry `display_order` order.

### Interfaces

- **Web admin** — CRUD at `/ui/curated-lists` (requires `curatedlist.manage`); "Manage curated lists" link on work detail pages for users with that permission.
- **Public OPAC** — `/ui/lists` (index of public lists) and `/ui/lists/{slug}` (list detail with annotated works). Guest access follows the `guest_search_enabled` site setting — same dependency used by catalog search.
- **REST API** — `GET/POST /curated-lists`, `PATCH/DELETE /curated-lists/{slug}`, `POST/DELETE /curated-lists/{slug}/works`.
- **CLI** — `compendium curated-list create/list/show/edit/delete/add-work/remove-work/reorder`.

### Permission model

`curatedlist.manage` gates all admin CRUD (create, edit, delete lists; add/remove/annotate/reorder works). Read access to public lists requires only `item.view` (or guest access when `guest_search_enabled=true`). The permission is included in the Librarian preset.

---

## Notifications

`services/notifications/` provides an outbox-pattern email notification pipeline. Triggers write `Notification` rows synchronously; a cron-invoked drainer (`compendium maintenance send-queued-notifications`) renders pending rows and delivers them via SMTP (stdlib `smtplib`). No extra runtime deps.

### Model

- `Notification` table — one row per definitive notification, with pre-rendered `subject` + `body` (snapshot-at-queue semantics so later data edits don't retroactively rewrite pending messages).
- `patron.receive_notifications` bool — default true; patrons without a resolvable email are implicitly opted out.
- **Email resolution order**: `patron.contact_email` (wins), then `patron.user.email` (fallback when `contact_email` is blank and the patron has a linked user account). The `_patron_email()` helper in `NotificationService` encapsulates this; both `_patron_can_receive` and the `recipient_email` column use it for consistency.

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

All knobs are **DB-editable** via `/ui/admin/system/{smtp,secrets,retention}` or `compendium settings set ...`. Env vars remain a break-glass override (env wins on read). `smtp_password` is a secret — set it via **Admin → System → Secrets** when `COMPENDIUM_SECRET_KEY` is configured, or fall back to `COMPENDIUM_SMTP_PASSWORD` in env.

```
smtp_host                   (unset = inert — rows queue but don't send)
smtp_port                   (default 587)
smtp_username
smtp_password               (secret — stored encrypted; env COMPENDIUM_SMTP_PASSWORD wins)
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

## Remote phone scanner

A librarian can pair a smartphone as a wireless barcode scanner without installing a native app. The phone's browser camera continuously reads barcodes and dispatches them to the desk session in real time via a lightweight polling endpoint.

### Pairing lifecycle

1. **Desk creates pairing** — a librarian visits `/ui/scan/pair`. Compendium creates a `ScanPairing` row with a short-TTL claim secret (hashed; raw secret is never stored). The secret and pairing URL are encoded into a QR rendered inline as SVG.
2. **Phone claims** — the phone camera scans the QR, POSTs the claim secret to `/ui/scan/phone/claim`, and receives a session cookie. The claim secret is rotated at this point (the hash is updated; the old secret is invalidated).
3. **Phone dispatches scans** — the phone browser POSTs each decoded barcode to `/ui/scan/phone/dispatch`. The server returns the result (work title, patron name, etc.) based on the active mode.
4. **Desk receives results** — the desk page polls `GET /ui/scan/pairings/{id}/log` (via HTMX) to show the latest scan count and any mode-change prompts.
5. **Session ends** — the librarian clicks "Unpair" (or closes the browser), which sets `revoked_at` on the `ScanPairing` row. The phone's next dispatch request sees a 401 and shows a "session ended" message. Sessions also expire automatically when `expires_at` passes (controlled by `scan_session_minutes`, default 60 min).

Expiry and revocation are enforced on every phone action (claim, dispatch, mode-change) — there is no grace window.

### Desk live feed and review queue

Each non-ignored phone dispatch appends a row to the `scan_event` table (`kind` is `"ok"` or `"error"`; the 2-second idempotency collapse is never written). The desk page polls `GET /ui/scan/pairings/{id}/log` every 1500 ms via HTMX and renders the most recent events as a live feed plus the review queue (below).

When a pairing's `catalog_review` flag is on (a per-pairing toggle the phone sets at any time via `POST /ui/scan/review`, available in catalog mode only), catalog-mode ISBN scans are held for desk review instead of immediately creating an item. Each held scan lands in the `scan_pending_item` table with `status="pending"`. The desk review queue is rendered as part of the `GET /ui/scan/pairings/{id}/log` poll partial; the librarian can approve (`POST /ui/scan/pairings/{id}/pending/{pid}/approve`), **edit-then-approve** (opens an inline modal on the desk page — no navigation away), or discard (`POST /ui/scan/pairings/{id}/pending/{pid}/discard`) each entry. Approving creates the Work+Item using the stored metadata snapshot. Discarding sets `status="discarded"`. The queue spans all of the librarian's pairings and survives unpairing, so a held scan can still be approved after the original pairing ends; approve, discard, and edit all authorize each entry via its own pairing's owner.

The phone polls `GET /ui/scan/heartbeat` approximately every 5 seconds. When the desk clicks **Unpair**, the pairing is revoked server-side; the next heartbeat returns a non-200 response and the phone ends its session within ~5 seconds.

### Multi-mode state machine

A pairing is created with an `allowed_modes` list (`checkout`, `checkin`, `catalog`) set at pairing time. Both the circulation desk and the Add-Item page offer every mode the librarian has permission for; they differ only in which they **pre-check** — the desk pre-checks the circulation modes (Checkout/Checkin), the Add-Item page pre-checks Catalog — and the librarian can opt into the others before generating the QR. The phone's initial `mode` is the first allowed mode, and the phone can toggle among the allowed modes thereafter. The active `mode` is stored on the `ScanPairing` row and read on every dispatch. In checkout mode, a `borrower_patron_id` is also tracked on the pairing (the librarian or patron selects the borrower on the desk; the phone sees just the scan).

### HTTPS / secure-context requirement

The phone camera API (`getUserMedia`, `BarcodeDetector`) requires a **secure context** — `https://` or `localhost`. The QR URL Compendium encodes must therefore use `https://`. Compendium derives the base URL from the staff request, honoring `X-Forwarded-Proto` when your reverse proxy sets it — the bundled `docker/nginx/nginx.conf` sets `X-Forwarded-Proto https`, so phone pairing works out-of-the-box behind the shipped nginx stack. Compendium refuses to render a QR code when the resolved base URL is not HTTPS (except `localhost` for development).

Set `COMPENDIUM_PUBLIC_BASE_URL` (or the DB-editable `public_base_url` setting) to your external `https://` origin when your proxy does **not** set `X-Forwarded-Proto`, or to pin a specific public hostname. The Docker compose file passes this through from `.env`; see `docker/README.md` for details.

### Shared decoder module seam (downstream consumers)

`scanner.js` exposes a stable public API for downstream projects (e.g. LitCat):

```js
runContinuous(videoElement, backend, { onCode, onMiss })
```

`backend` is `"native"` (BarcodeDetector) or `"zxing"` (ZXing-js fallback). `onCode(value)` is called for each decoded barcode; `onMiss()` is called each scan cycle when no barcode is found. This signature is **pinned** — changes are announced in CHANGELOG.

### Maintenance

`compendium maintenance prune-scan-pairings --older-than-days N` deletes terminal pairing rows (those whose `expires_at` is older than the cutoff, or whose `revoked_at` is set and older than the cutoff). When such a pairing is pruned, all of its child `scan_event` rows and **all** of its `scan_pending_item` rows are deleted with it (they are all resolved by construction — see below). A pairing is **skipped entirely** if it still has any `status="pending"` pending items — un-resolved desk-review work is never silently dropped, so no pending row is ever deleted. Separately, old resolved pending rows on pairings that are *not* being deleted this run are swept by the cutoff. Live, unexpired sessions are never pruned. Suggested cadence: daily, `--older-than-days 7`.

---

## ISBN/UPC circulation fallback

Checkout, checkin, and renew — at the desk, the self-checkout kiosk, the phone scanner, the CLI, and the API's checkout endpoint — accept a book's printed ISBN barcode or a disc's UPC when the scanned code is not a Compendium item barcode. This means home and classroom libraries can circulate items without printing labels first.

### Resolution order

1. **Exact Compendium barcode** — always wins; no ISBN lookup attempted.
2. **ISBN** — ISBN-10 is normalized to ISBN-13 before the lookup.
3. **UPC/EAN** — the leading-zero variant is tried both ways (a stored 12-digit UPC-A matches a scanned 13-digit `0`-prefixed EAN-13, and vice versa).

### Why scanned codes can't collide

Scanned barcodes are EAN-8 (8 digits), UPC-A (12 digits), or EAN-13 (13 digits). Compendium barcodes — item barcodes and patron cards alike — are 10 or 14 digits (`barcode_format` setting; type prefix digit 2 = patron, 3 = item; Luhn check digit). The digit-length ranges don't overlap, so a barcode physically printed on a book or disc can never be mistaken for a Compendium-format code.

**Typed ISBN-10 caveat.** A hand-typed all-digit ISBN-10 (10 digits) *does* overlap with the 10-digit Compendium format. Roughly 10% of ISBN-10s pass the Luhn check, and two ISBN registration groups collide with the type prefixes: group 3 (German) parses as an *item* barcode — harmless, because the exact-barcode lookup misses and the code falls through to ISBN resolution anyway; group 2 (French) parses as a *patron card*, so the phone scanner's checkout mode stops at "Card not recognized" (and checkin mode rejects it as a non-item code) rather than resolving it as an ISBN. Practical exposure is negligible because book barcodes are always printed as EAN-13; this only surfaces if someone manually types a bare ISBN-10.

### Per-operation copy selection

- **Checkout** — picks a copy automatically: the copy on the pickup shelf for the patron's active hold on that work is preferred; otherwise the lexicographically-first-accession available copy is chosen.
- **Renew** — scopes to the renewing patron's own active loans on the work. If the patron has several copies on loan, the earliest-due loan is renewed.
- **Checkin** — requires exactly one copy of the work to be on loan. If there is more than one copy out, `AmbiguousItemError` is raised and the caller must ask which copy came back:
  - **Web desk** — renders an inline copy picker (borrower name, due date, accession number); clicking a row re-posts the copy's real item barcode to the normal checkin flow.
  - **CLI** — prints a candidate table and exits with status 1 so scripts don't silently pick the wrong copy.
  - **Phone scanner** — shows the error message on the phone screen; the librarian must scan the individual item barcode.
  - **REST API** — unaffected: the API's checkin and renew endpoints are loan-ID-based (`/loans/{id}/checkin`, `/loans/{id}/renew`), so ISBN ambiguity cannot reach them.

Per-copy operations — `declare_lost`, `mark_damaged`, `clear_lost`, `clear_damage`, `withdraw`, condition changes — still require the copy's own barcode; ISBN/UPC resolution is only enabled for the three circulation operations above.

### Phone scanner dedup nuance

The phone scanner's duplicate-scan guard keys on the raw scanned code, so if the desk scans the same ISBN barcode twice within the ~2-second dedup window (two copies of the same title), the second scan is collapsed to a "Duplicate scan ignored." message. With item barcodes this never arose because each copy has a unique barcode; with ISBN barcodes two copies share the same printed code. The workaround is to wait for the first scan's result before scanning the second copy, or to scan the individual item barcodes for the second copy.

### Setting

`circulation_scan_isbn_enabled` (bool, default `true`). DB-editable at **Admin → Settings → Circulation**; env var `COMPENDIUM_CIRCULATION_SCAN_ISBN_ENABLED` wins on read. Set to `false` to require real item barcodes for all circulation operations (e.g. in a deployment that has labelled every item and wants strict one-scan-one-copy semantics).

---

## Site settings

Most runtime configuration is DB-editable via the `site_setting` table, with environment variables as a break-glass override.

**Read order**: env var → `site_setting` row → registry default. The first non-empty value wins. An unparseable env var raises `SettingValidationError` (fail-loud, matches Pydantic-Settings).

**Registry**: `services/settings_registry.py` is the canonical list of *what settings exist*. Each `SettingDescriptor` declares key, type, default, scope (`librarian` or `system`), human-friendly `display_name`, help text, optional validator, and `nullable` flag. New settings get added here; the table just stores text overrides.

**Cache**: `services/site_settings.py` keeps a process-local cache keyed on `MAX(updated_at)` with a 30s TTL. Writes invalidate immediately. Multi-worker (gunicorn) deployments see writes within 30s without a per-read DB round trip. Resilient to a missing `site_setting` table (logs a warning, returns defaults — covers the pre-`db init` case).

**Surfaces**:
- Web: `/ui/admin/settings/{general,circulation,kiosk}` (librarian-tier) and `/ui/admin/system/{smtp,secrets,retention}` (system-tier). Per-row "⚠ Overridden by env var" indicator when the env var is set; inputs disabled in that case.
- CLI: `compendium settings {list,get,set,reset}`. By default `list` shows DB-editable items only; pass `--all` to also include env-only `Settings` fields (DB URL, JWT secret, TLS material, etc.) — sensitive values mask to `********` unless `--show-secrets` is also passed. `--scope env-only` filters to just those. The combined `--all` view is the canonical answer to "what `COMPENDIUM_*` env vars does this app recognize?"
- API: `GET /settings/`, `GET/PATCH/DELETE /settings/{key}`.

**Secret storage**: `SettingDescriptor.secret=True` marks a setting as sensitive. On write, `services/secrets.py` encrypts the value with Fernet (AES-128-CBC + HMAC-SHA256) and stores `enc:v1:<ciphertext>`. On read, the layer decrypts transparently; if the key is missing or mismatched, a warning is logged and the registered default is returned. A canary value (`_secret_canary`) written on first use lets the app detect key rotation — the Secrets page shows a "Key mismatch" banner rather than silently failing. Currently registered secrets: `smtp_password`, `tmdb_api_key`, `google_books_api_key`. Env vars for these still win on read. `COMPENDIUM_SECRET_KEY` itself is env-only (can't store the key in the encrypted store). Audit payloads for secret settings redact `before`/`after` to `"***"`.

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

---

## Release & distribution

Every GitHub release triggers `.github/workflows/release.yml`, which runs two parallel jobs:

- **publish-pypi** — builds the wheel/sdist via `uv build` and publishes to PyPI via OIDC
  Trusted Publishing (no token required). Package name: `compendium-ils`.
- **publish-image** — builds a multi-arch (`linux/amd64` + `linux/arm64`) Docker image and
  pushes it to `ghcr.io/statyk/compendium`. Tags: `vX.Y.Z`, `X.Y`, `latest`.

The image is built from `docker/Dockerfile` with the repo root as context. The
`docker/docker-compose.yml` stack pulls the published image by default; operators can pin
a specific version via `COMPENDIUM_IMAGE=ghcr.io/statyk/compendium:X.Y.Z` in `.env`.
A `docker/docker-compose.build.yml` override exists for building from source.
