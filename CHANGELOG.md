# Changelog

All notable changes to Compendium are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Footer:** every page now shows the running app version and links to the
  README and issue tracker (external links open in a new tab).
- **SMTP settings:** cross-links to and from the notification delivery log,
  so a delivery failure is one click from the settings that caused it
  (gated on `notification.manage`).
- **Branches:** name is now editable from the web branch-edit page, CLI
  (`compendium branch edit --name`), and API (`PATCH /branches/{id}`); the
  branch code stays permanently locked (it's printed on spine labels) and
  the edit form now explains why.

### Fixed

- **Error redirects:** exception text is now URL-encoded before being
  appended to a redirect location, so messages containing `&` no longer
  corrupt the query string.
- **Curated lists:** status pills on the list and detail pages use
  theme-safe status classes instead of hardcoded colors that washed out in
  dark mode.
- **Audit log:** the details column now renders as collapsible
  pretty-printed JSON in the web UI and compact JSON in the CLI, instead of
  a raw dumped blob.
- **Create forms:** required-field markers (patrons, users, policies,
  roles, curated lists, households) are now consistent, replacing stray
  ad-hoc asterisks and filling in markers that were missing entirely.
- **Settings save:** the page now scrolls to the outcome banner after a
  save instead of leaving the user at the top of a long settings page; the
  reset checkbox is relabeled from "reset" to "Reset to default".
- **Patron fines (self-service):** kind/status cells render as
  Title Case instead of raw `snake_case` tokens, and partially-paid fines
  show a paid/remaining breakdown, matching the staff fines view.
- **Self-service tables:** keyboard focus is re-homed to the swapped row
  after a loan renewal or hold action instead of dropping to the page body.
- **Phone scanner:** mode buttons show human-readable labels
  ("Check Out" instead of `checkout`).
- **Google Books quota check:** the daily-exhaustion check compared an
  aware and a naive datetime and silently misreported "not exhausted";
  both sides are now normalized to aware UTC before comparing.
- **Patron update:** an explicit `full_name=None` is now rejected instead
  of overwriting the patron's name with the literal string `"None"`.
- **CLI:** `--limit` truncation notices were missing on `work search`,
  `work new-arrivals`, `work recently-returned`, `claim list`, `fine list`,
  `curated-list list`, and `reports popular`/`dormant`; all now report
  when results were truncated. `--help`'s quickstart epilog now renders
  each step on its own line (a missing blank line was collapsing them
  into one).
- **Money formatting:** the web pay-confirmation page and CLI `fine pay`
  now format the remaining balance as currency (e.g. `$3.00`) instead of
  a bare number.
- **Admin hub:** removed the redundant "Secrets" entry that duplicated the
  Metadata Sources link; the loan-policy list hides the Delete link on the
  default policy (the server already rejects deleting it); the settings
  breadcrumb points at the admin hub instead of a redirect.
- **CLI:** adding a second copy of an already-catalogued work via
  `item add` or `item add-manual` no longer crashes with
  `DetachedInstanceError`.

## [1.6.1] - 2026-07-17

### Fixed

- Metadata cache: two cache-miss lookups of the same `(adapter, kind, value)`
  key within one `autoflush=False` session each inserted a row, violating the
  primary key at commit (`IntegrityError` on `metadata_cache`). Cache reads
  and upserts now also see entries still pending in the session, so the
  second lookup is a cache hit instead of a duplicate insert — this also
  removes the redundant network fetch. Reported by RecordShelf (Add Album →
  Search crashed on a cold cache).

## [1.6.0] - 2026-07-16

### Added

- Discogs collection-CSV importer: migrate a vinyl/CD collection exported from
  Discogs via `compendium import discogs`, `POST /import/discogs`, or the admin
  Import page. One row per owned copy; identity/dedup anchors on the Discogs
  `release_id`; Goldmine condition grades are abbreviated (e.g. `NM/VG+`);
  cassette/file/DVD rows are reported as per-row errors. Per-copy notes now
  round-trip through native CSV export/import (previously dropped).
- Richer MusicBrainz release metadata: vinyl/CD lookups now capture the label
  catalog number, pressing country, original album year (from the release
  group, distinct from the pressing year), and up to five genres into
  `Work.extra_metadata` — all in the existing single API call, no schema
  change. Title-search candidates now carry a Cover Art Archive thumbnail URL,
  so the web add-item picker shows cover art (missing art falls back to the
  neutral placeholder).
- Patron list search and pagination: the staff patron list gains a search box
  (name, card number, or email), an Active/Inactive/All status filter, and
  pages of 50 (the previous 500-row cap is gone). `compendium patron list`
  gains `--search`, and the API gains `GET /patrons` with `q`/`status`/
  `limit`/`offset`.
- All CLI list/show commands accept `--format table|json` (reports also keep
  `csv`); tables render via rich with one consistent style, JSON is stable and
  script-friendly (stdout only, notices on stderr).
- CLI: `--yes` confirmations on `patron-category delete`, `closed-date delete`,
  `secrets clear`, `settings reset`; `--quiet` on all maintenance commands;
  `--dry-run` on `expire-holds`, `prune-metadata-cache`, `prune-cover-cache`,
  `purge-trash`; `import --fail-on-error`; truncation notices on `--limit`
  lists.
- Getting Started checklist card on the staff landing page for administrators,
  with live setup-state checks and one-click dismiss.
- `compendium --help` now ends with a quickstart (keygen → db init → user add → serve).
- Partial fine payments: the Pay action now takes an amount (defaults to the
  full balance) and an optional note, with a confirmation page in the web UI,
  `--amount/--note` on `compendium fine pay`, and optional body fields on the
  pay API. Waive notes are now optional everywhere.
- Patron name, email, and phone are editable after creation (web edit page,
  `patron set --name/--email/--phone`, API PATCH fields), and the API gains a
  `GET /patrons/{card}` show endpoint.
- Circulation desk: renew works from the item barcode alone; the patron card
  is optional and verifies the borrower when supplied.
- "Check out to this patron" shortcut on the patron page prefills the desk.
- Catalog results and work pages show aggregate copy availability ("2 of 5
  copies available"); the work page notes the earliest due date when every
  copy is on loan.
- **Admin hub:** all administration — policies, hours, branches, users, roles,
  settings, reports — now lives on one permission-aware page at **Admin →
  Admin Home** (`/ui/admin`). The old Settings hub URL redirects there.
- **Policy delete:** loan policies can be deleted from the web, CLI
  (`compendium policy delete --id N`), and API (`DELETE /policies/{id}`);
  the default policy is protected.

### Changed

- CLI: canonical verbs are now `add`/`edit` (`household add`, `role edit`,
  `patron edit`, ...); natural keys are positional (`item edit B-0001 ...`);
  `work list` replaces `item list`. Every old spelling keeps working
  indefinitely as a hidden alias — no scripts break.
- Desk checkout confirmation shows the resolved title, copy barcode, and
  patron name instead of echoing what was typed.
- Item edit uses the same condition dropdown as add-copy (legacy free-text
  values are preserved as a "(current)" option).
- Patron reactivate and account-unlink update in place like deactivate does.
- All-fields keyword searches now sort by relevance by default (best match
  first); pick another order from the sort menu to override. Field-scoped
  searches keep alphabetical order.
- The copies table labels the call number column "Shelf location".
- Suspending a hold no longer reloads the page, and the date picker refuses
  past dates up front.
- The Admin and Settings navigation dropdowns are merged into one; the kiosk
  link moved to the top-level navigation.
- The role permission picker shows plain-language descriptions (with the raw
  token secondary) and explains the `.self` / `.any` scope convention inline.
- Changing which policy is the default now asks for confirmation, and the
  policies page states exactly when edits take effect (new checkouts and
  future renewals).
- Library hours are edited and saved as a single form; leaving either the
  hours or policies page with unsaved edits now warns first.

### Fixed

- The "Checked Out" availability pill was unstyled due to a CSS class-name
  mismatch.
- Unchecking "Full access" on a role no longer silently wipes every
  permission checkbox — the previous selection (or the Librarian preset) is
  restored.

## [1.5.1] - 2026-07-12

### Fixed

- **Web self-service:** an expired session during renew/hold actions now
  returns you to the login page instead of rendering it inside the table;
  failed renew/claim/cancel/resume keep the row and its buttons with the
  error shown inline; resuming a hold shows the updated row immediately.
- **Settings:** API-key validator warnings no longer block or mislabel a
  successful save; a failed validation now offers a "Save anyway" override.
- **CLI:** `labels spine/pocket/barcode --since` rejects malformed dates
  with a usage error instead of a traceback.
- **Docker:** `docker run … compendium --version` (and `--help`, `keygen`,
  `init`) no longer fails behind database migrations; unreachable databases
  produce one clear error instead of a traceback wall.

## [1.5.0] - 2026-07-12

### Added

- Recoverable work deletion: works are deleted to a trash (snapshot of copies,
  loan/hold history, notes, creator links, list memberships) and can be
  restored from **Cataloging → Recently Deleted**, `compendium work trash`, or
  the API. Deletion is blocked while copies are on loan or have outstanding
  fines; holds are cancelled. New `work.delete` permission (added to
  the Librarian preset), `trash_retention_days` setting (default 90), and
  `compendium maintenance purge-trash` cron command.

## [1.4.2] - 2026-07-11

### Fixed

- **Circulation desk:** the barcode field is selected after each scan, so the
  next scan replaces it instead of appending to it.
- **Deactivate patron/user:** the confirmation is now styled as a success and
  the Reactivate control appears immediately, without a page reload.
- **Add Item permission:** the add-item pages now require `item.create`
  (matching the menu that advertises them) instead of `item.delete`; withdraw
  still requires `item.delete`.
- **Catalog search:** a zero-result "All fields" search now explains that it
  matches whole words and suggests the Title/Author fields for partial words;
  the empty-catalog "Add a work" button is hidden from visitors who can't use it.
- **CLI:** malformed dates passed to `patron add/set --expires` and
  `reports popular/dormant` produce a clean usage error instead of a traceback.

## [1.4.1] - 2026-06-16

### Fixed

- **Docker image build:** the builder stage now copies the `docker/` bundle into
  its context so the wheel's `compendium/_scaffold` force-include resolves. The
  1.4.0 container image failed to build for this reason; the 1.4.0 PyPI package is
  unaffected.

## [1.4.0] - 2026-06-16

### Added

- **One-command Docker install:** `compendium init [DIR]` scaffolds a ready-to-run
  deployment (compose file, nginx config, cron helpers, and a `.env` with freshly
  generated secrets) without cloning the repo, and a `curl … | sh` bootstrap
  (`install.sh`) does it end-to-end via the published image. The deploy bundle now
  ships inside the package/image, so the same files back both the repo and the CLI.

## [1.3.0] - 2026-06-14

### Added

- **Circulate by ISBN/UPC:** checkout, checkin, and renew now accept a book's
  printed ISBN barcode or a disc's UPC when the scanned code is not a
  Compendium item barcode — no printed labels needed for home/classroom use.
  Checkout picks an available copy automatically; an ambiguous checkin
  (several copies of the title on loan) shows a copy picker at the desk.
  New site setting `circulation_scan_isbn_enabled`
  (env `COMPENDIUM_CIRCULATION_SCAN_ISBN_ENABLED`, default on) at
  Admin → Settings → Circulation.

### Changed

- **Timezone setting is now a picker.** Admin → Settings → General → Library
  Timezone is a country → city dropdown pair instead of a free-form text
  field, sourced from the canonical IANA zone tables. UTC and the common
  English-speaking locales (US, Canada, UK, Ireland, Australia, New Zealand)
  are pinned to the top; the rest follow alphabetically.
- **Fresh installs no longer preload nav shortcuts.** The `custom_shortcuts`
  default is now empty; each library opts in via Admin → Settings → General.
  Existing deployments keep their configured shortcuts.

### Fixed

- **Menus only show links the user can use.** The site-wide nav shortcuts are
  now filtered by the signed-in user's permissions (a patron no longer sees —
  and 403s on — staff shortcuts like Circ Desk), and the catalog detail page
  only links a copy's barcode to the staff item page for users with
  `item.view` (anonymous visitors no longer get bounced to a login screen).
- **Patron-card barcode no longer stretches.** On the business-card patron
  card the barcode is capped to half the card width and centered, instead of
  spanning the full width.

## [1.2.1] - 2026-06-11

### Changed

- **CI:** bumped the `docker/*` actions in the release workflow to their
  Node-24-compatible major versions (`setup-qemu-action` v4, `setup-buildx-action`
  v4, `login-action` v4, `metadata-action` v6, `build-push-action` v7), ahead of
  GitHub's Node-20 runtime retirement.

### Fixed

- **Docs:** added 6 missing rows to the migration-history table in
  `docs/schema.md` so it matches `alembic history` (now 30 rows).

## [1.2.0] - 2026-06-11

### Added

- **Remote phone scanner** — pair a smartphone to the circulation desk via QR
  code. The phone camera dispatches barcodes to the desk in real time without
  installing a native app. Supports checkout, checkin, and catalog modes.
  Pairing is ephemeral: a short-TTL claim secret lives in the QR; after the
  phone claims it, the secret rotates to a session cookie; the librarian can
  unpair from the desk at any time, and the phone detects the unpair within ~5 s.
  - New settings: `public_base_url` (DB-editable, env `COMPENDIUM_PUBLIC_BASE_URL`)
    and `scan_session_minutes` (DB-editable, env `COMPENDIUM_SCAN_SESSION_MINUTES`,
    default 60).
  - The phone camera API requires a secure context, so the QR must encode an
    `https://` URL. Compendium derives the base URL from the staff request,
    honoring `X-Forwarded-Proto` — the bundled `docker/nginx` config sets it, so
    pairing works out-of-the-box behind the shipped stack. Set
    `COMPENDIUM_PUBLIC_BASE_URL` only when your proxy does not set that header, or
    to pin a specific public hostname; a non-HTTPS base URL is refused.
  - **Pairing on both the circulation desk and the Add-Item page** — both offer
    every scan mode the librarian has permission for, with page-appropriate modes
    pre-checked (desk: checkout/checkin; Add-Item: catalog).
  - **Desk live feed** — the desk page polls every 1500 ms and shows a real-time
    event log (`scan_event` table) for the active session, with barcode, mode,
    and success/error indicator per scan.
  - **Per-pairing review-first toggle** (`catalog_review` DB flag) — when enabled
    at pairing time, catalog-mode ISBN scans are held in a desk review queue
    (`scan_pending_item` table) instead of immediately creating an item. The
    librarian approves, edits (in an inline modal on the desk page — no
    navigating away), or discards each entry; approving creates the Work+Item
    from the stored metadata snapshot.
  - **Richer phone feedback** — the phone scanner page flashes a colour-coded
    result banner on each dispatch (green/red) and shows a scrollable recent-scan
    list so the operator can see the last few barcodes without looking at the desk.
  - New maintenance command: `compendium maintenance prune-scan-pairings
    --older-than-days N` — deletes terminal (expired or revoked) pairing rows and
    cascade-deletes their `scan_event` and resolved `scan_pending_item` rows.
    Pairings with un-resolved (`status="pending"`) review items are skipped
    entirely — no pending desk-review work is ever silently dropped. Suggested
    cadence: daily, `--older-than-days 7`.
  - **Public API seam (downstream consumers):** `runContinuous(video, backend,
    {onCode, onMiss})` in `scanner.js` is now a pinned public API consumed
    downstream (LitCat). Breaking changes to this signature will be called out
    explicitly in future changelog entries.

## [1.1.0] - 2026-06-01

### Added
- **Container image on GHCR** — a multi-arch (`linux/amd64` + `linux/arm64`)
  image is now built and pushed to `ghcr.io/statyk/compendium` automatically on
  every release (tags: `X.Y.Z`, `X.Y`, `latest`). Uses the built-in
  `GITHUB_TOKEN`; no Docker Hub account or secrets required.
- **`COMPENDIUM_IMAGE` env var** — pin a specific release in `.env` with
  `COMPENDIUM_IMAGE=ghcr.io/statyk/compendium:1.1.0`.
- `docker/docker-compose.build.yml` override for building the image from source.

### Changed
- **Pull-based Docker Compose default** — `docker/docker-compose.yml` now pulls
  the published image by default. Quick start is `docker compose pull &&
  docker compose up -d` instead of `docker compose up -d --build`.
- CI: bumped GitHub Actions to Node.js 24-compatible versions.

### Fixed
- Two release-workflow bugs in `.github/workflows/release.yml`.

## [1.0.2] - 2026-05-31

### Added
- `docs/releasing.md` documenting the version-bump and PyPI publish process.

### Changed
- Minor fixes and housekeeping.

## [1.0.1] - 2026-05-31

### Changed
- Documentation tweak.

## [1.0.0] - 2026-05-31

First public release.

### Added
- **Catalog** — add items by ISBN/UPC/MBID/TMDbID or title search (Google Books,
  Open Library, MusicBrainz, TMDb); faceted browse; full-text search.
- **Circulation** — checkout, checkin, renewal, lost/damaged/claims-returned;
  self-checkout kiosk mode; library hours & holiday calendar so due dates skip
  closed days.
- **Holds** — patron reservation queue with suspend/resume and auto-expiry.
- **Fines** — per-policy overdue rates, lost/damaged fees, pay/waive workflow,
  bulk assessment.
- **Notifications** — email (hold-ready, due-soon, overdue) via outbox pattern
  drained by cron.
- **Patrons** — categories, card expiry, households, optional patron↔user link
  for self-service.
- **Curated lists** — librarian-curated named shelves with annotations; featured
  lists on OPAC landing page.
- **Bulk import/export** — CSV, MARC21, MARCXML, LibraryThing TSV, GoodReads CSV.
- **Backup/restore** — portable JSONL tarballs; SQLite ↔ Postgres migration path.
- **Labels** — Avery-template item spine/pocket labels and patron cards as PDFs;
  live SVG preview.
- **Reports** — checkouts/month, popular works, weeding list, current overdues;
  CSV export.
- **Web UI** — HTMX + Jinja2; catalog search, circulation desk with camera
  barcode scanning, patron self-service, light/dark/auto theme.
- **REST API** — FastAPI; full parity with the CLI.
- **CLI** — complete librarian + sysadmin workflow without running a server.
- **Auth** — five preset roles + custom roles; JWT (API) + cookie (web);
  role-escalation guardrail.

[Unreleased]: https://github.com/statyk/compendium/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/statyk/compendium/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/statyk/compendium/compare/v1.0.2...v1.1.0
[1.0.2]: https://github.com/statyk/compendium/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/statyk/compendium/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/statyk/compendium/releases/tag/v1.0.0
