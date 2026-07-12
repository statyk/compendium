# Database Schema

Compendium uses SQLAlchemy 2.0 ORM models as domain models (no separate translation layer). Migrations are managed by Alembic. The same schema targets both SQLite and Postgres; dialect-specific features (FTS5 triggers, tsvector GIN index) are applied conditionally in migrations.

All primary keys are integer (`bigserial` on Postgres). Users see external-facing identifiers only — barcode, accession_number, library_card_number, ISBN/UPC — never internal row IDs.

---

## Tables

### `media_type`

Reference table for supported media formats.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| code | varchar(32) UNIQUE | `book`, `vinyl`, `cd`, `dvd`, `bluray`, `vhs` |
| display_name | varchar(64) | Human-readable label |

Seeded at startup. New codes can be added without schema changes.

---

### `branch`

Physical library location. Included from day one so every item and loan carries a branch reference; multi-branch features are deferred to a future version.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| code | varchar(32) UNIQUE | |
| name | varchar(128) | |
| address | JSON | Free-form address blob |
| is_default | boolean | Exactly one branch should be default |
| created_at | timestamptz | |

---

### `creator`

Author, director, artist, or any creative contributor. One row per person regardless of how many works they've contributed to.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| display_name | varchar(256) | Display form: "Frank Herbert" |
| sort_name | varchar(256) INDEX | Sort form: "Herbert, Frank" |
| external_ids | JSON | `{"viaf": "...", "musicbrainz": "..."}` |
| created_at | timestamptz | |

Linked to works via `work_creator`. The same creator can have different roles across different works (author on one, editor on another).

---

### `work`

Abstract title — one row per ISBN or UPC. Collapses FRBR Work/Expression/Manifestation into a single table for simplicity; different editions have different ISBNs and land in different rows.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| title | varchar(512) NOT NULL | |
| subtitle | varchar(512) | |
| media_type_id | integer FK → media_type | |
| publisher | varchar(256) | |
| publication_year | integer | |
| edition | varchar(128) | |
| language | varchar(8) | ISO 639-1 code |
| description | text | |
| isbn | varchar(13) INDEX | Null for non-book media |
| upc | varchar(20) INDEX | Null for books |
| classification_scheme | varchar(32) | `lcc`, `ddc`, or custom |
| classification_code | varchar(64) | Free-form; the scheme drives meaning |
| cover_image_url | varchar(512) | |
| extra_metadata | JSON | Media-specific extras (tracks, runtime, cast, …) |
| external_ids | JSON | `{"openlibrary": "...", "musicbrainz": "...", "tmdb": "..."}` |
| search_text | text | Denormalized FTS document (title + creators + description); maintained by service layer |
| sort_title | varchar(512) INDEX | Title with leading English article stripped ("The Great Gatsby" → "Great Gatsby"); used for catalog ordering |
| created_at | timestamptz | |
| updated_at | timestamptz | Set on update |

**Indexes:**
- `ix_work_isbn` on `isbn`
- `ix_work_upc` on `upc`
- `ix_work_sort_title` on `sort_title`
- SQLite: `work_fts` FTS5 virtual table (external content on `search_text`) + triggers
- Postgres: `ix_work_search_gin` GIN index on `to_tsvector('english', search_text)`

**extra_metadata shape by media type:**
- *vinyl/cd:* `{format, tracks: [{position, title, length_ms}], track_count}`
- *dvd/bluray/vhs:* `{runtime_minutes, genres, original_language, tagline, release_date, cast}`

---

### `work_creator`

Junction table linking works to creators with a role. Composite primary key `(work_id, creator_id, role)` allows the same person to have multiple roles on one work (director + writer).

| Column | Type | Notes |
|--------|------|-------|
| work_id | integer FK → work (CASCADE DELETE) | |
| creator_id | integer FK → creator | |
| role | varchar(32) | `author`, `editor`, `artist`, `director`, `writer`, … |
| display_order | integer | Sort order for display |

---

### `item`

