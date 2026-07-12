# Security audit — 2026-04-19

Scope: full codebase review of the running `compendium` package, focused on
authorization, input handling, rendering safety, CSRF, cookies, secrets, SSRF,
and test coverage. Baseline before changes: 276 tests passing.
Post-change: **293 tests passing** (17 new regression tests).

## Fixed in this audit

### Critical

**1. IDOR on legacy `/holds` API endpoints.**
`POST /holds`, `GET /holds?card_number=…`, and `DELETE /holds/{id}` required
only the `hold.place.self` / `hold.view.self` permission and accepted an
arbitrary `card_number` / `hold_id` without checking ownership. Any Patron-role
user with a valid JWT could place, list, or cancel holds on any other patron's
account.

- `DELETE /holds/{id}` was especially broken: it looked up the hold, then
  passed the *hold owner's* `patron_id` into `HoldService.cancel` — guaranteeing
  the service check passed because the route had already self-approved.
- Fix: `src/compendium/api/routes/holds.py` — callers without
  `hold.{place,view}.any` are required to match the target `card_number` /
  `hold.patron_id` against their own linked Patron record. `.any` scope is now
  honored for librarians.
- Regression tests: `tests/integration/test_api_authz.py::TestLegacyHoldsIDOR`
  (4 tests, covering place/list/cancel cross-patron and the self-happy-path).

### High

**2. Stored/reflected XSS in web HTMLResponse f-strings.**
Seven routes concatenated user-supplied data — barcodes, card numbers, ISBN
identifiers, exception messages that can carry work titles from external
sources — directly into HTML strings without escaping. Jinja autoescape does
not apply to `HTMLResponse(f"…")`.

Affected files (all fixed by wrapping interpolated values in
`markupsafe.escape`):
- `src/compendium/web/routes/me.py` — `/me/loans/{id}/renew`,
  `/me/holds/{id}/cancel`
- `src/compendium/web/routes/circ.py` — `/circ/checkout`, `/circ/checkin`,
  `/circ/renew`
- `src/compendium/web/routes/items.py` — `/items/lookup`, `/items/{barcode}/withdraw`
- `src/compendium/web/routes/catalog.py` — `/catalog/{id}/hold`
- `src/compendium/web/routes/patrons.py` — `/patrons/{card}/deactivate`
- `src/compendium/web/routes/users.py` — `/users/{username}/deactivate`

Also added: `quote()` around exception text inserted into redirect query strings
(`users.py`, `policies.py`, `roles.py`) to prevent CRLF or URL-splitting via
error messages.

Regression tests: three XSS tests in `tests/integration/test_web_ui.py`
covering circ-checkout, me-renew, and item-lookup paths.

### Medium

**3. `get_optional_user` ignored `is_active`.**
`src/compendium/api/deps.py::get_optional_user` (used by the guest-capable
search endpoints) returned a deactivated user if their still-unexpired JWT was
presented. `get_current_user` checked `is_active` correctly; the optional-path
did not.

- Fix: added `is_active` check, returning `None` (treat as anonymous) for
  deactivated users.
- Regression test:
  `TestInactiveUserTokens::test_inactive_user_treated_as_anonymous_on_optional_endpoint`.

**4. Login redirect loop on insufficient permission.**
`require_web_permission` raised `RequiresLoginException` when a *logged-in*
user lacked a permission, producing a 303 redirect back to `/ui/login` — the
user was already authenticated, so after logging in again they'd bounce back
to the same redirect. Fix: raise `HTTPException(403)` instead (it's an
authorization failure, not an authentication one).

Regression test:
`test_patron_sees_403_not_login_redirect_on_librarian_page`.

**5. Cookies lacked `Secure`.**
Both `AUTH_COOKIE` and `csrf_token` were set with `HttpOnly` + `SameSite=Strict`
but no `Secure` flag. Added `COMPENDIUM_SECURE_COOKIES` setting (default
`false` for local dev; set `true` in production HTTPS deployments). Deployment
doc update is noted below.

**6. Self-deactivation lockout.**
`POST /users/{username}/deactivate` allowed a librarian to deactivate
themselves. The last-Librarian problem isn't solved yet (needs a count
check at commit), but the trivial self-lockout is now blocked at both API and
web layer.

Regression tests:
`TestSelfDeactivation::test_librarian_cannot_deactivate_themselves` and
`…test_librarian_can_deactivate_another_user`.

