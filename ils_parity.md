How Compendium stacks up against mainstream ILS systems (Koha, Evergreen,
Apollo, LibraryWorld, TinyCat, FOLIO, Alma, Sierra/Polaris). Updated
2026-05-30 to reflect features shipped since the 2026-04-21 analysis.

Legend:
- **[Gap-S]** — gap that matters for small libraries (home / classroom / school / club)
- **[Gap-L]** — gap primarily relevant to large/academic/consortium libraries; lower priority
- **[Schema-ready]** — data model accommodates it; feature work deferred
- **[Decided-no]** — explicitly ruled out in v1 with a rationale; kept for the record
- **[Out]** — out of scope and not planned: e-books/streaming, DDC reference data, third-party plugin discovery, remote-daemon CLI mode

---

## Shipped since the 2026-04-21 analysis

**Cataloging & metadata**
- Item notes / condition history trail — per-item dated log with auto-logging on
  condition changes and lifecycle status transitions (lost, damaged, withdrawn,
  claims-returned, and reversals); manual entries via web UI, API, and CLI;
  system entries are immutable; gated on item.view/item.edit
- MARC21 / MARCXML + CSV bulk import & export (`compendium bulk import/export`)
- LibraryThing TSV and GoodReads CSV importers
- Metadata cache (DB-backed) with Google Books as primary adapter, symmetric
  OL↔GB fallback, per-source refresh, quota sentinel, and configurable TTL
- Cover art proxy with on-disk cache + prune CLI; fallback chain across OL/GB
- Claims-returned item state (`ItemStatus.CLAIMS_RETURNED`) with patron-initiated flow
- Lost / damaged / withdrawn item states with full lifecycle workflows
- Item-level `is_loanable` flag with `LoanRestrictionReason`

**Circulation**
- Fines & fees — overdue, lost, damaged, processing kinds; grace period;
  per-policy cap; waive/pay flows; cron-driven assessment
- Self-checkout kiosk mode (`/ui/kiosk/*`)
- Library hours / closed-date calendar — `CalendarService` skips closed days
  when computing due dates and suppresses overdue fine accrual on holidays;
  per-weekday open/close hours + holiday/closure date ranges with optional
  annual recurrence; CLI (`calendar hours`, `calendar closed-date`), REST API,
  and admin web UI

**Holds & reservations**
- Hold suspend/freeze (`suspended_until` + `suspended_reason`) with manual or
  timed auto-resume via cron
- Hold pickup email notification (queued when a copy becomes available)

**Patron management**
- Patron categories + card expiry as a second loan-policy axis (`PatronCategory`,
  `Patron.expires_at`, `LoanPolicy.patron_category_id`)
- Patron card deactivation, reactivation, inactive-filter on lists
- `patron.account.manage` permission — inline Patron↔User linking without exposing
  the full user list
- Household / family linking — `Household` groups patrons; add/remove members,
  members surfaced on the patron detail page; `household.manage` permission in
  the Librarian preset; CLI / API / web parity

**OPAC**
- Search facets (media type, decade, availability)
- Landing-page "new arrivals" and "recently returned" shelves
- Autocomplete suggest dropdown in the search bar
- Curated lists / bookshelves — librarian-editable named collections of Works
  ("Staff picks", "Summer reads") with slug, description, per-work annotations,
  public/private toggle, optional landing-page shelf (is_featured); admin CRUD
  at /ui/curated-lists; public OPAC at /ui/lists; CLI compendium curated-list;
  REST API /curated-lists; new permission curatedlist.manage

**Notifications**
- Email notifications via outbox-pattern SMTP pipeline: hold-ready, due-soon,
  overdue, courtesy reminders
- Per-patron `receive_notifications` opt-out
- `compendium maintenance queue-due-soon-notices / queue-overdue-notices /
  send-queued-notifications / prune-notifications`

**Reports & analytics**
- Circulation statistics with Chart.js trend lines
- Overdue loans report
- Dormant-item and popular-work reports

**Labels, cards, barcodes**
- Item label PDF + patron-card PDF generation
- Live SVG preview, selectable barcode symbology (Codabar / Code 39 / Code 128
  with Subset C numeric density), multi-template (avery-5160, 5390-spine,
  spine-barcode, square, etc.), orientation-aware layout