Physical copy of a work.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| work_id | integer FK → work INDEX | |
| branch_id | integer FK → branch | |
| barcode | varchar(64) UNIQUE | Printed on the item |
| accession_number | varchar(64) UNIQUE | Sequential accession number |
| call_number | varchar(64) | Shelf location call number |
| location | varchar(256) | Free-text shelf description |
| condition | varchar(16) | `good`, `fair`, `poor` |
| status | varchar(16) INDEX | `available`, `checked_out`, `on_hold`, `claims_returned`, `lost`, `damaged`, `withdrawn` |
| is_loanable | boolean | Default true. Per-item non-circulating flag (Koha-style) |
| loan_restriction_reason | varchar(32) NULLABLE | When `is_loanable=false`: `reference`, `in_library_use`, `archive`, `staff_only`, `display`, `other` |
| loan_restriction_note | varchar(256) NULLABLE | Free-text required when reason is `other` |
| acquired_at | date | |
| notes | text | |
| created_at | timestamptz | |
| updated_at | timestamptz | |

---

### `role`

Named permission bundle for app users.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| name | varchar(64) UNIQUE | |
| permissions | JSON | String array: `["item.view", "loan.checkout", …]` |
| is_system | boolean | If true, role cannot be edited; can be cloned |
| created_at | timestamptz | |

System roles seeded at startup: `ReadOnly`, `Patron`, `Librarian` (slimmed — explicit list, no wildcard), `SystemAdmin` (manages users, roles, infrastructure settings), `Administrator` (`["*"]` wildcard — single-person deployments). Custom roles use `["*"]` to grant all permissions.

---

### `app_user`

Authentication identity. Table name avoids the reserved word `user`.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| username | varchar(64) UNIQUE | |
| email | varchar(256) | |
| password_hash | varchar(256) | bcrypt |
| role_id | integer FK → role | |
| is_active | boolean | |
| created_at | timestamptz | |
| updated_at | timestamptz | |

---

### `patron_category`

Patron category (Adult/Child/Staff/Teacher) used for category-aware loan policies.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| code | varchar(32) UNIQUE | e.g. `adult`, `child`, `staff`, `teacher` |
| display_name | varchar(64) | |
| is_default | boolean | Exactly one row may be the default; assigned to new patrons |
| created_at | timestamptz | |

System defaults seeded at startup: `adult` (default), `child`, `staff`, `teacher`.

---

### `patron`

Borrower record. `user_id` is nullable — card-only patrons (children, guests) can borrow without an app account.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| user_id | integer FK → app_user NULLABLE | Partial unique index `ix_patron_user_id_unique` (`WHERE user_id IS NOT NULL`) enforces 1:1 at the DB level |
| category_id | integer FK → patron_category NULLABLE INDEX | Drives category-aware loan policies |
| library_card_number | varchar(64) UNIQUE | |
| full_name | varchar(256) | |
| contact_email | varchar(256) | |
| contact_phone | varchar(64) | |
| address | JSON | |
| notes | text | |
| is_active | boolean | |
| receive_notifications | boolean | Default true; patron self-service opt-out |
| expires_at | date NULLABLE | Card expiry. Past-expiry patrons can't check out (holds still allowed) |
| created_at | timestamptz | |
| updated_at | timestamptz | |

---

### `loan_policy`

Configures loan duration, renewal limits, and fine rates, optionally scoped to a media type.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| name | varchar(128) | |
| media_type_id | integer FK → media_type NULLABLE | Null = applies to all types |
| patron_category_id | integer FK → patron_category NULLABLE | Null = applies to all categories |
| loan_period_days | integer | |
| max_renewals | integer | |
| is_default | boolean | At least one policy must be default; swap is atomic at service level |
| overdue_fine_per_day_cents | integer NULLABLE | Null or 0 = no overdue fine for this policy |
| overdue_fine_cap_cents | integer NULLABLE | Max overdue fine per loan |
| grace_period_days | integer | Default 0; days overdue before fines accrue |
| lost_item_default_cents | integer NULLABLE | Fallback replacement cost |
| lost_item_processing_fee_cents | integer NULLABLE | Flat fee added on lost declarations |

