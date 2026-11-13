# Pallas Athena — core principles and specifications

Pallas Athena is the practice manager of one Québec civil-litigation lawyer, handling **highly privileged legal data** subject to *secret professionnel* (*Barreau du Québec*). `athena/` holds its App Engine Standard services (no system libraries; worker-local memory): the default service (`main:app`: web app, DavX5 CardDAV/CalDAV, MCP connector, cron, Cloud Tasks) and client « portail » (`client.wsgi:app`). Flask, Firestore, Firebase Storage, Jinja/HTMX/Alpine, precompiled Tailwind; no SPA, no deploy-time build — save the Python bytecode, compiled (checked-hash) by Cloud Build after tests pass. **Pushing to `main` runs all tests, then deploys to production.**

Sources of truth: `models/*`, `services/*`, `mcp/disclosure.py`, `mcp/output_schemas.py`, `utils/deadlines.py`, [LITIGATION.md](LITIGATION.md), [ACCOUNTING.md](ACCOUNTING.md), [SECURITY.md](SECURITY.md), [DEPLOYMENT.md](DEPLOYMENT.md), [athena/OBSERVABILITY.md](athena/OBSERVABILITY.md), [athena/GABARITS_PLACEHOLDERS.md](athena/GABARITS_PLACEHOLDERS.md) (`/gabarits/aide`). **`CLAUDE.md` and `GEMINI.md` must stay byte-identical and under 21 KB** (`tests/test_deployment_inventory.py`).

**The claude.ai « Athena » connector acts on PRODUCTION: never use it to test changes** (invoice numbers and register entries are permanent).

## Foundational pillars & design principles

Every architectural, backend, and UI/UX decision flows from four pillars:

### 1. Privileged data & professional secrecy (*secret professionnel*)
All records/files are confidential client material. Enforce single-user authority (`AUTHORIZED_USER_EMAIL` + MFA), strict trust boundaries (portal quarantine, scoped MCP), and zero PII in logs/traces.
- **The lawyer decides.** Machine judgments (compliance, categories/analyses) are `présumé`; name matches and délais are proposals. Only the lawyer confirms in the web UI; automated paths never confirm, overwrite, or lower confirmed choices (category, KYC/conflict status, protection).
- **Records are permanent.** Replaced note text moves to `revisions` in-transaction; budget versions, analyses, template versions, and `audit_events` are write-once. Referenced contacts/dossiers refuse deletion; hard deletes append one `audit_events` row post-commit. Sequences come transactionally from `counters/{scope}`, never reused.

### 2. Light, efficient engineering
Keep App Engine lean: stdlib first, then minimal exact-pinned pure-Python packages; server-side Jinja/HTMX/Alpine fragments with precompiled Tailwind (no SPA); flat Firestore with bounded, composite-indexed queries.
- **One door per rule.** Validation/writes live in `models/`: normalize, sanitize, validate whitelisted keys (`_sanitize_data` covers top-level strings; `_normalize` covers nested free text; callers never set identity/stamps/provenance/payment state). `services/` orchestrate without rules; `utils/` domain math imports neither Flask nor `models/`. Web, DAV, MCP, cron share one door and derived authorities (`invoice.balance_of`, `dossier.derive_prescription`, `template_fields.selected_address`). Repair/migration scripts writing around a model declare it in their docstring, regenerate `etag`/stamps, and bump CTags.
- **One source per list.** Derive lists, enums, counts, and fan-outs from one test-pinned source; never restate inventories in prose (except the data-model map). Compare keys with `==`/dict lookup, never `startswith`. Stored keys change only via one-shot migration (renames change labels). Declare exceptions in module docstrings.

### 3. Secure, reliable & fault-tolerant (fail-closed)
Never trade safety for convenience. Commit multi-record writes atomically; guard edits with `etag` compare-and-set.
- **A failed read never passes for absence.** Decisions/writes/refusals use `*_strict` readers that raise; fail-open readers serve display only. Reject blank ids (`''` matches parentless rows) and invalid ids (`document.is_addressable_id`). Truncated reads raise on totals/decisions; deletion sweeps prove completeness first. DAV returns `503` + `Retry-After` (or `500` on failed CTag)—never `404`, short listings, or false `412`.
- **Merge by presence; grow additively.** Absent keys keep stored values; present empty keys erase them. Parsers omit unreceived keys; normalizers add no unsent keys. Migrate missing legacy fields on read (`_migrate_*`) and purge on save; judge only written fields so legacy rows never lock.
- **Honest outcomes.** Refuse rather than degrade, round, default unresolvable refs, or resolve contradictions by precedence; batches are all-or-nothing. Refuse inputs `security.sanitize` alters; validate exact stored strings (incl. CRLF) in French; disabled buttons are no guard. Post-commit side effects never raise (log ids, report incomplete). Re-read uncertain commits to settle them.

