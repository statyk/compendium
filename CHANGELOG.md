# Changelog

All notable changes to Compendium are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