**7. Open-redirect tightening.**
`/ui/login?next=…` only accepted paths starting with `/ui/`, which blocked
`https://evil.com/` but not `//evil.com/` (protocol-relative; browsers treat
as absolute) nor `/ui/\evil.com`. Fix: reject if `next` begins `/ui//` or
contains a backslash.

Regression tests:
`test_open_redirect_on_login_falls_back_to_catalog` and
`…absolute_url_falls_back`.

### Low / Hardening

**8. Baseline security headers.**
Added `_SecurityHeadersMiddleware` to `create_app()` setting
`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, and a reasonable Content-Security-Policy.

Notes on the CSP:
- `script-src 'self' 'unsafe-inline'` — a few HTMX partials still inline
  `<script>` blocks. Tighten once those are moved into `/ui/static/…`.
- `img-src` allows the two external image CDNs we legitimately embed
  (`covers.openlibrary.org`, `image.tmdb.org`).
- `worker-src 'self' blob:` for the ZXing barcode worker.
- `frame-ancestors 'none'` forbids iframing.

Regression test: `TestSecurityHeaders::test_baseline_headers_present`.

## Observations — not fixed, tracked here

- **Timing leak in `AuthService.authenticate`.** Unknown usernames return
  before the bcrypt call; known usernames pay the hash-verify cost. Measurable
  with timing analysis. Fix: call `verify_password` against a fixed dummy hash
  when the user is missing. Low priority — not worth a rushed change.

- **CSRF cookie uses the JWT secret for HMAC.** Functionally fine (compare_digest,
  urlsafe tokens). Long-term cleaner: separate `csrf_secret_key` so JWT
  rotation doesn't invalidate open forms.

- **No rate limiting on `/auth/login` or `/ui/login`.** Outside v1 scope
  per project conventions, but worth tracking. Bcrypt's inherent cost limits
  the damage somewhat.

- **Audit log has no retention/prune command.** Already tracked in CLAUDE.md
  under "Deferred from AuditLog slice".

- **INSECURE_JWT_DEFAULT warning is log-only.** A future release could refuse
  to start in production (`COMPENDIUM_ENV=production`) if the secret is still
  the default. Present warning is adequate for now.

- **Search route permission inconsistency.** `/ui/catalog/search-results`
  checks `guest_search_enabled`; `/ui/catalog/{work_id}` (work detail) does
  not. A guest can still deep-link to a work even when guest search is off.
  Cosmetic — no data exposure beyond what search would already reveal — but
  worth a small follow-up.

- **Nav template uses `item.create` for the Add-Item link** but the route
  is gated on `item.delete` (the `_PERM_MANAGE` alias). Minor UX drift;
  doesn't affect security. **Resolved** — the add-item routes now require
  `item.create` (2026-07 UX quick-wins pass); withdraw still requires
  `item.delete`.

## Clean areas (reviewed, no action needed)

- **SQL injection:** all raw `text()` queries in repositories use bound
  parameters (`:q`, `:lim`). SQLAlchemy ORM calls everywhere else.
- **SSRF / external HTTP:** all `httpx` calls in `services/metadata.py` have
  explicit timeouts (8–15s) and fixed host URLs. No user-controlled URL
  construction.
- **CSRF:** signed double-submit cookie, `hmac.compare_digest` comparisons,
  wired into every state-changing web form. Signature-tampering test now
  added.
- **Template XSS:** Jinja autoescape is on globally; all template files
  reviewed — no `|safe` on user-controlled data.
- **JWT handling:** bcrypt + HS256 PyJWT, `exp` claim enforced, explicit
  algorithm allowlist.

## Post-audit test coverage additions

| File | Tests added | Covers |
|---|---|---|
| `tests/integration/test_api_authz.py` | 9 | `/holds` IDOR (4), inactive user (2), self-deactivation (2), security headers (1) |
| `tests/integration/test_web_ui.py` | 8 | XSS escaping (3), open-redirect (2), 403-vs-redirect (1), CSRF signature tampering (1), inactive-user cookie (1) |
| **Total** | **17** | — |

Final suite: **293 passed** in ~107s.

## Deployment docs — follow-up

`docs/deployment.md` should gain an entry for `COMPENDIUM_SECURE_COOKIES`:

```dotenv
# Set to true in production when serving over HTTPS.
# When true, auth and CSRF cookies are marked Secure and browsers
# will refuse to send them over plain HTTP.
COMPENDIUM_SECURE_COOKIES=true
```

Not added in this pass to avoid scope creep — recommend adding alongside the
existing JWT/SSL settings table.
