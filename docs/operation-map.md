# Operation map — canonical buckets

This document is the source of truth for which conceptual bucket each
operation belongs to. When adding a new operation, declare its bucket here
**before** implementing it. Both CLI and REST API must place the operation in
the same bucket, while each interface keeps its own native idiom.

## Buckets

| Bucket | Operations | CLI surface | API surface |
|---|---|---|---|
| **circulation** | checkout, checkin, renew, active, loan list, loan history, item-history | `compendium loan …` | `POST /loans/checkout`, `/loans/{id}/{checkin,renew}`, `GET /loans`, `/loans/patron/{card}`, `/loans/item/{barcode}` |
| **item-lifecycle** | add, add-manual, edit, show, list, withdraw, set-loanable, notes; **declare-lost, mark-damaged, clear-lost, clear-damage** | `compendium item …` | `GET/PATCH /items/{barcode}`, `POST /items/{barcode}/{withdraw,loanable,lost,damaged,clear-lost,clear-damage}` |
| **claims** | patron disputes return, librarian verifies or writes off, open-claims list | `compendium claim …` | `GET /claims`, `POST /claims/{barcode}/{returned,verify,write-off}` |
| **holds** | place, cancel, suspend/resume, queue, list | `compendium hold …` | `GET/POST /holds`, `/holds/{id}`, `/holds/{id}/{suspend,resume}`, `/holds/queue/{work_id}` |
| **fines** | list, pay, waive, assess manual, assess overdue per patron | `compendium fine …` | `GET /fines`, `POST /fines/{id}/{pay,waive}`, `GET/POST /patrons/{card}/fines/…` |
| **catalog** | work search/browse, work edit, creator management | `compendium work …`, `compendium creator …` | `GET /works/search`, `/works/new-arrivals`, `/works/recently-returned`, `PATCH /works/{id}`, `PUT /works/{id}/creators` |
| **patron self-service** | patron renews own loan, places/cancels own hold, disputes return | `compendium loan renew --self` (if added) | `GET /me/loans`, `POST /me/loans/{id}/{renew,claim-returned}`, `GET/POST/DELETE /me/holds`, `/me/holds/{id}/{suspend,resume}`, `GET /me/fines` |
| **admin** | settings, calendar, users, roles, policies, branches | `compendium settings …`, `compendium calendar …`, `compendium user …`, etc. | `GET/PATCH /settings/{key}`, `/library-hours`, `/closed-dates`, `/users`, `/policies`, `/branches` |
| **bulk** | import, export | `compendium import …`, `compendium export …` | `POST /import/{csv,…}`, `GET /export/{csv,…}` |
| **reports/labels** | usage reports, label printing | `compendium reports …`, `compendium labels …` | `GET /reports/{…}`, `GET /labels/{…}` |
| **ops** | maintenance crons, backup, db migrations (CLI-only by design) | `compendium maintenance …`, `compendium backup`, `compendium db …` | — |

## Approved parity exceptions

| Operation | Interface | Justification |
|---|---|---|
| `db init/upgrade/history` | CLI-only | Deployment-time migrations; OS-level trust required |
| `maintenance *` | CLI-only | Cron-invoked; must run without the daemon |
| `backup` / `restore` | CLI-only | Admin/ops territory; needs OS-level trust |
| `POST /me/loans/{id}/claim-returned` | API/Web-only | Patron self-service uses loan_id (patron sees their own loan list); the librarian path uses barcode via `POST /claims/{barcode}/returned` |
| `/ui/login`, `/ui/logout` | Web-only | CLI trust is OS-level; no login needed |
| `/ui/kiosk/*` | Web-only | Physical self-checkout terminal; no CLI analog |
| `/ui/scan/*` | Web-only | Phone-as-scanner pairing: QR generation, phone claim/dispatch are inherently interactive browser flows tied to a physical device; no meaningful CLI analog |

## Naming conventions

- **CLI**: noun-group then imperative verb (`item declare-lost`, `claim verify`).
  Multi-word names are hyphenated (`patron-category`, `curated-list`). Verb-noun
  compounds also hyphenated (`set-loanable`, `link-user`, `write-off`).
- **API**: pluralized kebab-case resource nouns as prefixes (`/items`, `/claims`,
  `/curated-lists`). CRUD uses HTTP-method semantics; state transitions use
  `POST` sub-resources (`/loans/{id}/checkin`, `/claims/{barcode}/verify`).
  Path params use external identifiers where one exists (`{barcode}`,
  `{card_number}`, `{slug}`, `{username}`), internal IDs only otherwise.