- Per-field configurability on all templates

**Administration**
- Portable JSONL backup/restore with Alembic auto-migrate on restore
  (`compendium backup / restore`)
- Settings UI + hybrid env→DB→default reader for all runtime knobs
- Encrypted at-rest secrets for SMTP password, TMDb, Google Books keys
  (`compendium secrets`, Admin → Secrets)
- Audit-log prune maintenance command
- `compendium maintenance refresh-metadata` for bulk post-import enrichment
- Security hardening: nonce-based CSP, per-IP login rate limiting, HSTS,
  bcrypt tuning, CSRF HKDF, JWT aud/iat claims, Docker secrets via `*_FILE`

---

## Remaining gaps by domain

### Cataloging & metadata

- **[Gap-S]** **Tags on works** (separate from subjects), with OPAC tag-browse.
  Deferred in v1; surfaces as a lightweight alternative to full LCSH subject
  browsing.
- **[Gap-S]** **Bulk-edit items across a search result** — change location,
  branch, condition, or status for a selection. Cataloger pain point after
  large imports.
- **[Gap-S]** **Creator merge / de-duplicate tool.** OL and GoodReads both
  produce near-duplicate name variants that accumulate across imports.
- **[Gap-S]** **Cataloging templates / quick-record forms** — pre-filled form
  profiles ("new chapter book", "new vinyl LP") to speed repetitive hand-entry.