### 4. Mobile-first, Material-inspired UI/UX, responsive for desktop
Material Design clarity—clean, calm, high-craft interfaces reducing cognitive load. Start at 375 px mobile and scale cleanly at `md`/`lg` into an airy desktop workspace with clear surface hierarchy, ≥44 px targets, accessible contrast, and immediate HTMX feedback (Rules 8–10).

## Development commands

```bash
# LIVE DATA: local runs use production Firestore (ADC) unless FIRESTORE_EMULATOR_HOST
# points at a RUNNING emulator (a dead port hangs every page).
cp .env.example .env    # repo root; fill it in (DEPLOYMENT.md §4)
pip install -r athena/requirements.txt
pip install -r athena/requirements-dev.txt
cd athena
python -m scripts.check_config
flask --app main run --debug    # no app.py: --app main is required
python -m pytest tests/ -q
```

- `scripts/provision.py` audits GCP read-only (`python -m scripts.provision --project <id>`).
- Re-lock `athena/requirements.in` from `athena/` with the `uv` command heading `requirements.txt`, never by hand (Dependabot `/athena` included; Linux-only).
- Cloud Build skips indexes/rules: from repo root run `firebase deploy --only firestore:indexes --project athena-pallas` (or `firestore:rules,storage`).

## Change Impact Assessment

These subsystems fail SILENTLY in production/mobile. State per item whether touched and what you verified; record touched numbers in the French commit `Impact` paragraph. Code, tests, and `DEPLOYMENT.md` cite these numbers.

1. **MCP connector** (`mcp/`, `templates/mcp/`). *Touches:* `auth.py`, `security.py`, `config.py`, OAuth collections, reached models/services, payload shapes. *Verify:* capabilities live only in `mcp/disclosure.py` (update `templates/mcp/consent.html` for new reads); writes use `write_support.run_write` (`idempotency`/`concurrency`) returning WRITTEN row; `output_schemas.py` moves with payloads/fixtures (`required` never adds keys to shipped write outputs); never widen shipped tool matches/defaults in place; run `exporter_plugin_athena --check` (`plugin/athena/README.md`); revoke → deploy → re-consent on consent text changes (`DEPLOYMENT.md` §11).
2. **DavX5 sync** (`dav/`, serializers/parsers, `services/dossier_dav.py`). *Touches:* serializers, parsers, read fields, CTag sites, layout, writes to DAV-exposed records. *Verify:* RFC 5545 iCal (`UID`, `DTSTAMP`, `CREATED`) & RFC 6350 vCard (`UID`, `REV`); round-trip PUT stripping served metadata; `bump_ctag(dav.sync.collection_for(dossier_id))` (`"parties"` for contacts) at every write site (in-batch when batched); tombstones on removal; `relocate_resource` on moves. **Verify via `curl` (`DEPLOYMENT.md` §15–16), then on device.**
3. **Template generation / docx fill** (`utils/*docx*`, `utils/template_fields.py`, `services/gabarit*.py`). *Verify:* substitute in document/header/footer XML and copy other zip entries byte-identical (no `docxtpl`/`python-docx` round-trip or new zip parts); fill split runs; `resolve_values` matches `athena/GABARITS_PLACEHOLDERS.md`. **Open `.docx` in Word by hand.**
4. **Observability & logging** (`utils/logging_setup.py`, `utils/tracing_setup.py`). *Touches:* log lines, spans, user-data paths. *Verify:* metric/alert events use typed `log_*` helpers in `athena/OBSERVABILITY.md`; structured fields in `jsonPayload`; single handler only; zero PII logged.
5. **Security & edge defense** (`security.py`, `main.py` `before_request`, `client/security.py`, `.yaml`, Cloudflare/GCP). *Touches:* headers/CSP, CSRF, rate limits, caps, allowlists, secrets/settings, special prefixes. *Verify:* Security Rules; App Check and origin check fail OPEN if unconfigured—check `scripts.check_config` and Paramètres → Configuration.
6. **Frontend asset pipeline** (`static/vendor/`, `static/src/app.input.css`, `static/sw.js`, base templates, Early Hints in `security.py`). *Touches:* Tailwind classes in templates/routes/models, vendored assets, base scripts. *Verify:* **missing CSS classes silently fail to apply**—reuse compiled classes or run « Regenerating the stylesheet » and its 7-file fan-out; never edit vendored files in place (1-yr cache); keep footer order (`App Check boot → page scripts → htmx → Alpine`).
7. **Firestore indexes & query invariants** (`firestore.indexes.json`, model queries). *Touches:* `where` + `order_by`, filter/sort fields, cursor pagination, `COUNT`/`SUM`/`AVG`. *Verify:* composite indexes **deploy before/with code** (`_fake_firestore.py` ignores indexes); exact direction matching; `SUM`/`AVG` indexes trail aggregated fields alphabetically matching last sort direction; bounded pages (`pagination.py`) or capped groups.

