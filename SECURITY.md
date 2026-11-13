# Security Policy

Pallas Athena is a single-user legal practice management application handling confidential client data subject to professional secrecy obligations (*secret professionnel*) under the Code of ethics of advocates of the *Barreau du Québec*. Security is designed fail-closed across every layer.

## Reporting a vulnerability

If you discover a security issue, **do not open a public GitHub issue**. Email the maintainer directly:

**Contact:** Jason Poirier Lavoie — `jason@poirierlavoie.ca`

Please include:
- A description of the vulnerability
- Steps to reproduce (or a proof-of-concept)
- The potential impact you have identified
- Any suggested mitigation, if you have one

You will receive an initial acknowledgement within 72 hours. Coordinated disclosure is appreciated — please allow a reasonable window to patch before publishing details.

## Scope

In scope:
- The main practice management service at `athena.poirierlavoie.ca` (`athena/app.yaml`, `main:app`), including:
  - The web application and internal task/cron handlers
  - CardDAV / CalDAV endpoints (`/dav/*`, `/.well-known/carddav`, `/.well-known/caldav`)
  - The MCP connector and OAuth 2.1 PKCE endpoints (`/mcp`, `/oauth/*`, `/.well-known/oauth-*`)
- The isolated client portal (« portail ») service at `portail.poirierlavoie.ca` (`athena/portail.yaml`, `client.wsgi:app`)
- Code and configuration in this repository

Out of scope:
- Social engineering of the maintainer or third parties
- Physical attacks
- Volumetric / DDoS attacks
- Issues in third-party dependencies that do not affect this application's configuration (report those upstream)
- Findings that require already-compromised credentials or compromised endpoint devices

## How secrets are managed

Pallas Athena runs as two isolated Google App Engine Standard services (`default` and `portail`) in a single production project (`athena-pallas`). Secrets live in two places:

**Production (Google Cloud Secret Manager):**
- All **six** sensitive values (`flask-secret-key`, `portail-secret-key`, `firebase-api-key`, `dav-password-hash`, `cf-origin-secret`, `graph-client-secret` — inventoried in [`utils/deployment_inventory.py`](athena/utils/deployment_inventory.py)) are stored in [Google Cloud Secret Manager](https://cloud.google.com/secret-manager) and prefetched in parallel at worker startup (`config.py` for `default`, `client/config.py` for `portail`).
- **Service-account separation is a hard security boundary:**
  - The `default` service runs as `athena-pallas@appspot.gserviceaccount.com` and holds `roles/secretmanager.secretAccessor` only on the **five** secrets resolved by `config.py` (`flask-secret-key`, `firebase-api-key`, `dav-password-hash`, `cf-origin-secret`, `graph-client-secret`), plus `roles/secretmanager.secretVersionAdder` only on the four writable secrets (`firebase-api-key`, `dav-password-hash`, `cf-origin-secret`, `graph-client-secret` — never on `flask-secret-key` or `portail-secret-key`). It has no access to `portail-secret-key`.
  - The `portail` service runs as the least-privilege service account `portail-svc@athena-pallas.iam.gserviceaccount.com` and holds `roles/secretmanager.secretAccessor` only on the **three** secrets resolved by `client/config.py` (`portail-secret-key`, `firebase-api-key`, `cf-origin-secret`). It has no access to `flask-secret-key`, `dav-password-hash`, or `graph-client-secret`.
- Non-sensitive identifiers (project ID, storage bucket, App ID, reCAPTCHA Enterprise site key, Graph tenant/client IDs) live as plain environment variables in `athena/app.yaml` and `athena/portail.yaml`.
- The Firebase Admin SDK and Google Cloud client libraries use Application Default Credentials (ADC) — no service-account JSON key file is ever deployed.

**Local development:**
- A gitignored `.env` file at the repository root supplies environment variables (`SECRET_KEY`, `PORTAIL_SECRET_KEY`, `FIREBASE_API_KEY`, `DAV_PASSWORD_HASH`, `CF_ORIGIN_SECRET`, `GRAPH_CLIENT_SECRET`).
- Local Google Cloud credentials come from `gcloud auth application-default login` or a service-account JSON file stored **outside** the repository and referenced via `GOOGLE_APPLICATION_CREDENTIALS`.

**Never committed (enforced by `.gitignore`):**
- `.env` and any `.env.*` files (except `.env.example`)
- Service-account JSON files (`**/service-account*.json`, `**/*-adminsdk-*.json`) and `*.token` files
- Private keys, bcrypt hashes, API keys, session secrets, or App Check debug tokens
- Client workbooks or accounting directories (`/Comptes recevables/`, `/Comptes payables/`, `*.xlsx`)

If you spot any secret or privileged data in this repository (current files or git history), please report it immediately.

## Defense-in-depth controls

- **Edge & origin authentication (`security.py`, `client/security.py`):** Traffic transits Cloudflare with Full (Strict) TLS. The App Engine firewall admits only Cloudflare's published IP ranges plus `0.1.0.2/32` (App Engine internal cron and Cloud Tasks dispatches; [DEPLOYMENT.md](DEPLOYMENT.md) §7). On both `default` and `portail`, `before_request` hooks run **before** `Flask-Limiter` and `CSRFProtect` to:
  1. Reject any `*.appspot.com` `Host` header with `403` (emitting `appspot_blocked`).
  2. Require the Cloudflare Transform Rule header `X-Origin-Auth` to match `CF_ORIGIN_SECRET` via byte-level `hmac.compare_digest` (fail-closed on startup in production; if ever unconfigured, emits a structured `origin_secret_disabled` warning once per process). Only `/_ah/*` and spoof-proof `X-AppEngine-QueueName` / `X-Appengine-Cron` dispatches bypass the origin header check, and machine handlers re-verify their own headers.
- **Authentication & sessions (`auth.py`):** Firebase Auth (email/password) with mandatory Phone MFA (`REQUIRE_MFA=true`). Server-side verification enforces the single `AUTHORIZED_USER_EMAIL`, checks revocation (`check_revoked=True`), requires a fresh ID token (≤ 5 minutes old), and rejects any token carrying the `portail` custom claim. Sessions use a stateless signed cookie (`HttpOnly`, `Secure`, `SameSite=Lax`, expiring after `SESSION_LIFETIME_HOURS`, default 12 hours).
- **Route guards & CSRF:** Every non-public route declares its guard (`@login_required` for browser routes, HTTP Basic Auth for `/dav/*`, OAuth 2.1 PKCE bearer token for `/mcp`, or App Engine cron/queue header verification). `flask-wtf` `CSRFProtect` covers all browser mutations; CSRF-exempt blueprints (`dav`, `mcp`, internal `taches_*` handlers) never host browser-session routes (`tests/test_csrf_exemptions.py`).
- **Client portal isolation (`athena/client/`):** The portal runs as a separate App Engine service (`portail.yaml`) under `portail-svc@athena-pallas.iam.gserviceaccount.com` with its own session signing key (`portail-secret-key`), its own named Firestore database (`portail`, holding only `invitations`), and its own quarantine bucket (`athena-pallas-portail-quarantaine`, `roles/storage.objectCreator` only). Portal sessions authenticate via single-use Firebase email links bound to a `portail` custom claim. Uploaded files use an inert extension allowlist (`client/config.py` `PORTAIL_EXTENSIONS` — no `html`/`svg`/`js`) and stream directly from the browser to GCS resumable sessions; nothing enters the main database or bucket until the lawyer explicitly approves (« Verser ») in Réception.
- **Firebase App Check:** Enforced via reCAPTCHA Enterprise (`X-Firebase-AppCheck`) on HTMX requests in the main app and on mutating `POST` requests in the portal (`creer_session` is exempted after the single-use email link is spent). If unconfigured in production, both services emit `appcheck_disabled` once per process.
- **DAV authentication (`dav/dav_auth.py`):** Separate HTTP Basic Auth (`DAV_PASSWORD_HASH`, 60-character bcrypt hash) plus a per-IP brute-force brake, behind the same App Engine firewall and `X-Origin-Auth` check. Cloudflare Access is deliberately not used (DAVx⁵ cannot send service-token headers, and mTLS on a shared hostname would affect the web UI and MCP connector).
- **Hardened HTTP headers & CSP:**
  - 2-year HSTS with preload (`max-age=63072000; includeSubDomains; preload`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()`, and `Cache-Control: no-store, no-cache, must-revalidate, private` on dynamic responses (`Referrer-Policy: strict-origin-when-cross-origin` on `default`; `no-referrer` on `portail` so invitation links never leak).
  - Static handlers in both `app.yaml` and `portail.yaml` repeat the `secure: always` + `nosniff` + HSTS baseline (pinned across all handlers by `tests/test_security_headers.py`).
  - **Enforced Content-Security-Policy** with a per-request cryptographic nonce on `script-src` (no `'unsafe-inline'`; all frontend libraries and fonts are vendored same-origin under `static/vendor/`). `form-action` is locked to `'self'` everywhere except `/oauth/authorize` (which permits redirects only to the allowlisted Claude OAuth callback origins in `mcp.ALLOWED_REDIRECT_URIS`). Violations report to `/csp-report` (`csp_violation`). The portal enforces a stricter CSP without `'unsafe-eval'`.
- **Rate limiting & request size caps:**
  - `Flask-Limiter` (keyed on `CF-Connecting-IP` behind the verified Cloudflare edge) protects `/auth/login`, `/auth/verify-token`, `/csp-report`, `/dav/*`, `/oauth/*`, and portal endpoints (`/api/session`, `/api/renvoi`, upload endpoints).
  - Request body size guards (`_enforce_request_size`) cap payloads at **1 MB** by default, **5 MB** on `/dav/*` and `/.well-known/*`, and **10 MB** on template `.docx` uploads (`/gabarits/`), refusing oversized bodies with `413` and a zero-PII `request_too_large` security log event.
- **Input validation, redirect & SSRF guards:**
  - Top-level and nested free-text strings are sanitized (`security.sanitize` with linear-time `_TAG_RE = re.compile(r"<[^<>]*>")`, CWE-1333) and length-capped per field in `models/`. MCP write tools refuse inputs that `sanitize` would alter so legal citations (e.g., `<2024 QCCS 1234>`) never silently vanish.
  - Every request-supplied `return_to` or `next` target passes through `security.safe_internal_redirect()` on read (rejecting external schemes/hosts, `//`, backslashes, and control characters, logging `redirect_rejected`; pinned by `tests/test_redirect_guard.py` and CodeQL barrier).
  - Hearing conference links are validated by `models/hearing.is_safe_conference_uri()` (HTTPS-only, no embedded credentials, no loopback/private/link-local hosts).
- **Storage & Firestore security rules:**
  - Both [`firestore.rules`](firestore.rules) and [`storage.rules`](storage.rules) are **deny-all (`allow read, write: if false;`)**. Every legitimate read or write goes through the server-side Admin SDK or 15-minute V4 signed URLs (`iam.serviceAccountTokenCreator` self-impersonation), so the public `FIREBASE_API_KEY` cannot be used by self-signed-up Firebase accounts to access data or host files.
  - Main-app document and statement uploads stream directly from the browser to GCS resumable upload sessions under `staging/{uid}/` and are verified and copied server-side to `users/{uid}/` (deriving `uid` fail-closed via `utils/storage_identity.py`, never `"unknown"`).
- **MCP connector — scoped OAuth & no file-read URLs (`mcp/`):**
  - Protected by OAuth 2.1 + PKCE (`S256`), SHA-256-hashed bearer tokens, rotating refresh tokens, and explicit French consent behind `@login_required` + MFA.
  - No tool output ever carries a signed download URL or storage path. The **one** documented capability URL is `begin_upload`'s write-only GCS resumable session for a single staging object (`staging/{uid}/mcp/{ticket_id}/upload`): it cannot read or list anything, is never persisted in Firestore, idempotency records, or logs, and `finalize_upload` accepts the staged bytes only within the ticket's 1-hour TTL if their size and MD5 match the values declared at opening ([DEPLOYMENT.md](DEPLOYMENT.md) §15 « Lot 2A »).
- **Zero-PII observability & security alerting (`utils/logging_setup.py`, `monitoring/`):**
  - Logs, spans (`utils/tracing_setup.py`), and exception messages record only opaque IDs, closed reason codes, field names, and counts — never client names, titles, free text, tokens, secrets, or dollar amounts (backed by `RedactionFilter` and `sanitize_log_value`).
  - Security events on logger `pallas.security` feed test-pinned Cloud Logging metrics and Cloud Monitoring alert policies (`monitoring/`, [DEPLOYMENT.md](DEPLOYMENT.md) §6.10, [athena/OBSERVABILITY.md](athena/OBSERVABILITY.md)).

## Credential rotation

**Never pass a secret or password as a command-line argument (`--data=`, `b'...'`) or pipe through `echo` / `print(...)` / PowerShell.** Command-line arguments leak into `~/.bash_history` and `ps`, while `echo`/`print` append a trailing newline (`\n`) and Windows PowerShell pipes inject a UTF-8 BOM + CRLF — corrupting byte-compared secrets (`cf-origin-secret` → site-wide `403`, `dav-password-hash` → silent 61-byte DavX5 failure).

Use the **in-app « Paramètres → Configuration » page** (which can write new versions of the 4 writable secrets and validates byte length, ASCII charset, bcrypt format, and stray whitespace) or run the **test-pinned `bash` recipes in [DEPLOYMENT.md](DEPLOYMENT.md) §6.4** (generated from `utils/deployment_inventory.gcloud_recipe()`, which probes `$PY`, reads secrets via `IFS= read -rs` or `getpass`, guards byte length before `gcloud secrets versions add --data-file=-`, and verifies the newly written `$VERSION`).

If you have reason to believe a credential has been exposed:

1. **`flask-secret-key` (`SECRET_KEY` — `default` service):** Mint and add a new version via the [DEPLOYMENT.md](DEPLOYMENT.md) §6.4 recipe (32–256 ASCII token chars; not writable from the web UI by design). Redeploy `default`. Invalidates all active lawyer sessions.
2. **`portail-secret-key` (`PORTAIL_SECRET_KEY` — `portail` service):** Mint and add a new version via the [DEPLOYMENT.md](DEPLOYMENT.md) §6.4 recipe (32–256 ASCII token chars; not writable from the web UI). Redeploy `portail`. Invalidates active 24-hour client portal sessions.
3. **`firebase-api-key` (`FIREBASE_API_KEY` — both services):** Regenerate in [GCP Console → Credentials](https://console.cloud.google.com/apis/credentials), add the new version via Paramètres → Configuration or [DEPLOYMENT.md](DEPLOYMENT.md) §6.4 (30–60 chars), and redeploy both `default` and `portail`.
4. **`dav-password-hash` (`DAV_PASSWORD_HASH` — `default` service):** Compute a 60-character bcrypt hash interactively (`getpass.getpass`) and add the new version via Paramètres → Configuration or [DEPLOYMENT.md](DEPLOYMENT.md) §6.4. Redeploy `default` and update the password in the DAVx⁵ client.
5. **`cf-origin-secret` (`CF_ORIGIN_SECRET` — both services + Cloudflare edge):** Because each worker caches the secret at startup, rotate in **three chained steps without pause** ([DEPLOYMENT.md](DEPLOYMENT.md) §6.4 & §7):
   1. Add the new secret version in Secret Manager (32–128 ASCII token chars).
   2. Set the exact same value in the Cloudflare zone-wide Transform Rule (`X-Origin-Auth`).
   3. Redeploy **both** `default` and `portail`.
6. **`graph-client-secret` (`GRAPH_CLIENT_SECRET` — `default` service):** Create a new client secret in Microsoft Entra ID → App registrations → Certificates & secrets, add the new version via Paramètres → Configuration or [DEPLOYMENT.md](DEPLOYMENT.md) §6.4 (20–256 chars), redeploy `default`, and delete the old secret in Entra.
7. **MCP OAuth tokens (claude.ai connector):** Revoke active access and refresh tokens immediately without redeploying:
   ```bash
   cd athena
   python -m scripts.revoke_mcp_tokens --all --yes
   ```
8. **Firebase Admin SDK local key (if used locally):** Generate a new key in [Firebase Console → Service Accounts](https://console.firebase.google.com/project/_/settings/serviceaccounts/adminsdk) and delete the old one (App Engine uses ADC and has no deployed key file).
9. **App Check debug token (local dev only):** Revoke in Firebase Console → App Check → Manage debug tokens.

After rotating any Secret Manager secret, redeploy the affected service(s) from `athena/` so running workers load the new version:
```bash
cd athena
gcloud app deploy app.yaml portail.yaml --project=athena-pallas
```

## Supported versions

This is a single-deployment application — only the currently deployed `main` branch is supported. Older App Engine versions are retained solely for emergency rollback and receive no fixes; `cloudbuild.yaml` automatically prunes all but the three most recent non-serving versions on `default`.

## Data handling

Client data — confidential material subject to professional secrecy — is stored exclusively in the **`northamerica-northeast1` (Montréal)** region. Google-managed telemetry sinks (Cloud Logging, Cloud Trace) default to a **`global`** location, but hold no client records: application logging and tracing are PII-redacted at source.

| Data | Service | Region | Notes |
|---|---|---|---|
| Structured practice records (`parties`, `dossiers`, `timeentries`, `invoices`, `notes`, `protocols`, registers, …) | Firestore (`(default)` database, native mode) | `northamerica-northeast1` (Montréal) | Region fixed at database creation — immutable; `firestore.rules` deny all clients |
| Client portal invitations (`invitations`) | Firestore (`portail` named database, native mode) | `northamerica-northeast1` (Montréal) | Isolated from `(default)`; `portail-svc` has read-only access |
| Document, template & statement files (`users/{uid}/`, `staging/{uid}/`) | Firebase Storage (`athena-pallas.firebasestorage.app`) | `northamerica-northeast1` (Montréal) | Bucket location fixed at creation — immutable; `storage.rules` deny all clients; `staging/` auto-deletes after 7 days |
| Client portal quarantine uploads (`submissions/`, `archive/`) | Cloud Storage (`athena-pallas-portail-quarantaine`) | `northamerica-northeast1` (Montréal) | `portail-svc` is `objectCreator` only; lifecycle deletes `submissions/` after 30 days and `archive/` after 90 days |
| Application compute (`default` and `portail` services) | App Engine Standard | `northamerica-northeast1` (Montréal) | App region is permanent |
| Application & security logs (`pallas-athena`, `pallas.security`) | Cloud Logging — `_Default` bucket | `global` by default (redirectable to Montréal) | PII-redacted at source by `RedactionFilter`; see below |
| Audit logs (Admin Activity / System Event / Data Access) | Cloud Logging — `_Required` bucket | `global` — **permanent** | Google-imposed; project-admin metadata, not client data |
| Distributed traces | Cloud Trace | Google-managed (global) | IDs/counts only, PII-sanitized (`tracing_setup.py`); no regional-storage control |
| Application secrets (6 secrets) | Secret Manager | per each secret's replication policy | Keys/hashes only — no client data |

**Logging residency.** Cloud Logging keeps logs in *log buckets* whose location is immutable once created; the two auto-created buckets (`_Default`, `_Required`) start in `global`. Application logs can be routed to a Montréal bucket by creating a regional log bucket and repointing the `_Default` sink at it (procedure in [DEPLOYMENT.md](DEPLOYMENT.md) → §6.8). The `_Required` audit-log bucket cannot be relocated and stays `global` by Google's design. Because application logs are PII-redacted before they leave the process ([utils/logging_setup.py](athena/utils/logging_setup.py) `RedactionFilter`), the residency stakes on logging are low relative to Firestore and Storage.

- All access is logged via Cloud Logging; PII is **not** logged at the application level.
- **Firestore:** native scheduled backups, daily (7-day retention) and weekly (14-week retention), stored in `northamerica-northeast1`.
- **Firebase Storage:** object versioning enabled, with a 180-day non-current version retention policy. GCS bucket-level soft delete provides an additional 7-day window to undelete objects. A client's proof of identity (`users/{uid}/parties/{id}/pieces_identite/`) is the exception: deleting it erases its version, so no non-current copy is kept — only the 7-day soft delete — while its Firestore record stays in the backups above until they expire. So is a reconciliation's bank statement (`users/{uid}/{fideicommis|administration}/conciliations/{id}/`, since 2026-11-10) while its reconciliation is a draft: a statement replaced, removed (« Retirer ») or abandoned with its draft is erased by generation, after its record stopped pointing at it. A statement joined to a COMPLETED reconciliation is never erased by the application (art. 32 keeps it 7 years), and a removed or replaced draft statement is retired for good — never attached again; like every object it reaches only through 15-minute signed URLs, and the Storage rules deny every client.
- Data subject rights (access, deletion, rectification) are handled manually by the maintainer.
