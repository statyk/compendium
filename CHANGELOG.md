# Changelog

All notable changes to Compendium are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