**Dependency bumps are a silent trigger** for items 1–7 (`icalendar`/`vobject`, `google-auth`/`google-cloud-storage`, `google-cloud-logging`, `firebase-admin`/`pyjwt`, `pypdf`, `Pillow`, `olefile`, `Markdown`/`bleach`, OpenTelemetry). Re-run matching checks before landing.

## Architecture Rules

Code comments cite these numbers: keep them.

1. **Single user.** One authorized email (`AUTHORIZED_USER_EMAIL`): no tenancy/roles. Every non-public main route declares its guard (`@login_required` via Firebase Auth + phone MFA + stateless signed cookie expiring after `SESSION_LIFETIME_HOURS`, DAV Basic auth vs bcrypt, MCP bearer token, or App Engine cron/queue header). Only fresh, unrevoked ID tokens open sessions (never `portail` claims). Rotating `flask-secret-key` ends all sessions.
2. **Firestore is flat.** Top-level collections linked by id (`dossier_id`), never under `users/{uid}`; subcollections hold only parent-owned records (line items, steps, revisions, tombstones). Main Storage paths live under `users/{uid}/` or `staging/{uid}/` (portal quarantine keys by invitation).
3. **French for the user.** User-facing strings are French. Write new code/comments/docstrings in English; existing French modules (`client/`, Réception, French-named services/scripts/tests) keep French. Québec legal terms stay French.
4. **Money in integer cents.** `15000` is 150,00 $, never float; `Decimal` only for intermediates, rounded `ROUND_HALF_UP` to the cent before storage. Parse/format via `utils/format_fr`.
5. **UTC instants, Montréal days.** Store instants tz-aware UTC and display via `to_mtl`; store date-only values at UTC midnight (never pass to `to_mtl`). Derive « today »/lateness solely from `utils/deadlines` (`today_mtl`, `is_past_due`, `days_until`; `today_utc` only for invoice-date bounds): templates read route flags; tests freeze `today_mtl` with fixed deadlines.
6. **UUIDv4 ids.** Server-minted, never reused; caller ids discarded. Keyed ids are exceptions declared in module docstrings; concurrent creations use `create()`, never `set()`.
7. **`created_at`, `updated_at`, `etag` (+ provenance).** Carried by editable records; writes regenerate `etag` and `*_via` stamps (`models/provenance.py`, `updated_via` from writer context). `expected_etag` commits transactionally (`models/concurrency.py`); pass target state, never toggles. Bookkeeping (`counters`, `dav_sync`) and append-only records (`audit_events`, versions, revisions, credentials) omit `etag`.
8. **HTMX first.** Return a fragment if `routes/_helpers.is_htmx()`, full page otherwise. HTMX swaps 2xx only: refusals render 200 or redirect with `?erreur=`; external filter-dependent elements update via `hx-swap-oob`.
9. **Mobile-first & responsive desktop UX.** Design at **375 px** first, enhance at `md`/`lg`. Touch targets ≥44 px with comfortable spacing; zero horizontal overflow; stack forms on mobile, use calm multi-column grids on desktop; pair HTMX actions with loading indicators/skeletons and toast feedback; use `env(safe-area-inset-bottom)` on mobile bottom nav.
10. **Material-inspired visual language & theming.** Material Design clarity: `bg-canvas` (`#FAFAFA`) ground, `bg-white` elevated cards/surfaces, `gray-900` text, `indigo-600` accent, clear typography, generous whitespace. `Noto Sans` for UI; `Noto Serif` only for reading surfaces (`.note-content`, PDFs). Dark mode remaps `--color-*` tokens once in `app.input.css` under `<html data-theme>` (`utils/theme.py`, screen-only; portal/print/emails/exports stay light). Use palette utilities only—never literal colors, `dark:` only where remap cannot apply; new shades need dark values (`tests/test_theme_palette.py`). Render icons via `ms()` (`utils/icons.MATERIAL_ICONS`) with `aria-label`/`title` on icon-only controls; new icons require regenerating the vendored subset (`utils/fonts/README.md`) and browser checks.
11. **DAV-ready records.** Parties, hearings, tasks, and notes carry a vCard/iCal UID (`vcard_uid`, `vevent_uid`, `vtodo_uid`, `vjournal_uid`) minted at creation (or preserved on DAV PUT) and never altered.