Resolution precedence at runtime: (media+category) > (media,any) > (any,category) > default. Media wins the tiebreaker between #2 and #3 (Koha convention).

---

### `loan`

Single checkout record. Active loans have `returned_at IS NULL`. History lives in the same table.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| item_id | integer FK → item INDEX | |
| patron_id | integer FK → patron INDEX | |
| branch_id | integer FK → branch | |
| checked_out_at | timestamptz | |
| due_at | timestamptz | |
| returned_at | timestamptz INDEX | NULL = active loan |
| renewal_count | integer | |
| notes | text | |

**Rationale for single table:** partial indexes on `WHERE returned_at IS NULL` keep active-loan queries fast without a separate table.

---

### `hold`

Work-level reservation. Any available copy satisfies the hold; copy-level holds are not supported in v1.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| work_id | integer FK → work INDEX | |
| patron_id | integer FK → patron INDEX | |
| branch_id | integer FK → branch | |
| held_item_id | integer FK → item NULLABLE | When hold is `available`, this records which copy is on the pickup shelf (ON DELETE SET NULL) |
| status | varchar(16) INDEX | `waiting`, `available`, `fulfilled`, `cancelled`, `expired` |
| placed_at | timestamptz | |
| expires_at | timestamptz | Pickup-shelf or queue expiry; maintenance job expires past-deadline rows |
| notified_at | timestamptz | Set when status → `available` and `hold_ready` notification is queued |
| suspended_until | date NULLABLE | If set, queue-promotion skips this hold until this date; auto-resume via maintenance command |
| suspended_reason | varchar(256) NULLABLE | Patron-supplied free text |

---

### `notification`

Outbox row for a queued or sent notification. Each row stores subject + body pre-rendered at queue time, so later data edits don't retroactively rewrite pending messages.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| recipient_patron_id | integer FK → patron NULLABLE | |
| recipient_email | varchar(256) | Snapshot of patron email at queue time |
| template_key | varchar(32) | `hold_ready`, `due_soon`, `overdue` |
| context | JSON | Render context (for debugging) |
| subject | text | Pre-rendered |
| body | text | Pre-rendered (plain text) |
| status | varchar(16) | `pending`, `sent`, `failed`, `cancelled` |
| attempts | integer | Sends attempted |
| last_error | text | Last failure reason |
| loan_id | integer FK → loan NULLABLE | Dedup anchor for due_soon/overdue |
| hold_id | integer FK → hold NULLABLE | Dedup anchor for hold_ready |
| discriminator | integer | Renewal count (due_soon) or tier (overdue); 0 for hold_ready |
| scheduled_for | timestamptz | Earliest delivery time |
| sent_at | timestamptz | Set on success |
| created_at | timestamptz | |

**Indexes:**
- `ix_notification_status` on `(status)`
- `ix_notification_scheduled` partial on `(scheduled_for)` WHERE `status='pending'`
- `ix_notification_loan_dedup` partial unique on `(loan_id, template_key, discriminator)` WHERE `loan_id IS NOT NULL AND status != 'cancelled'`
- `ix_notification_hold_dedup` partial unique on `(hold_id, template_key, discriminator)` WHERE `hold_id IS NOT NULL AND status != 'cancelled'`

---

### `fine`

Outstanding or resolved charges owed by a patron. One row per definitive charge; projected overdue amounts for active loans are computed on demand and not materialized until checkin or explicit assessment.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| patron_id | integer FK → patron INDEX | |
| loan_id | integer FK → loan NULLABLE | |
| item_id | integer FK → item NULLABLE | |
| kind | varchar(16) | `overdue`, `lost`, `damaged`, `processing`, `other` |
| amount_cents | integer | Always positive |
| status | varchar(16) | `outstanding`, `paid`, `waived` |
| assessed_at | timestamptz | |
| resolved_at | timestamptz | Set when paid or waived |
| reason | text | Optional short reason |
| note | text | Optional free-text note; waive reasons appended here |
| resolved_by_user_id | integer FK → app_user NULLABLE | User who paid/waived |