- **[Gap-S]** **Z39.50 / SRU copy cataloging** — fetch records from LoC, OCLC,
  or peer catalogs by ISBN. Lower priority given OL coverage; bumps up when
  librarians migrate from another ILS. (CLAUDE.md roadmap #15)
- **[Gap-L]** **Subject browsing index** — alphabetical LCSH/BISAC subject list
  with counts, the standard OPAC discovery surface.
- **[Gap-L]** **Authority control** — LCNAF/LCSH with cross-references.
  (CLAUDE.md roadmap #21)
- **[Gap-L]** **Series / multi-part relationships** — "Book 3 of Foundation"
  linking. Deferred in v1.
- **[Gap-L]** **MARC editor UI** — hand-editing MARC records. Only relevant when
  librarians need full bibliographic control.

### Circulation

- **[Gap-S]** **Print / email checkout receipts** at the circ desk and kiosk.
  Thermal-receipt printers are common in school libraries; an email option works
  for all others.
- **[Gap-S]** **In-library-use counter.** Anonymous "browsed / used" tally for
  items marked in-library-only; feeds weeding and usage reports.
- **[Gap-L]** **Curbside / hold-shelf pull list.** Printable list of
  waiting-hold copies to retrieve each morning — common in public libraries.
- **[Gap-L]** **Amnesty / bulk-waive workflow.** Forgive all outstanding
  overdue fines below threshold X for a date range (e.g., summer amnesty).
  Requires bulk operations not in the current fine service.
- **[Gap-L]** **Offline circulation.** Queue transactions when the server is
  down. Koha ships `koha-offline-circ`; niche but asked for by mobile/tablet
  deployments.
- **[Gap-L]** **SIP2 protocol** — for self-checkout hardware, Bibliotheca
  sorters, payment kiosks. (CLAUDE.md roadmap #17)
- **[Gap-L]** **NCIP** — interlibrary loan protocol. Consortium-tier.
  (CLAUDE.md roadmap #17)

### Holds & reservations

- **[Gap-S]** **Patron suggest-a-purchase / acquisitions wishlist.** A
  lightweight patron-initiated request ("please buy this title") that lands in a
  librarian review queue, separate from holds. Common in Koha and Apollo.
- **[Decided-no]** Item-level holds (specific copy). Explicitly not in v1; a
  Work-level hold already scopes to an edition via ISBN.
- **[Gap-L]** **Booking system for rooms / AV equipment / makerspace items.**
  Could anchor on the existing `Item` table with a new `Booking` model. Public
  libraries increasingly bundle this.
- **[Gap-L]** **Hold queue priority / booking for specific dates** (course
  reserves pattern).

### Patron management

- **[Gap-S]** **Patron self-update of contact info** (email / phone / address)
  from the account page. Currently only staff can edit patron records.
- **[Gap-S]** **Per-template notification-channel preferences.** The schema has
  a global `receive_notifications` boolean; a richer model lets patrons say
  "hold-ready by email, overdue by SMS." Depends on SMS support.
- **[Gap-S]** **Reading-history opt-in.** Patron opts to retain a permanent
  personal loan history (distinct from the operational loan table that gets
  pruned by `prune-loan-history`). GDPR-sensitive; gate behind a setting.
- **[Gap-S]** **Patron messages / account notes visible to staff.** ("Call
  parent before releasing.") There is a free-text `notes` field; surfacing it
  prominently at checkout is the gap.
- **[Gap-L]** **LDAP / OIDC / SAML SSO.** Schema already separates `AppUser`
  from `Patron`; the lift is the auth integration. School-district requirement.
- **[Gap-L]** **Patron privacy controls.** GDPR/state-law export and erasure
  endpoints at institutional scale.

### OPAC

- **[Gap-S]** **Subject / genre facet.** Current facets cover media type,
  decade, and availability; adding subject/genre requires tagging data (see Tags
  gap above) or LCSH mining from metadata.
- **[Gap-S]** **Patron-side saved searches + "alert me when items arrive."**
  Rides the notifications pipeline already in place.
- **[Gap-S]** **PWA manifest + offline shell.** Lets the OPAC install as a
  home-screen app on mobile; service worker caches the shell for offline browse.
  Low lift, high perception of polish.
- **[Gap-S]** **"Did you mean?" / spell-correct suggestions** in All-Fields
  search. All-Fields uses FTS (whole-token only); a prefix-suggest layer would
  close the usability gap without changing the FTS backend.
- **[Gap-L]** **Patron reviews / ratings.** Deferred in v1; divisive in small
  libraries.
- **[Gap-L]** **Multi-language UI (i18n).** Jinja strings need a `gettext`
  pass. Not critical for the English-first target but asked for internationally.
- **[Gap-L]** **Federated / discovery-layer search** (EDS, Primo, Summon).
  Academic-lib world. (CLAUDE.md roadmap #23)

### Notifications

- **[Gap-S]** **Per-tenant email template editing** in the admin UI. Templates
  currently live in `services/notifications/templates/`; editing requires a
  file-system deploy.
- **[Gap-L]** **SMS via Twilio / similar.** Unlocks per-template channel
  preferences (see Patron management).
- **[Gap-L]** **Web-push for the OPAC PWA.** Depends on PWA manifest (above).
- **[Gap-L]** **Outbound webhooks on circulation events.** Enables third-party
  integrations (newsletter tools, parent-notification apps, analytics).

### Reports & analytics

- **[Gap-S]** **Inventory / shelf-read workflow.** Scan all barcodes on a
  shelf → get a diff against the expected shelflist: missing, misshelved,
  status mismatch. Mobile-friendly; one of the highest-value librarian tools.
  Not in the CLAUDE.md roadmap but a natural next step after the barcode
  scanner investment.
- **[Gap-S]** **Hold fill-rate / wait-time report.** Answers "how long does a
  patron wait for popular titles?" — drives collection development.
- **[Gap-S]** **Patron-activity report** (top borrowers, dormant patrons,
  reading habits). Sensitive — gate behind `report.view` and a configurable
  retention setting.
- **[Gap-L]** **Custom report builder** (saved SQL-like queries, exports).
  (CLAUDE.md roadmap #22)
- **[Gap-L]** **Collection analytics** (turnover rate, diversity, budget per
  subject). Acquisitions-adjacent.

### Acquisitions & serials

- **[Gap-L]** **Acquisitions module** — vendors, orders, invoicing, budget
  lines, receipt-to-catalog. (CLAUDE.md roadmap #18)
- **[Gap-L]** **Serials management** — subscription tracking, issue check-in,
  binding. Academic/reference territory. (CLAUDE.md roadmap #18)
- **[Gap-L]** **EDIFACT / vendor ordering integrations.**

### Multi-branch & consortia

- **[Schema-ready]** Branch column on every relevant table; UI hides the picker
  in single-branch mode.
- **[Gap-L]** **Inter-branch transfer workflow** — in-transit state,
  pickup-at-any-branch. (CLAUDE.md roadmap #16)
- **[Gap-L]** **Per-branch policies and permissions.** (CLAUDE.md roadmap #16)
- **[Gap-L]** **Floating collections** — items don't return to a home branch.
  Consortium-tier.
- **[Gap-L]** **Union catalog across libraries.** Consortium-tier.

### Course reserves / reading lists

- **[Gap-L]** **Course reserves** — time-limited loan policy tied to a course.
  Could be expressed as a short-period patron category + a curated list.
  (CLAUDE.md roadmap #19)
- **[Gap-L]** **Instructor-curated reading lists** linked to works.
  (CLAUDE.md roadmap #19)

### Interlibrary loan

- **[Gap-L]** ILL module. (CLAUDE.md roadmap #20)

### Labels, cards, barcodes

- **[Gap-S]** **Batch label print from a search-result selection.** The label
  generator today is per-template with a work/item picker; selecting from a
  filtered item list and printing all in one pass is the UX gap.
- **[Gap-L]** **RFID / smart-shelf integration.** (CLAUDE.md roadmap #23)

### Administration

- **[Gap-S]** **Donation tracking on Item.** Donor name, donation drive,
  tax-deductible receipt generation. Schema addition + small UI. Common request
  from school and public libraries that rely on donations.
- **[Gap-S]** **Import-validation diagnostics in the admin UI.** The CLI has
  `--dry-run`; an equivalent web preview (row-by-row error table before
  committing) would help non-technical librarians.
- **[Gap-S]** **Public display / digital signage mode.** Read-only new-arrivals
  slideshow for a lobby screen; auth-free, auto-rotating.
- **[Gap-L]** **Programming / event calendar.** Library events, signups,
  capacity limits. Many small libraries want this bundled rather than running
  a separate tool.
- **[Gap-L]** **Audit retention / configurable prune policy.** `prune-audit-log`
  exists; making the retention window a DB-editable setting rounds it out.

---

## Priority analysis for the target audience

*Home / classroom / school / club libraries, with mid-size institutional
accommodated by schema. Order is a working guess — revisit before starting
each slice.*

### Tier 1 — highest-value remaining gaps for the core audience

1. **Subject / genre facet + tag-browse.** Closes the last major OPAC discovery
   gap. Depends on tags or metadata subject fields being indexed.
2. **Bulk-edit items across a search result.** Biggest daily pain point after a
   large import.
3. **Print / email checkout receipts.** Table stakes for schools that replaced
   paper-card systems; thermal receipt printer support is a common ask.
4. **PWA manifest + offline shell.** Makes the OPAC feel like a native app on
   phones — high patron perception impact for low engineering cost.

### Tier 2 — useful for mid-size institutional, lower core-audience priority

- Z39.50 / SRU copy cataloging (roadmap #15)
- Inventory / shelf-read workflow
- Patron-activity / hold fill-rate / custom report builder (roadmap #22)
- Multi-branch features, course reserves, ILL, acquisitions, serials
  (roadmap #16 / #18 / #19 / #20)
- SIP2 / NCIP, MARC authority control, RFID, federated discovery
  (roadmap #17 / #21 / #23)
- LDAP / OIDC / SAML SSO

---

## Out of scope and decided-no

**Permanently out of scope:**
- E-books and streaming media (pure-electronic items)
- DDC reference data (copyrighted by OCLC; per-book DDC numbers are fine to
  store in the free-form field)
- Third-party plugin discovery (may revisit via Python `entry_points`)
- Remote-daemon CLI mode (CLI always calls services directly in v1)

**Decided-no with rationale:**
- Item-level holds (specific copy): Work-level hold already scopes to an edition
  via ISBN; adding copy-level holds adds queue complexity for marginal benefit
  at small-library scale.
- Mocked-DB in integration tests: prior incident where mock/prod divergence
  masked a broken migration (see feedback memory).