## Security Rules

See [SECURITY.md](SECURITY.md) for full posture and rotation recipes.

- **Edge.** Traffic transits Cloudflare, enforced on both services by App Engine firewall (Cloudflare + `0.1.0.2/32`; `DEPLOYMENT.md` §7), `X-Origin-Auth`, and `*.appspot.com` rejection so `CF-Connecting-IP` keys rate limits. Only `/_ah/` and `X-AppEngine-QueueName`/`X-Appengine-Cron` skip checks (`security.is_appengine_internal_request`); machine handlers re-check headers.
- **CSRF.** Browser writes carry CSRF tokens. Never place browser routes on CSRF-exempt blueprints (DAV, MCP, `taches_*`) or special-cased prefixes.
- **CSP.** `<script nonce="{{ csp_nonce }}">`, zero inline `on*` (`addEventListener` on `data-*`), JSON in `<script type="application/json">`, Alpine `x-data` via `|jsattr`, vendored assets (Google origins serve App Check reCAPTCHA only). App responses are `no-store`; SW caches only static vendor/icons/offline assets.
- **Secrets.** Loaded only from Secret Manager via `config.py`/`client/config.py` (env vars locally; `DEPLOYMENT.md` §4). Compare via `hmac.compare_digest` on bytes; verify expiries in code on read.
- **Inputs & errors.** Validate filters against `VALID_*`; pass `return_to`/`next` through `security.safe_internal_redirect` on read; check link URIs (`models/hearing.is_safe_conference_uri`); show French errors by type (never `str(exc)`); keep untrusted regexes linear (CWE-1333).
- **Storage.** Derive uid via fail-closed `utils/storage_identity` (never `"unknown"`). Files never transit Flask (32 MB cap): browser → GCS resumable uploads, GCS-side copies, 15-min signed URLs (only small gabarit `.docx` uploads and generated outputs stream through Flask). Write objects before Firestore records; rules deny all clients.
- **Tool outputs & zero-PII logging.** `begin_upload`'s write-only `upload_url` is the ONLY capability a tool output may carry (create-only, staging-bound, verified by size + MD5 in `finalize_upload`, never logged/persisted). Logs/spans/exceptions carry ids, codes, field names, and counts only—never names, titles, free text, tokens, secrets, or amounts (save reconciliation variance); wrap request values in `sanitize_log_value`.

## Core specifications