**Indexes:**
- `ix_fine_patron_status` on `(patron_id, status)`
- `ix_fine_loan` on `(loan_id)`
- `ix_fine_overdue_uniq` partial unique on `(loan_id)` `WHERE status = 'outstanding' AND kind = 'overdue'` — enforces idempotent overdue materialization

---

### `audit_log`

Append-only log of administrative mutations (Librarian and System tier). Routine circulation (loans, returns) is not audited — the loan table itself carries that history.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| occurred_at | timestamptz INDEX | |
| user_id | integer FK → app_user NULLABLE | Null for system/CLI actions |
| actor_label | varchar(128) | Username or label at time of action |
| source | varchar(16) | `web`, `api`, `cli`, `system` |
| entity_type | varchar(32) | `work`, `item`, `patron`, `patron_category`, `policy`, `role`, `fine`, `creator`, `user`, `notification`, `site_setting` |
| entity_id | integer NULLABLE | |
| action | varchar(32) | `create`, `update`, `delete`, `withdraw`, `claim_returned`, `claim_verified`, `hold_suspend`, `setting_update`, `setting_reset`, … (see `services/audit.AuditAction` for the full set) |
| details | JSON | Snapshot of changed fields |

**Compound indexes:**
- `ix_audit_log_entity` on `(entity_type, entity_id, occurred_at)`
- `ix_audit_log_user_time` on `(user_id, occurred_at)`

---

### `site_setting`

DB-backed override layer for runtime-configurable settings (introduced in slice A; descriptors in `services/settings_registry.py`). Read order: env var → DB row → registry default. Each row records who changed it and when.

| Column | Type | Notes |
|--------|------|-------|
| key | varchar(128) PK | Setting key (e.g. `library_name`, `smtp_host`) |
| value | text | JSON-encoded value; nullable settings store `""` for None |
| updated_at | timestamptz | Doubles as the in-memory cache's epoch source |
| updated_by_id | integer FK → app_user NULLABLE | ON DELETE SET NULL |

Writes go through `services/site_settings.set_site_setting()`, which also emits a `SETTING_UPDATE` audit entry with `{key, before, after}`. Schema-side, the registry is the source of truth for which keys are valid; arbitrary keys can be inserted but reads will fail with `UnknownSettingError`.

---

## Key constraints summary

| Table | Unique constraints |
|-------|-------------------|
| media_type | code |
| branch | code |
| creator | (none; deduplication by sort_name at service level) |
| work | isbn (where non-null), upc (where non-null) |
| item | barcode, accession_number |
| role | name |
| app_user | username |
| patron | library_card_number |

---

### `library_hours`

One row per weekday; drives closed-day checks and due-date rolling (see `CalendarService`).

| Column | Type | Notes |
|--------|------|-------|
| weekday | integer PK | 0=Monday … 6=Sunday (ISO Python convention) |
| is_open | boolean | Whether the library is open that day |
| open_time | time NULLABLE | Opening time in local library hours |
| close_time | time NULLABLE | Closing time; due dates roll to this moment. Falls back to 23:59 if NULL |

Seeded at `db init` / first migration with 7 rows (all open, 00:00–23:59), which preserves pre-calendar due-date behaviour until a librarian configures hours.

---

### `curated_list`

Librarian-curated named collection of works ("Staff picks", "Summer reads"). `slug` is the external-facing identifier — internal IDs are never exposed.

```sql
CREATE TABLE curated_list (
    id            INTEGER PRIMARY KEY,
    slug          VARCHAR(96) NOT NULL UNIQUE,
    name          VARCHAR(256) NOT NULL,
    description   TEXT,
    is_public     BOOLEAN NOT NULL DEFAULT 1,
    is_featured   BOOLEAN NOT NULL DEFAULT 0,
    display_order INTEGER NOT NULL DEFAULT 0,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME
);
```

---

### `curated_list_entry`

One row per work in a curated list. Composite PK `(list_id, work_id)` prevents duplicates; cascades delete with the parent list.

```sql
CREATE TABLE curated_list_entry (
    list_id       INTEGER NOT NULL REFERENCES curated_list(id) ON DELETE CASCADE,
    work_id       INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE,
    display_order INTEGER NOT NULL DEFAULT 0,
    annotation    TEXT,
    PRIMARY KEY (list_id, work_id)
);
CREATE INDEX ix_curated_list_entry_list_id ON curated_list_entry(list_id);
```

