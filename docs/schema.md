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
| created_at | timestamptz | |
| updated_at | timestamptz | Set on update |

**Indexes:**
- `ix_work_isbn` on `isbn`
- `ix_work_upc` on `upc`
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
| status | varchar(16) INDEX | `available`, `checked_out`, `on_hold`, `lost`, `damaged`, `withdrawn` |
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

System roles seeded at startup: `ReadOnly`, `Patron`, `Librarian`. Use `["*"]` to grant all permissions.

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

### `patron`

Borrower record. `user_id` is nullable — card-only patrons (children, guests) can borrow without an app account.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| user_id | integer FK → app_user NULLABLE | 1:1 enforced at service level |
| library_card_number | varchar(64) UNIQUE | |
| full_name | varchar(256) | |
| contact_email | varchar(256) | |
| contact_phone | varchar(64) | |
| address | JSON | |
| notes | text | |
| is_active | boolean | |
| receive_notifications | boolean | Default true; patron self-service opt-out |
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
| loan_period_days | integer | |
| max_renewals | integer | |
| is_default | boolean | At least one policy must be default; swap is atomic at service level |
| overdue_fine_per_day_cents | integer NULLABLE | Null or 0 = no overdue fine for this policy |
| overdue_fine_cap_cents | integer NULLABLE | Max overdue fine per loan |
| grace_period_days | integer | Default 0; days overdue before fines accrue |
| lost_item_default_cents | integer NULLABLE | Fallback replacement cost |
| lost_item_processing_fee_cents | integer NULLABLE | Flat fee added on lost declarations |

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
| status | varchar(16) INDEX | `waiting`, `available`, `fulfilled`, `cancelled`, `expired` |
| placed_at | timestamptz | |
| expires_at | timestamptz | Set when status → `available`; maintenance job expires waiting holds |
| notified_at | timestamptz | Set when status → `available` (for future notification feature) |

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

Append-only log of Librarian-level mutations. Routine circulation (loans, returns) is not audited — the loan table itself carries that history.

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| occurred_at | timestamptz INDEX | |
| user_id | integer FK → app_user NULLABLE | Null for system/CLI actions |
| actor_label | varchar(128) | Username or label at time of action |
| source | varchar(16) | `web`, `api`, `cli`, `system` |
| entity_type | varchar(32) | `work`, `item`, `patron`, `policy`, `role`, `fine`, `creator`, `user` |
| entity_id | integer NULLABLE | |
| action | varchar(32) | `create`, `update`, `delete`, `withdraw` |
| details | JSON | Snapshot of changed fields |

**Compound indexes:**
- `ix_audit_log_entity` on `(entity_type, entity_id, occurred_at)`
- `ix_audit_log_user_time` on `(user_id, occurred_at)`

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

## Migration history

| Revision | Description |
|----------|-------------|
| `cec97d4626cf` | Initial schema |
| `6e72f68cf15a` | Add auth (app_user, role) |
| `a9b9ea15933f` | Add holds and loan policies |
| `b3c4d5e6f7a8` | Add audit log |
| `11dbd4cede12` | Add search_text + FTS (SQLite FTS5 / Postgres GIN) |