- **Data model** (orientation; see model docstrings). *Cases:* `parties` (`/pieces_identite`), `dossiers`, `folders`, `documents` (`/analyses`), `notes` (`/revisions`), `tasks`, `hearings`, `protocols` (`/steps`). *Billing:* `timeentries`, `expenses`, `invoices` (`/lineitems`), `budgets`. *Accounting:* `trust_accounts`, `trust_transactions`, `trust_reconciliations`, `admin_accounts`, `admin_transactions`, `admin_reconciliations`. *Templates:* `doc_templates` (`/versions`). *System:* `settings` (`cabinet`, `integrations`), `counters`, `audit_events`, `dav_sync` (`/tombstones`), `oauth_clients`, `oauth_codes`, `oauth_tokens`, `mcp_idempotency`, `mcp_upload_tickets`. Portal DB `portail` holds `invitations`.
- **Contacts & dossiers.** Contacts are `parties` vCards (`contact_role`). Dossiers hold clients, opposing parties, roles, counsel (`client_ids`, `opposing_party_ids`, `avocat_ids` for `array_contains`), addresses (`template_fields.selected_address`), KYC/conflicts (`kyc.apply_status_transition`), `greffe`/`juridiction` (`models/reference.py`).
- **Billing, accounting & statements.** See **[ACCOUNTING.md](ACCOUNTING.md)**. TPS (5 %) + TVQ (9.975 %) never compound; time/expenses freeze on live invoices; payments are register entries only (`fee_payment.py`, `comptabilite.py`); write-offs (`radiation`) settle overdue remainders with `counters/notes-credit-{year}` (`AAAA-NC###`) credit notes; trust (append-only) and admin registers stay parallel; `models/financial_statements` governs `/finances/`, PDFs, `get_financial_statements`, `/comptabilite/radiations/`.
- **Délais, taxonomie & procédure.** See **[LITIGATION.md](LITIGATION.md)**. `deadlines.py` separates `PROCEDURAL` (`art. 83 C.p.c.`) and `CIVIL` (`art. 2879 C.c.Q.`); `recurrence.py` anchors via `tz.mtl_to_utc`; `taxonomie.py`/`phases.py` stay orthogonal; alerts read `dossier.derive_prescription`.
- **DAV.** Direct `/dav/` children: `addressbook/` (non-suppliers via `carnet.py`), `general/`, `dossier-{id}/` (`ACTIVE_DOSSIER_STATUSES`, via `dossier_dav.run_status_transition`). `dav_sync/{name}` holds CTags/tombstones; hrefs are permanent.
- **MCP connector.** Stateless HTTP JSON-RPC (`POST /mcp`) behind PKCE OAuth (`sha256` tokens, rotating refresh, Claude callbacks, French consent behind login + MFA). Scopes `athena:read`/`athena:write` freeze at grant; revoke via `scripts.revoke_mcp_tokens`.
- **Client portal.** `athena/client/` imports no main modules, reads only `portail` DB, uses Firebase email links, quarantines uploads in a create-only bucket, signals via Cloud Tasks. Nothing files before « Verser » in Réception.
- **Bookings & Outlook.** Cron imports Bookings as `confirmation="à_confirmer"` (hidden from DAV/dashboard/default MCP) decided via `rendez_vous.py` (refusal cancels Outlook meeting). Web emails use Graph (`courriel.py`); another cron mirrors confirmed hearings one-way to Outlook.
- **Documents & gabarits.** Folders (`folder.DEFAULT_TREE`) resolve by `system_role` (adopting matching unstamped folders). Gabarits (`athena/GABARITS_PLACEHOLDERS.md`) version append-only and apply for special kinds when « actif »; `kind="modele"` never fills. `Bibliothèque` groups Gabarits, Modèles, Documents. Notes render via `markdown_to_safe_html`.
- **Protocols & théorie de la cause.** One « actif » protocol (`cq_simplifié`, `cs_ordinaire`, `conventionnel`) and one undated `is_analyse` note per dossier; tasks/steps sync two-way.

## Regenerating the stylesheet

`@source` lines in `athena/static/src/app.input.css` are the complete scan set (classes outside/runtime-built never compile). From repo root:

```bash
TW=$(sed -n 's/.*@tailwindcss\/cli@\([0-9.]*\).*/\1/p' athena/static/src/app.input.css)
npm install --no-save --no-package-lock "@tailwindcss/cli@${TW:?}" "tailwindcss@${TW:?}"
npx "@tailwindcss/cli@${TW:?}" -i athena/static/src/app.input.css -o athena/static/vendor/app.css --minify
H=$(sha256sum athena/static/vendor/app.css | cut -c1-8)
mv athena/static/vendor/app.css "athena/static/vendor/app.$H.css"
```

Delete old `app.*.css` (one only) and update every reference (`DEPLOYMENT.md` §15 « Frontend assets » is test-pinned; `git grep -l` confirms). Bump `STATIC_CACHE` in `athena/static/sw.js` and delete `node_modules`. Other vendored assets take a new hash/version filename on change, updating all references.

## Adding a feature

- Document new collections in model docstrings (plus DAV UID if DavX5-synced) and add to the data-model map.
- Extend existing models: writers return `(doc, errors)` in French; pair `get_X_strict` with fail-open `get_X`.
- Connector-writable edit forms carry `{{ etag_field(record) }}`, `{{ conflict_banner(conflict) }}`, and route via `routes/edit_conflict.py`.
- Test via real routes, models, and templates on `_fake_firestore.py`; every guard proves it catches something, and every fix adds a failing-before test.
- Edit this file only for principle/spec changes—never history, counts, or inventories—and keep `GEMINI.md` byte-identical (`tests/test_deployment_inventory.py`).
