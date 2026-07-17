# Operation map — canonical buckets

This document is the source of truth for which conceptual bucket each
operation belongs to. When adding a new operation, declare its bucket here
**before** implementing it. Both CLI and REST API must place the operation in
the same bucket, while each interface keeps its own native idiom.

> **Note (1.6.0):** several CLI command spellings changed to the `add`/`edit`
> convention (see `docs/architecture.md` → "CLI conventions"); old spellings
> keep working as permanent hidden aliases. This was a spelling change only —
> no operation moved buckets.

## Buckets

| Bucket | Operations | CLI surface | API surface |
|---|---|---|---|
| **circulation** | checkout, checkin, renew, active, loan list, loan history, item-history | `compendium loan …` | `POST /loans/checkout`, `/loans/{id}/{checkin,renew}`, `GET /loans`, `/loans/patron/{card}`, `/loans/item/{barcode}` |
| **item-lifecycle** | add, add-manual, edit (incl. loanable toggle), show, list, withdraw, notes; **declare-lost, mark-damaged, clear-lost, clear-damage** | `compendium item …` | `GET/PATCH /items/{barcode}`, `POST /items/{barcode}/{withdraw,loanable,lost,damaged,clear-lost,clear-damage}` |
| **claims** | patron disputes return, librarian verifies or writes off, open-claims list | `compendium claim …` | `GET /claims`, `POST /claims/{barcode}/{returned,verify,write-off}` |
| **holds** | place, cancel, suspend/resume, queue, list | `compendium hold …` | `GET/POST /holds`, `/holds/{id}`, `/holds/{id}/{suspend,resume}`, `/holds/queue/{work_id}` |
| **fines** | list, pay (optionally partial, with an amount and note), waive, assess manual, assess overdue per patron | `compendium fine …` (`pay --amount/--note`) | `GET /fines`, `POST /fines/{id}/{pay,waive}` (`pay` body accepts optional `amount_cents`/`note`), `GET/POST /patrons/{card}/fines/…` |
| **patrons** | add, edit (name/email/phone contact fields), show, list/search, deactivate/reactivate, link/unlink user, create account | `compendium patron …` (`edit`/`set --name/--email/--phone`) | `POST /patrons`, `GET /patrons`, `GET /patrons/{card}`, `PATCH /patrons/{card}` (name/email/phone), `POST /patrons/{card}/{deactivate,reactivate,account}` |
| **catalog** | work search/browse, work edit, creator management, work delete, trash list/restore/purge | `compendium work …`, `compendium creator …`, `compendium work trash …` | `GET /works/search`, `/works/new-arrivals`, `/works/recently-returned`, `PATCH /works/{id}`, `PUT /works/{id}/creators`, `DELETE /works/{id}`, `GET /trash`, `POST /trash/{id}/restore`, `DELETE /trash/{id}` |
| **patron self-service** | patron renews own loan, places/cancels own hold, disputes return | `compendium loan renew --self` (if added) | `GET /me/loans`, `POST /me/loans/{id}/{renew,claim-returned}`, `GET/POST/DELETE /me/holds`, `/me/holds/{id}/{suspend,resume}`, `GET /me/fines` |
| **admin** | settings, calendar, users, roles, policies, branches (name + classification scheme + location code edit; code is locked — see exceptions), policy delete | `compendium settings …`, `compendium calendar …`, `compendium user …`, `compendium branch edit --name/--classification`, etc. | `GET/PATCH /settings/{key}`, `/library-hours`, `/closed-dates`, `/users`, `/policies`, `PATCH /branches/{id}` (`name`, `default_classification_scheme`), `DELETE /policies/{id}` |
| **bulk** | import, export | `compendium import …`, `compendium export …` | `POST /import/{csv,…}`, `GET /export/{csv,…}` |
| **reports/labels** | usage reports, label printing | `compendium reports …`, `compendium labels …` | `GET /reports/{…}`, `GET /labels/{…}` |
| **ops** | maintenance crons, backup, db migrations (CLI-only by design) | `compendium maintenance …`, `compendium backup`, `compendium db …` | — |

## Approved parity exceptions

| Operation | Interface | Justification |
|---|---|---|
| `db init/upgrade/history` | CLI-only | Deployment-time migrations; OS-level trust required |
| `init` | CLI-only | Deployment scaffold; runs in a shell with OS trust (often inside the image) |
| `maintenance *` | CLI-only | Cron-invoked; must run without the daemon |
| `backup` / `restore` | CLI-only | Admin/ops territory; needs OS-level trust |
| `POST /me/loans/{id}/claim-returned` | API/Web-only | Patron self-service uses loan_id (patron sees their own loan list); the librarian path uses barcode via `POST /claims/{barcode}/returned` |
| `/ui/login`, `/ui/logout` | Web-only | CLI trust is OS-level; no login needed |
| `/ui/kiosk/*` | Web-only | Physical self-checkout terminal; no CLI analog |
| `/ui/scan/*` | Web-only | Phone-as-scanner pairing: QR generation, phone claim/dispatch, desk live feed (`scan_event` table, 1500 ms HTMX poll), per-pairing `catalog_review` toggle, and desk review queue (`scan_pending_item`): `POST /ui/scan/review`, `POST /ui/scan/pairings/{id}/pending/{pid}/approve`, `.../discard`, `GET`/`POST .../edit` (inline edit modal), and the phone liveness `GET /ui/scan/heartbeat` (unpair detection). All inherently interactive browser flows tied to a physical device; no meaningful CLI analog. |

- `/ui/first-run/dismiss` + the Getting Started card — web-only by design: a
  landing-page onboarding affordance, not a library operation; the CLI's
  equivalent is the root `--help` quickstart epilog.

- **Branch create/delete** — CLI/deploy-out-of-scope on all interfaces (no
  web, API, or CLI surface). `branch.code` is permanently locked (printed on
  spine labels; changing it would orphan already-printed labels) — only
  `name`, `default_classification_scheme`, and `location_code` are editable.
  Creating a second branch would implicitly enable multi-branch UI while
  multi-branch *features* (transfers, inter-branch holds, per-branch
  policies) are deferred (roadmap item 16); revisit branch create/delete
  when that lands.

## Naming conventions

- **CLI**: noun-group then imperative verb (`item declare-lost`, `claim verify`).
  Multi-word names are hyphenated (`patron-category`, `curated-list`). Verb-noun
  compounds also hyphenated (`declare-lost`, `link-user`, `write-off`).
- **API**: pluralized kebab-case resource nouns as prefixes (`/items`, `/claims`,
  `/curated-lists`). CRUD uses HTTP-method semantics; state transitions use
  `POST` sub-resources (`/loans/{id}/checkin`, `/claims/{barcode}/verify`).
  Path params use external identifiers where one exists (`{barcode}`,
  `{card_number}`, `{slug}`, `{username}`), internal IDs only otherwise.
