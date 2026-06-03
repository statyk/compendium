# Changelog

All notable changes to Compendium are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/statyk/compendium/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/statyk/compendium/compare/v1.0.2...v1.1.0
[1.0.2]: https://github.com/statyk/compendium/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/statyk/compendium/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/statyk/compendium/releases/tag/v1.0.0