---

### `closed_date`

Holidays, breaks, and one-off closures. Closed dates override the weekday schedule.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| start_date | date | Inclusive start of the closure |
| end_date | date | Inclusive end (same as start for single-day closures) |
| label | varchar(128) NULLABLE | Human-readable name (e.g. "Christmas") |
| recurs_annually | boolean | When true, the closure repeats on the same month/day every year |

Index: `ix_closed_date_start` on `start_date`.

---

### `scan_pairing`

A staff member's paired phone-scanner session (remote phone scanner). The pre-claim row is created when a librarian generates a pairing QR, then upgraded in place when a phone claims it — only the SHA-256 of the *current* secret (claim secret before claim, session secret after) is stored, never the raw secret. Terminal (expired/revoked) rows are pruned by `compendium maintenance prune-scan-pairings`.

```sql
CREATE TABLE scan_pairing (
    id                 INTEGER PRIMARY KEY,
    token_hash         VARCHAR(64) NOT NULL,      -- SHA-256 of the current secret; raw secret never stored
    user_id            INTEGER NOT NULL REFERENCES app_user(id),
    allowed_modes      JSON NOT NULL,             -- subset of ["checkout","checkin","catalog"] set at pairing time
    mode               VARCHAR(16) NOT NULL,      -- current scanner mode
    borrower_patron_id INTEGER REFERENCES patron(id),  -- current borrower in checkout mode
    count              INTEGER NOT NULL DEFAULT 0, -- items handled this session
    catalog_review     BOOLEAN NOT NULL DEFAULT 0, -- hold catalog scans for desk review (review-first)
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at         DATETIME NOT NULL,
    claimed_at         DATETIME,                  -- set when a phone claims the QR
    revoked_at         DATETIME                   -- set on Unpair
);
CREATE UNIQUE INDEX ix_scan_pairing_token_hash ON scan_pairing(token_hash);
CREATE INDEX ix_scan_pairing_expires_at ON scan_pairing(expires_at);
```

---

### `scan_event`

Append-only desk live-feed log; one row per non-ignored phone dispatch (the 2-second idempotency collapse, `kind="ignored"`, is never written). Pruned alongside its terminal pairing.

```sql
CREATE TABLE scan_event (
    id          INTEGER PRIMARY KEY,
    pairing_id  INTEGER NOT NULL REFERENCES scan_pairing(id),
    mode        VARCHAR(16) NOT NULL,
    kind        VARCHAR(16) NOT NULL,    -- "ok" | "error"
    message     VARCHAR(255) NOT NULL,
    item_id     INTEGER REFERENCES item(id),
    patron_id   INTEGER REFERENCES patron(id),
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ix_scan_event_pairing_id ON scan_event(pairing_id);
```

---

### `scan_pending_item`

A catalog scan held for desk review (review-first mode, `scan_pairing.catalog_review`). No Item exists yet; `meta_json` is the fetched metadata snapshot used to create the Work+Item on approve. The review queue spans all of a librarian's pairings and survives unpairing, so un-resolved (`status="pending"`) rows are never pruned.

```sql
CREATE TABLE scan_pending_item (
    id              INTEGER PRIMARY KEY,
    pairing_id      INTEGER NOT NULL REFERENCES scan_pairing(id),
    isbn            VARCHAR(20) NOT NULL,
    title           VARCHAR(512) NOT NULL,
    meta_json       JSON NOT NULL,        -- metadata snapshot; Work+Item created from it on approve
    cover_url       VARCHAR(1024),        -- upstream cover URL (rendered via the same-origin /ui/covers proxy)
    status          VARCHAR(16) NOT NULL DEFAULT 'pending',  -- pending | approved | discarded
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at     DATETIME,
    resolved_by     INTEGER REFERENCES app_user(id),
    created_item_id INTEGER REFERENCES item(id)   -- the Item created on approve
);
CREATE INDEX ix_scan_pending_item_pairing_id ON scan_pending_item(pairing_id);
```

---

### `deleted_entity`

Trash row for recoverable deletion. One row per deleted entity graph (currently only works); `payload` is a versioned JSON snapshot of the entity and everything it owns (see `docs/architecture.md` → "Recoverable work deletion (trash)"). Restoring re-inserts the payload under fresh PKs and removes this row.

```sql
CREATE TABLE deleted_entity (
    id          INTEGER PRIMARY KEY,
    entity_type VARCHAR(32) NOT NULL,   -- "work" (only value today)
    entity_id   INTEGER NOT NULL,       -- original id at time of deletion
    label       VARCHAR(512) NOT NULL,  -- human-readable summary, e.g. "Dune — 2 copies"
    payload     JSON NOT NULL,          -- versioned snapshot; see PAYLOAD_VERSION
    deleted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_by  INTEGER REFERENCES app_user(id)
);
CREATE INDEX ix_deleted_entity_type_deleted_at ON deleted_entity(entity_type, deleted_at);
```

---

## Migration history

| Revision | Description |
|----------|-------------|
| `cec97d4626cf` | Initial schema |
| `6e72f68cf15a` | Add auth (app_user, role) |
| `a9b9ea15933f` | Add holds and loan policies |
| `b3c4d5e6f7a8` | Add audit log |
| `11dbd4cede12` | Add search_text + FTS (SQLite FTS5 / Postgres GIN) |
| `c1d2e3f4a5b6` | Add branch.default_classification_scheme |
| `d4e5f6a7b8c9` | Add item.is_loanable + restriction reason/note |
| `e5f6a7b8c9d0` | Add fines (Fine entity, policy fine columns, item.lost/damaged status) |
| `f6a7b8c9d0e1` | Add notification table + patron.receive_notifications |
| `a7b8c9d0e1f2` | Add hold.held_item_id (immediate hold promotion) |
| `b8c9d0e1f2a3` | Add patron_category + patron.category_id/expires_at + loan_policy.patron_category_id |
| `c9d0e1f2a3b4` | Add hold.suspended_until / suspended_reason |
| `d0e1f2a3b4c5` | Add site_setting (env→DB→default settings) |
| `e1f2a3b4c5d6` | Split admin roles (Administrator + SystemAdmin presets, slim Librarian) |
| `f2a3b4c5d6e7` | Add failed_login table |
| `a8b9c0d1e2f3` | Add branch.location_code |
| `b2c3d4e5f6a7` | Add counters table (auto-sequence for accession numbers) |
| `c3d4e5f6a7b8` | Revamp identifiers (barcode format + external ID normalization) |
| `d7e8f9a0b1c2` | Add work.sort_title (article-ignoring catalog sort key) |
| `e9f0a1b2c3d4` | Fix failed_login.id column type (Integer, not BigInteger, for SQLite autoincrement) |
| `fab1c2d3e4f5` | Add patron.account.manage permission to Librarian; partial unique index on patron.user_id |
| `611abe9ea6e5` | Add metadata_cache table (persistent external-lookup cache) |
| `a1b2c3d4e5f6` | Add app_user.password_changed_at (backfilled; pwd_iat JWT-invalidation claim) |
| `1b17e2ba445c` | Consolidate barcode settings (barcode_length + barcode_location_enabled → barcode_format) |
| `c4d5e6f7a8b9` | Add library_hours + closed_date; calendar.manage permission on Librarian |
| `5b9e539aca65` | Add household table + patron.household_id; household.manage permission on Librarian |
| `443891bfaa50` | Add item_note table (condition / lifecycle history trail) |
| `b1c2d3e4f5a6` | Add curated_list + curated_list_entry; curatedlist.manage permission on Librarian |
| `c2d3e4f5a6b7` | Add scan_pairing (remote phone-scanner sessions) |
| `d3e4f5a6b7c8` | Add scan_event + scan_pending_item; scan_pairing.catalog_review (desk live feed + review queue) |
| `0c0bf7eed591` | Add deleted_entity (recoverable work deletion / trash); work.delete permission on Librarian |
