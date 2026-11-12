# Deploying your own Pallas Athena instance

This guide takes you from an empty Google Cloud project to a running,
Cloudflare-fronted deployment of Pallas Athena. It is written for someone who
has **not** worked with App Engine, Firebase, or Cloudflare before, so it errs
on the side of explaining *why* a step exists.

> **New to the project?** Read [README.md](README.md) first for what the app is,
> then come back here.

---

## 0. Before you begin — permission & eligibility

**Pallas Athena is not open-source.** The [LICENSE](LICENSE) is
all-rights-reserved: the source is published for reference, and *no right to
use, copy, modify, or deploy it is granted automatically.*

You may run your own instance **only after** obtaining **prior written
permission** from the copyright holder, Jason Poirier Lavoie. Because the
application is purpose-built for legal practice and handles data subject to
professional-secrecy obligations, permission is granted **only to practising
lawyers** (e.g. members of the Barreau du Québec or another bar).

**To request permission**, email `jason@poirierlavoie.ca` with:
- your name and bar / order membership (with member number),
- the jurisdiction you practise in,
- a brief description of how you intend to use the app.

Do not deploy until you have that written consent. The rest of this guide
assumes you have it.

---

## 1. What you are deploying (and what you're signing up for)

Pallas Athena is a **deliberately single-tenant** practice manager. Adopting it
is closer to *re-provisioning and adapting* than *clone-and-run*. Three
assumptions are baked deep into the code — accept them before you start:

| Assumption | What it means for you |
|---|---|
| **One authorized user** | Exactly one email is allowed, enforced server-side ([auth.py](athena/auth.py)). There is no registration, no roles, no multi-tenancy. Supporting more than one user is a rewrite, not a setting. |
| **French-only UI** | Every label, button, and message is hardcoded French. There is no i18n layer. |
| **Québec legal domain** | Taxes (GST 5 % / QST 9.975 %), judicial deadlines (art. 83 C.p.c. + Québec holidays), court-file-number parsing, courthouse/tribunal reference data, and the CQ/CS protocol templates are Québec-specific. **Deploying as-is outside Québec produces wrong deadlines and wrong taxes.** See §13 to adapt. |

Architecture at a glance:

```
Browser / DavX5 / Claude                     Invited client (public)
        │                                              │
   Cloudflare  (TLS, WAF, Early Hints, origin secret)  │
        │                                              │
  App Engine « default »                    App Engine « portail »
  (Flask + gunicorn, F2)                    (F1, own least-privilege SA)
        │                                              │
        │                           ┌──────────────────┤
        │                           │                  │
        │                    Cloud Tasks queue   Quarantine bucket
        │                     « portail »        (client uploads)
        │                           │                  │
        └──────────┬────────────────┴──────────────────┘
                   │
  ┌────────────────┼──────────────┬──────────────┬────────────────┐
Firestore     Firestore      Firebase Storage  Firebase Auth  Secret Manager
(default)     « portail »    (documents/       (+ Phone MFA)  (6 secrets)
              (named DB)      gabarits)
```

**There are TWO App Engine services, not one**, plus a `dispatch.yaml` that
routes the portal host to the second and a `cron.yaml` running three jobs
during office hours only (`athena/utils/cron_schedule.py`).
This diagram showed one until 2026-09-12, which is how an adopter learns the
`portail` service exists only when their first CI build fails deploying it.
Its infrastructure — the least-privilege service account, the quarantine
bucket, the named Firestore database, the Cloud Tasks queue and their IAM
grants — is §6.9.

---

## 2. Prerequisites

**Accounts**
- A **Google Cloud** account with billing enabled.
- A **Cloudflare** account on the **Pro plan** (≈ US$25/mo) — required for
  Early Hints. (Zero Trust / Access is optional — this deployment does not use it.)
- A **domain name** you control (this guide uses `yourdomain.example`).
- *Optional:* a **Google Play Console** account (only for the Android TWA, §12).
- *Optional:* a **claude.ai** account (only for the MCP connector, §11).

**Local tools**
- Python **3.13**
- [`gcloud` CLI](https://cloud.google.com/sdk/docs/install)
- [`firebase` CLI](https://firebase.google.com/docs/cli) (`npm i -g firebase-tools`)
- [`uv`](https://github.com/astral-sh/uv) (only if you change dependencies)
- Node.js + npm (only if you recompile the CSS — see §15)
- `git`. **Not** a separate `pip install bcrypt`: `bcrypt` is a pinned direct
  dependency, so `pip install -r athena/requirements.txt` (§14) already brings
  it — and a `.venv` created with `--without-pip` has no `pip` to run the
  command with anyway (« No module named pip », measured on this repository's
  own venv). §6.4's recipe therefore *checks* for bcrypt instead of installing
  it blind.

---

## 3. Cost expectations (rough, low-traffic single user)

| Component | Ballpark |
|---|---|
| App Engine F2, `min_instances: 0` (as deployed) | Billed per **instance-hour** while an instance is up — until about 15 minutes after its last request —, never per request. The free tier is 28 F1-hours a day for both services together; an F2 hour counts as two. Until 2026-11-12 the crons, every 10–15 minutes, kept one instance up around the clock (about 24 F2-hours a day, measured): about US$30–42/mo after the free tier (estimate). They now run only from 9 h to 17 h (`cron.yaml`): about 14 F2-hours a day (estimated from a week of request logs), at the edge of the free tier, warm during office hours and with cold starts (about 8 s) only outside them. The exact figure: Billing → Reports |
| App Engine F2, `min_instances: 1` | one always-on instance, no cold starts: about US$30–42/mo after the free tier (estimate) |
| App Engine F1 — the **`portail` service** | a few $/mo (also `min_instances: 0`; it only moves JSON, never file bytes) |
| Firestore (native) — **two databases** | cents–low $/mo at single-user volume |
| Cloud Tasks + Cloud Scheduler | free tier covers one queue and three cron jobs |
| Quarantine bucket (client uploads) | pennies/mo + egress; lifecycle purges at 90 / 365 days |
| Firebase Storage | pennies/mo + egress |
| Secret Manager | negligible |
| Artifact Registry — the deploy images (`gae-standard`) | US$0.10 per GB-month beyond 0.5 GB free. App Engine stores one image per service per deploy and never deletes them: 21.3 GB on 2026-11-12 (about US$2/mo, growing about 2.7 GB a month), until the cleanup policy of §15 (« Office-hours crons… ») keeps only the ten newest of each package |
| reCAPTCHA Enterprise | free tier is generous for one user |
| **Cloudflare Pro** | **~US$25/mo (required)** |
| Domain | ~US$10–20/yr |

Cloudflare Pro and (optionally) an always-on App Engine instance dominate the
bill. Everything Google-side is small at single-user scale.

---

## 4. Configuration reference

### 4.1 Environment variables

Set these in [`athena/app.yaml`](athena/app.yaml) (main service) and
[`athena/portail.yaml`](athena/portail.yaml) (portal service) for production,
and in a local `.env` (from [`.env.example`](.env.example)) for development.
**Never** put a secret in either yaml.

**Complete by derivation.** A test parses `config.py`, `client/config.py` and
`client/app.py` for every environment variable they actually read and fails if
one is absent from the tables below. **Twenty-five were missing until
2026-09-12** — the whole Microsoft Graph, Bookings and Outlook-mirror surface,
the portal's own four, and `MCP_WRITE_ENABLED`. Five of those govern
integrations an adopter cannot otherwise discover without reading `config.py`.
The MCP connector has had **no switch** since 2026-10-05: `MCP_ENABLED`,
`MCP_WRITE_ENABLED` and `MCP_COMPTABILITE_ENABLED` were removed (the lawyer's
decision), and a value of theirs left in a yaml or a `.env` is read by
nothing — to stop the connector, revoke its tokens (§11).

#### Core — boot, authentication, edge

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `ENV` | ✔ | `development` | `production` switches secret resolution to Secret Manager. Read by **both** services |
| `SECRET_KEY` → secret `flask-secret-key` | ✔ **hard** | — | Flask session signing; **app won't boot without it** |
| `FIREBASE_PROJECT_ID` | ✔ **hard** | — | Your GCP/Firebase project id. It also *builds the Secret Manager resource path*, so **nothing** — not one secret — resolves without it |
| `FIREBASE_STORAGE_BUCKET` | ✔ **hard** | — | Your Storage bucket name |
| `AUTHORIZED_USER_EMAIL` | ✔ **hard** | — | The single user; also the DAV username |
| `FIREBASE_APP_ID` | ○ | `""` | Web-app id used by the App Check bootstrap |
| `FIREBASE_API_KEY` → secret `firebase-api-key` | ○ | `""` | Public browser key (safe to expose, kept out of git) |
| `DAV_PASSWORD_HASH` → secret `dav-password-hash` | ○ (needed for DAV) | `""` | **bcrypt** hash of the DAV password |
| `RECAPTCHA_ENTERPRISE_SITE_KEY` | ○ (recommended) | `""` | App Check; **fail-open + loud warning** if unset in prod |
| `APPCHECK_DEBUG_TOKEN` | ○ | `""` | Local dev only |
| `CF_ORIGIN_SECRET` → secret `cf-origin-secret` | ○ | `""` | Edge origin check; **fail-open** if unset or unreadable — it then logs `origin_secret_disabled` once per process, which fires the alert « Sécurité — une protection est désactivée » (§6.10); §4.4 and « Paramètres → Configuration » report it too |
| `REQUIRE_MFA` | ○ | `true` | Enforce Phone MFA. Read by « Paramètres → Sécurité » to decide whether the last enrolled factor may be removed |
| `SESSION_LIFETIME_HOURS` | ○ | `12` | Server-side session lifetime |
| `RATE_LIMIT_LOGIN` | ○ | `5 per minute` | Login rate limit |
| `MCP_CANONICAL_ORIGIN` | ○ | owner domain in [config.py](athena/config.py) | OAuth issuer — **must be your domain** |
| `TRACE_SAMPLE_RATIO` | ○ | `0.1` | Trace sampling (read by `utils/tracing_setup.py`, not by `config.py`) |
| `OTEL_EXPERIMENTAL_RESOURCE_DETECTORS` | ✔ (prod) | — | `gcp`. Read by the **OpenTelemetry SDK itself**: since SDK 1.42 the resource detectors load *only* when it is set. Needed in **both** yamls — each service runs its own `tracing_setup` in its own process |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | ○ (prod) | SDK default | OTLP export timeout **in seconds**. Both yamls set `5` |
| `PIP_REQUIRE_HASHES`, `PIP_NO_DEPS` | ✔ (prod) | `1`, `1` | Supply-chain: reject unhashed/out-of-band installs |

#### Firm profile — SEED and FALLBACK only

The live firm profile is edited in « Paramètres » and stored at
`settings/cabinet`. These eleven bootstrap a fresh deploy on an empty database
and are what the app falls back to if Firestore is unreadable — they are **not**
the running values once the profile has been saved once. The `portail` service
is the exception: it reads `FIRM_NAME`/`FIRM_PHONE` straight from its own yaml
and cannot reach the singleton.

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `FIRM_NAME` | ○ | `""` | **The lawyer**, not the firm — the compliance signer and the letterhead |
| `FIRM_STREET`, `FIRM_UNIT`, `FIRM_CITY`, `FIRM_POSTAL_CODE` | ○ | `""` | Address, in the app's own six-suffix shape |
| `FIRM_PROVINCE` | ○ | `QC` | ⚠ The only non-empty default here, and it disagrees with the convention every contact address in the app follows (`Québec`, spelled out). `_seed_from_env` migrates it through `apply_address_defaults`, so the stored profile is correct — but a reader of `app.yaml` sees `QC` |
| `FIRM_PHONE`, `FIRM_FAX`, `FIRM_EMAIL` | ○ | `""` | Phones are stored **E.164**; `cabinet_dict()` renders the local form |
| `GST_NUMBER`, `QST_NUMBER` | ○ | `""` | ⚠ Read at invoice creation and **snapshotted per invoice** — nothing rewrites them on an existing invoice (a brouillon's correction, lot 3a, reaches its notes, terms, due date and billing address only), so setting them here (or in « Paramètres ») affects **future invoices only**. **Since lot 3a a NEW invoice charging GST or QST under an EMPTY number is REFUSED**, on the web form as through the connector — fill both in « Paramètres → Profil du cabinet » before deploying it (§15 « Lot 3 », step 1) |

#### Integrations — Microsoft Graph, Bookings, Outlook mirror

Main service only. The `portail` service has **no** `GRAPH_*` variable by
design (spec L1 §8.1 — it cannot call Graph at all).

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `GRAPH_TENANT_ID` | ○ | `""` | Entra tenant. Outbound email is off unless all four Graph values are set (`Config.graph_configured()`) |
| `GRAPH_CLIENT_ID` | ○ | `""` | The app registration |
| `GRAPH_SENDER_UPN` | ○ | `""` | The mailbox invitations and accusés are sent **from** |
| `GRAPH_SENDER_NAME` | ○ | `""` | Display name on those messages |
| `GRAPH_CLIENT_SECRET` → secret `graph-client-secret` | ○ | `""` | Resolved with `required=False`, which **swallows a PERMISSION_DENIED into `""`** — a missing accessor binding therefore turns outbound email off in silence. §6.4's grant loop covers it |
| `BOOKINGS_JURISTE_UPN` | ○ | `""` | The mailbox whose calendar is polled. **Empty short-circuits the sync** |
| `BOOKINGS_SYNC_ACTIVE` | ○ | `true` | Kill switch for the 10-minute Bookings import |
| `BOOKINGS_SUBJECT_KEYWORDS` | ○ | `Consultation` | Comma-separated. A subject matches when it **ends** with « {separator} {keyword} », case- and accent-folded — Bookings names the event `{Customer} - {Service}`, so the service name is a suffix. ⚠ An **empty** value is a total, silent outage: nothing matches, and the absence loop then flags every already-imported reservation `annulée_client`. The sync refuses to run rather than proceed |
| `BOOKINGS_SYNC_LOOKAHEAD_DAYS` | ○ | `90` | Forward window of the poll |
| `BOOKINGS_SYNC_LOOKBACK_DAYS` | ○ | `1` | Backward window of the poll |
| `BOOKINGS_DEBUG_PAYLOAD` | ○ | `false` | Predicate tuning: logs the first detected and first undetected event **at INFO** (the root logger sits at INFO in production, so DEBUG would be mute exactly where it is needed). Domains and predicate booleans only — never the subject, which embeds the client's name |
| `MIROIR_OUTLOOK_ACTIF` | ○ | `true` | Kill switch for the Athéna → Outlook mirror. `false` freezes existing mirrors in place; it does not clean them up |
| `MIROIR_OUTLOOK_LOOKAHEAD_DAYS` | ○ | `365` | The mirror's forward window — **shared** by the Athéna read and the Outlook read, which is what keeps a mirror from becoming an invisible orphan |
| `MIROIR_OUTLOOK_LOOKBACK_DAYS` | ○ | `30` | Its backward window |
| `FEATURE_INTAKE` | ○ | `false` | Offers the portal's intake (dossier-opening) form when confirming a Bookings rendez-vous whose email matches no contact |

⚠ **Two Bookings settings have no environment variable at all.**
`BOOKINGS_TYPE_PAR_MOT_CLE` (keyword → hearing type) and
`BOOKINGS_TYPE_DEFAUT` are literals in
[`config.py`](athena/config.py). Publishing a new Bookings service and naming
it in `BOOKINGS_SUBJECT_KEYWORDS` **without** adding it to that map falls back
to « consultation » — logged, but only in the logs. Editing `config.py` is the
only way to change it today.

#### The `portail` service — set in `portail.yaml`

The portal runs in its own process under its own least-privilege service
account, so it re-reads what it needs from its own yaml. `ENV`,
`FIREBASE_PROJECT_ID`, `FIREBASE_APP_ID`, `RECAPTCHA_ENTERPRISE_SITE_KEY`,
`APPCHECK_DEBUG_TOKEN`, `FIRM_NAME` and `FIRM_PHONE` are the same names as
above and must be set **again** there.

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `PORTAIL_SECRET_KEY` → secret `portail-secret-key` | ✔ **hard** (portal) | — | The portal's session key, **distinct** from the main one. The service does not boot without it |
| `PORTAIL_HOST` | ○ | ⚠ `portail.poirierlavoie.ca` — the **original owner's** host | Used to build the fallback link in every invitation email |
| `PORTAIL_BUCKET` | ○ | `{FIREBASE_PROJECT_ID}-portail-quarantaine` | Derived, so it follows your project id by itself |
| `TASKS_LOCATION` | ○ | ⚠ `northamerica-northeast1` (Montréal) | **Must equal your App Engine region** (`gcloud app describe`). It is not derived: deploy elsewhere and leave it, and every `signaler()` raises `NOT_FOUND` at finalization — a failure the portal swallows **by design** (the envelope is the durable truth), so the only symptom is lots arriving in Réception late, at the next run of the reconciliation cron (every 15 minutes during office hours, `cron.yaml`) |

### 4.2 The six Secret Manager secrets (production)

**Six, not four.** This section said "the four" from the day
`portail-secret-key` shipped as a fifth and `graph-client-secret` as a sixth —
which is the predicted behaviour of a hand-kept list, and the reason the table
below is now GENERATED from
[`athena/utils/deployment_inventory.py`](athena/utils/deployment_inventory.py)
and pinned against it by a test.

| Secret | Required for | Where the value comes from | Expected length | If absent or wrong |
|---|---|---|---|---|
| `flask-secret-key` | `default` | you mint it | entre 32 et 256 chars | l'application NE DÉMARRE PAS |
| `portail-secret-key` | `portail` | you mint it | entre 32 et 256 chars | le service « portail » NE DÉMARRE PAS |
| `firebase-api-key` | optional | a console hands it to you | entre 30 et 60 chars | la page de connexion ne peut pas initialiser Firebase |
| `dav-password-hash` | optional | computed from a password you choose | exactement 60 chars | l'authentification DAV ne peut pas réussir — DavX5 cesse de synchroniser SANS message d'erreur |
| `cf-origin-secret` | optional | you mint it | entre 32 et 128 chars | absent, vide ou illisible, le secret DÉSACTIVE le contrôle d'origine — l'accès direct à App Engine n'est plus bloqué, et origin_secret_disabled le signale par l'alerte « Sécurité — une protection est désactivée » ; présent mais différent de la Transform Rule, il fait refuser toute requête venue de Cloudflare (403) |
| `graph-client-secret` | optional | a console hands it to you | entre 20 et 256 chars | le courriel sortant est désactivé |

Created in §6.4. `portail-secret-key` is **required for the `portail` service
to boot** — it is not optional in the way the other five non-`flask` entries
are; the portal simply does not start without it.

The live verdict on all six — does each resolve, is its length right, does it
carry stray whitespace or a character that cannot survive an HTTP header — is
on the **« Paramètres → Configuration »** page, and in
`python -m scripts.check_config --prod`. Both read the same table and the same
predicates as this section.

### 4.3 Owner-specific values to replace

Every value below is hardcoded to the original deployment. Replace each one,
then run the checker in §4.4 — **which names the exact value and the exact
file**, so this table deliberately does not repeat the owner's identifiers a
third time.

The table is GENERATED from `OWNER_LITERALS` and `SCAN_FILES` in
[`athena/utils/deployment_inventory.py`](athena/utils/deployment_inventory.py)
and pinned against them by a test: the « Where » column is not a claim, it is
the result of searching each scanned file for that value. It listed nine rows
until 2026-09-12 and the checker already knew of fifteen.

| Value | Where the checker finds it |
|---|---|
| GCP project id | `athena/app.yaml`, `athena/portail.yaml`, `monitoring/metrics/security_redirect_rejected.json`, `monitoring/metrics/security_control_disabled.json`, `monitoring/metrics/security_appspot_blocked.json`, `monitoring/metrics/security_request_too_large.json`, `monitoring/policies/security_appspot_blocked.json` |
| Storage bucket | `athena/app.yaml` |
| Firebase app id | `athena/app.yaml`, `athena/portail.yaml` |
| Authorized user email | `athena/app.yaml` |
| Firm name | `athena/app.yaml`, `athena/portail.yaml` |
| reCAPTCHA site key | `athena/app.yaml`, `athena/portail.yaml` |
| MCP canonical origin | `athena/app.yaml`, `athena/config.py` |
| Domain | `athena/app.yaml`, `dispatch.yaml`, `athena/config.py` |
| TWA package | `athena/main.py` |
| Portal host | `dispatch.yaml`, `athena/client/config.py` |
| Portal service account | `athena/portail.yaml` |
| Firm phone | `athena/app.yaml`, `athena/portail.yaml` |
| Firm email | `athena/app.yaml` |
| Graph tenant id | `athena/app.yaml` |
| Graph client id | `athena/app.yaml` |
| App Engine region | `athena/client/config.py` |

**Also carrying the owner's identity, outside the scan.** The checker looks at
deployment config only, so these are on you: the firm name, domain and contact
in [`static/legal/privacy.html`](athena/static/legal/privacy.html) and
[`terms.html`](athena/static/legal/terms.html), [README.md](README.md),
[SECURITY.md](SECURITY.md) and [LICENSE](LICENSE); the TWA package and its
SHA-256 signing fingerprint in [main.py](athena/main.py)'s `assetlinks.json`
route (only if you build the Android app, §12); and the placeholder values in
[`.env.example`](.env.example).

⚠ **The reCAPTCHA site key must be replaced *and* the old one rotated** — a
site key is public, so forking the repo hands it to everyone; and a fork that
keeps it is sending App Check attestations against someone else's key.

### 4.4 Verify your configuration

A helper script checks required vars, warns on disabled fail-open controls, and
flags any owner-default literal you forgot to replace:

```bash
cd athena
python -m scripts.check_config          # uses ENV from your shell/.env
python -m scripts.check_config --prod   # force the production ruleset
```

**And a second one checks the INFRASTRUCTURE**, which the first never
looked at — it reads env vars, secrets and committed files, but knows nothing
about whether the queue exists or the firewall lets your own cron through:

```bash
cd athena
python -m scripts.provision --project $PROJECT                  # état + plan
python -m scripts.provision --project $PROJECT --plan-seulement # le plan seul
python -m scripts.provision --project $PROJECT --json           # pour une machine
```

It **never mutates anything**, and that is structural rather than promised:
every command it runs comes from a table whose verbs are restricted, by
allowlist, to `describe` / `list` / `get-iam-policy` — a test enforces it.
There is deliberately no `--apply`.

`--project` is **required and never inherited** from `gcloud config`: a stale
active project is how you inspect — or provision — inside someone else's.
The region is not asked for; it is read from the App Engine application,
which is authoritative because its region is permanent.

Exit codes, and the distinct `2` matters — a CI that conflates « go click in a
console » with « something is broken » ends up ignoring both:

| Code | Meaning |
|---|---|
| `0` | everything detectable is in place |
| `1` | **drift** — a resource exists and contradicts what the code expects |
| `2` | work remains: not yet provisioned, console-only, or not verifiable |

⚠ **A failed probe reports `inconnu`, never « absent ».** `gcloud` missing,
stale credentials, a disabled API — none of those say anything about the
resource, and reporting an absence would send you to provision what is
already there. The first real run proved the rule before it was ever tested:
`subprocess` on Windows resolves only `.exe`, never the SDK's `gcloud.CMD`,
so ten present resources came back « not verified » instead of « missing ».

---

## 5. Order of operations (read this before running anything)

**Three of these are irreversible**, and no later step can undo them: the App
Engine region (3), the default Firestore database's location (4), and — once
the portal is provisioned — the named `portail` database's. Everything else
can be re-run.

**Four are order-sensitive**, and each fails in its own way: indexes before the
first deploy (5), or a query silently returns an empty list while it builds;
the firewall's `0.1.0.2/32` allow **before** the default-deny (12), or you cut
off your own cron and queue; the Cloudflare Transform Rule (12) **before** the
origin secret gets a VALUE (13), or the site answers 403 everywhere with no
deploy to explain it; and the storage bucket before its rules (8).

⚠ **That third one was wrong in this very list until 2026-09-13** — the
preamble warned about the order while the numbered steps put the secrets at 6
and Cloudflare at 12, which is the trap itself. It is split now, and the split
is the honest shape: an empty *container* is inert, a *version* is what arms
the check.

0. **Authenticate** — `gcloud auth login`, `gcloud auth
   application-default login` (the ADC the scripts of §9 use), and
   `firebase login`. This step did not exist in this document until
   2026-09-12: §6.1 opened straight on `gcloud projects create`, and §9 later
   referenced credentials nothing had told you to obtain. Note that
   `.firebaserc` is gitignored and absent, so **every `firebase` command needs
   an explicit `--project`**.
1. Create GCP project + enable billing, then `gcloud config set project
   $PROJECT` (it cannot be selected before it exists — every command below
   also takes `--project=$PROJECT` explicitly, which is the safer habit: a
   stale active project is how you provision into someone else's)
2. Enable required APIs
3. **Create the App Engine app — the region choice is PERMANENT** (§6.2)
4. Create Firestore in native mode (same region)
5. **Deploy indexes + rules — BEFORE the first code deploy** (§6.3). Until an
   index finishes building, the query it serves fails and the view silently
   shows an empty list.
6. Create the six empty secret containers + grant IAM, then write **five** of
   the six VALUES (§6.4). The origin secret's value is deliberately NOT one of
   them — it waits for 13. An empty container is harmless: `config._secret`
   resolves an optional secret with no version to `""`, and
   `_enforce_origin_secret` reads that as « disabled »
7. Firebase Auth: create the single user + enroll Phone MFA (§6.5)
8. Storage bucket **then its rules** (§6.6) — the bucket must exist first
9. App Check + reCAPTCHA (§6.7)
10. Provision the `portail` service (§6.9 steps 1–5, 7 and 9) — the first CI
    build deploys it and fails without its service account — then the first
    deploy + smoke test (§8); §6.9's end-to-end test waits for step 12
11. Seed reference data (§9)
12. Cloudflare edge (§7) — DNS cutover, Full (Strict), the firewall, and the
    zone-wide **Transform Rule** that injects `X-Origin-Auth`. Can be prepared
    in parallel, but it lands here. **Prove the rule fires** with Cloudflare's
    request tracer before going on; do not infer it
13. **Arm the origin secret** — only NOW write `cf-origin-secret`'s first
    version (§6.4). Earlier, you arm the application against a header nobody
    sends, and with `min_instances: 0` the instances recycle by themselves —
    so the site starts answering **403 on every path with no deploy to
    blame**, including the login page you would use to investigate. The value
    takes effect as instances recycle; redeploy if you want it immediate.
    The `portail` service arms ITS check only once `portail-svc` may read
    the secret (the commented grant in §6.4) — grant it after the tracer
    shows the rule firing on the portal host as well
14. **Security alerts** (§6.10) — from the repo's `monitoring/` files: the
    counters first, then, once their descriptors exist, the policies with
    your notification channel. Earlier, `security_control_disabled` fires on
    every fresh instance
15. Optional: DavX5 (§10), MCP (§11), Android TWA (§12)

---

## 6. Google Cloud & Firebase provisioning

Throughout, set `PROJECT=your-project-id` and substitute your own values.

### 6.1 Project, billing, APIs

```bash
gcloud projects create $PROJECT --name="Pallas Athena"
gcloud billing projects link $PROJECT --billing-account=XXXXXX-XXXXXX-XXXXXX
firebase projects:addfirebase $PROJECT

gcloud services enable \
  appengine.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com \
  iam.googleapis.com iamcredentials.googleapis.com \
  firebase.googleapis.com firebaseappcheck.googleapis.com \
  firebaserules.googleapis.com firebasestorage.googleapis.com \
  storage.googleapis.com identitytoolkit.googleapis.com \
  recaptchaenterprise.googleapis.com logging.googleapis.com \
  cloudtrace.googleapis.com telemetry.googleapis.com \
  monitoring.googleapis.com cloudtasks.googleapis.com \
  cloudscheduler.googleapis.com \
  --project=$PROJECT
```

**This list said twelve until 2026-09-12 and omitted six**, of which
`cloudscheduler` breaks an adopter's very first CI
build — `cloudbuild.yaml` deploys `cron.yaml` unconditionally, as its
**fourth** step, so the failure lands after `default`, `portail` and
`dispatch` have already gone out. `monitoring.googleapis.com` joined it on
2026-11-08 with the security alerts of §6.10. The block above and the table
below are both GENERATED from
[`athena/utils/deployment_inventory.py`](athena/utils/deployment_inventory.py)
and pinned against it by a test, so an API added to the code announces
itself here or the suite goes red.

| API | Why it is required | Who calls it |
|---|---|---|
| `appengine.googleapis.com` | Les deux services App Engine y tournent. | app.yaml, portail.yaml |
| `firestore.googleapis.com` | La base par défaut ET la base NOMMÉE « portail » — ce sont deux bases du même service. | models/, client/services/invitations.py |
| `secretmanager.googleapis.com` | Les six secrets de §4.2. Sans elle, `config.py` lève à l'import et le service ne démarre pas du tout. | config.py, client/config.py |
| `cloudbuild.googleapis.com` | Le déclencheur qui exécute la suite puis déploie. | cloudbuild.yaml |
| `iam.googleapis.com` | Les liaisons de rôles que §6.4 pose. | les commandes de §6.4 |
| `iamcredentials.googleapis.com` | L'API `signBlob`. Sans elle l'auto-impersonation échoue et AUCUN URL signé n'est produit — en silence, et en PRODUCTION seulement : en local une clé de compte de service signe sur place, donc ce chemin n'est jamais emprunté avant le déploiement. | models/document.sign_blob_url, models/doc_template |
| `firebase.googleapis.com` | La gestion du projet Firebase. | la CLI firebase |
| `firebaseappcheck.googleapis.com` | App Check, qui vérifie l'attestation des requêtes HTMX. | security.py |
| `firebaserules.googleapis.com` | Le déploiement des règles Firestore et Storage. Sans elle `firebase deploy --only firestore:rules,storage` échoue — et ces règles SONT le refus par défaut qui couvre chaque collection. | firestore.rules, storage.rules |
| `firebasestorage.googleapis.com` | Le seau par défaut de Firebase Storage et ses règles. | firebase-admin.storage |
| `storage.googleapis.com` | L'API JSON de GCS elle-même : URL signés, sessions reprenables, ingestion par rewrite, composition du ZIP d'un dossier de classement. `firebasestorage` gère le seau ; c'est celle-ci qui déplace les octets. | google-cloud-storage |
| `identitytoolkit.googleapis.com` | Firebase Auth — la session, la MFA, et le lien courriel du portail. | auth.py, client/routes.py |
| `recaptchaenterprise.googleapis.com` | Le fournisseur d'attestation d'App Check. | security.py, base.html |
| `logging.googleapis.com` | Le journal structuré, par `CloudLoggingHandler`. Sans elle, il ne reste aucune trace agrégée de ce que la production a fait. | utils/logging_setup.py |
| `cloudtrace.googleapis.com` | Doit rester activée À CÔTÉ de `telemetry` : la note de migration de Google est explicite — désactiver Cloud Trace fait JETER les traces envoyées à l'API Telemetry, en silence. C'est aussi elle qui sert la LECTURE des traces. | utils/tracing_setup.py, la console |
| `telemetry.googleapis.com` | La destination OTLP des spans depuis le 2026-07-30. | utils/tracing_setup.py |
| `monitoring.googleapis.com` | Les politiques d'alerte de sécurité de §6.10 et le canal qui écrit au juriste. Sans elle aucune alerte ne se crée, et la sonde de ces politiques lit « inconnu ». | DEPLOYMENT.md §6.10, scripts/provision.py |
| `cloudtasks.googleapis.com` | La file « portail » par laquelle le service public signale. | client/services/taches.py |
| `cloudscheduler.googleapis.com` | EXIGÉE par `gcloud app deploy cron.yaml`. Son absence fait échouer cette étape sur `SERVICE_DISABLED`, et `cloudbuild.yaml` la place en QUATRIÈME : l'échec arrive donc APRÈS que `default`, `portail` et `dispatch` sont déployés. La première construction d'un adoptant se termine rouge sur un déploiement à moitié fait. | cloudbuild.yaml, cron.yaml |

**`telemetry.googleapis.com` is where spans are now sent** (OTLP, since
2026-07-30 — see `athena/OBSERVABILITY.md`). **`cloudtrace.googleapis.com` must
stay enabled alongside it**, and that is not redundancy: Google's own migration
note is explicit that "if you disable the Cloud Trace API, then Google Cloud
Observability discards trace data you send to the Telemetry API" — silently.
Trace *reading* (the console, the log-entry « View trace » link) is also still
served by the Cloud Trace API.

No IAM change accompanied the migration: `roles/telemetry.tracesWriter` grants
only `telemetry.traces.write`, which `roles/cloudtrace.agent` already carries —
and both service accounts (`$PROJECT@appspot` and `portail-svc`) hold it. If
spans stop arriving with `PERMISSION_DENIED`, that assumption is the first
thing to re-verify.

### 6.2 App Engine app — choose the region carefully (permanent)

```bash
gcloud app create --region=northamerica-northeast1 --project=$PROJECT
```

The original deploys to `northamerica-northeast1` (Montréal). **You cannot
change an App Engine app's region later** — pick the region closest to you and
your data-residency obligations before running this. Firestore should use the
same region (next step).

### 6.3 Firestore (native mode) + indexes + rules

```bash
gcloud firestore databases create \
  --location=northamerica-northeast1 --type=firestore-native --project=$PROJECT

# From the repo root (firebase.json points at the root rule/index files).
# NOTE: storage rules are NOT deployed here — the bucket does not exist yet.
# They go in §6.6, right after you create it.
firebase deploy --only firestore:indexes,firestore:rules --project $PROJECT
```

- **Native mode**, not Datastore mode.
- The rules are intentional **deny-all** — all access is through the Admin SDK
  and signed URLs, which bypass rules. This is also what stops a self-signed-up
  Firebase account from reading your data.
- `firestore.indexes.json` contains the composite indexes (dashboard
  aggregations, cursor-paginated lists, the protocol-steps collection group).
  **They must finish building before you send real traffic.** There is no
  « done » signal from the CLI; watch Firebase console → Firestore → Indexes
  until every one reads **Enabled**.
- ⚠ **The storage rules moved to §6.6.** Until 2026-09-12 this command carried
  `,storage`, which asks the Firebase CLI to deploy rules for a bucket §6.6 has
  not created yet. Doing it in the documented order is strictly safer;
  *whether* the combined form hard-errors or silently no-ops on a project with
  no default bucket has not been tested here, and would need a fresh project to
  settle.

### 6.4 Secret Manager + IAM

**Everything in this section below the IAM grants is GENERATED** from
`athena/utils/deployment_inventory.gcloud_recipe()` and pinned against it by
`athena/tests/test_deployment_inventory.py`. It is therefore the *same*
recipe the « Paramètres → Configuration » page renders beside each secret for
every later rotation — the doc and the page cannot drift, which is the whole
reason the shared table exists. Do not hand-edit the command blocks; change
the function.

The comments inside the blocks are in **French**. That is not an oversight:
they are the very strings the French settings page renders, generated rather
than translated. A second, English copy is exactly the thing that would drift.

> **What the previous version of this section got wrong**, kept here because
> each defect is instructive. It ran `hashpw(b'YOUR_DAV_PASSWORD', …)`, which
> deposits the **plaintext DAV password** in `~/.bash_history`. Every line
> said `gcloud secrets create --data-file=-` — correct on a first deploy, and
> wrong for every rotation since, where the secret already exists and `create`
> fails in a way that reads like a platform fault. And every line ended in
> `| tr -d '\n'` to mop up a newline `sys.stdout.write` never emits, which
> teaches a superstition instead of the rule.

> **The rule itself is one sentence: nothing in the pipeline may emit a
> trailing newline.** `gcloud secrets versions add --data-file=-` stores the
> payload **byte for byte**, and nothing in `config.py` strips it (`_secret` →
> `_from_secret_manager` decodes and returns as-is). So `printf '%s'` and
> `sys.stdout.write` are safe; `echo`, `print()` and a heredoc are not. For
> `cf-origin-secret` one stray byte is site-wide downtime — `security.py`'s
> `hmac.compare_digest` compares the stored value against a header a
> Cloudflare Transform Rule injects, and **a rule cannot emit a newline**, so
> every request answers 403. `dav-password-hash` has the same trap for a
> different reason: a 61-byte hash never matches the 60 bytes `checkpw`
> recomputes, and DavX5 then fails **silently**.

> ⚠ **bash only — and on Windows that is a measurement, not a preference.**
> Measured on the maintainer's Windows 11 machine, 2026-09-11.
>
> **Git Bash works.** `printf '%s' "$V" | <native exe>` delivers exactly the
> bytes held in `$V` — 3 for `abc`, 43 for a 43-character token. The negative
> control is what makes that mean something: `echo`, a heredoc and a
> here-string each deliver one byte MORE, and the extra byte is a bare `\n`,
> so the MSYS→native pipe performs no LF↔CRLF translation either. Verified
> across all 256 byte values, at 200 KB, and into gcloud's own
> `files.ReadStdinBytes()`. On that machine `gcloud` resolves to a POSIX
> launcher, never to `gcloud.cmd`, so `cmd.exe` is not in the chain — check
> yours with `file $(command -v gcloud)`.
>
> ⚠ **But `python3` is a trap on Windows, and it fails SILENTLY.** It is
> present on PATH as a Microsoft Store App Execution Alias that exits 49 and
> writes nothing, so `command -v python3` SUCCEEDS while the interpreter is
> broken. Found by running this section for real against a throwaway secret on
> 2026-09-11: the generated value came out EMPTY, and only the length
> comparison stopped an empty `cf-origin-secret` from being written — which
> would have silently disabled the entire origin check. Every block below
> therefore resolves the interpreter by TRYING it, never by looking for it.
>
> **PowerShell does not work, and it is worse than a stray newline.** `abc`
> arrives at a NATIVE executable as **8 bytes** — `ef bb bf 61 62 63 0d 0a`.
> But on Windows `gcloud` is itself a `.ps1` that re-pipes (`gcloud.ps1:118`,
> `$input | & "$exe_path"`), so the payload is terminated TWICE. Measured end
> to end through the real `gcloud` into a real Secret Manager on 2026-09-11:
> ten ASCII characters were stored as **16 bytes**,
> `efbbbf 6162636465666768696a 0d 0d 0a` — the BOM, the payload, then the CR
> left over from the first hop followed by the second hop's CRLF. Three
> mechanisms, not one: a CRLF appended once per pipeline OBJECT (unconditional, in every
> idiom tested, including native→native and including when the upstream
> program emitted nothing); a 3-byte BOM prepended, which comes from
> `[Console]::InputEncoding`; and `$OutputEncoding` re-encoding the payload —
> Windows PowerShell 5.1 defaults it to **ASCII** (code page 20127), so an
> accented character silently becomes `?`. That last one corrupts the
> CONTENT, not just the tail. PowerShell 7 was not installed and therefore
> not measured; do not assume it is safe.
>
> **Google Cloud Shell remains a good second venue** — a clean Linux
> userland, no trace on your own disk, reachable from a phone. What it does
> NOT buy is better audit attribution: a local `gcloud` authenticated as you
> already writes as you. This document claimed otherwise until 2026-09-11.
> The audit log settles it — `cf-origin-secret` version 1 was written from a
> Windows laptop by the local CLI and Cloud Audit attributed it to the
> practitioner, as it did for 26 of 26 human secret writes over 400 days.

**First, create the six empty containers.** `gcloud secrets create` without
`--data-file` makes the container and no version; the first version is written
by the per-secret blocks below.

```bash
REGION=northamerica-northeast1   # the SAME region as §6.2

for s in flask-secret-key portail-secret-key firebase-api-key dav-password-hash cf-origin-secret graph-client-secret; do
  gcloud secrets create $s --project=$PROJECT \
    --replication-policy=user-managed --locations=$REGION
done
```

> **Replication is permanent per secret.** `user-managed` pinned to the App
> Engine region keeps the payloads in one jurisdiction, which for a law
> practice is a data-residency question rather than a preference.
> `--replication-policy=automatic` is the alternative and cannot be changed
> afterwards — you would have to create a new secret under a new name.

**Then write the first version of each.** These are the same commands the
Configuration page renders for a rotation; on a rotation you run only these,
never the `create` above — and for `cf-origin-secret`, as the first of the
three steps of its rotation note (below the IAM grants): the same value in
the Transform Rule, then both services redeployed.

#### `flask-secret-key` — Clé de signature des sessions Flask

If this value is wrong or absent: l'application NE DÉMARRE PAS.

```bash
# 1. Résoudre l'interpréteur en l'ESSAYANT, jamais en le cherchant. Sur Windows, `python3` est un raccourci du Microsoft Store : il EXISTE sur le PATH et sort en code 49 sans rien produire, si bien qu'un `command -v` le choisirait et que la valeur engendrée serait vide.
PY=""; for c in python3 python py; do "$c" -c '' >/dev/null 2>&1 && { PY=$c; break; }; done
if [ -n "$PY" ]; then
  printf 'interpréteur : %s\n' "$PY"
else
  printf 'aucun interpréteur Python utilisable — REFUSER\n'
fi

# 2. Frapper une valeur neuve. `sys.stdout.write` plutôt que `print` par discipline : aucune ligne de cette recette n'émet de saut de ligne. Et `secrets` plutôt qu'un tirage depuis `/dev/urandom` : c'est le générateur cryptographique de la bibliothèque standard, ce qui vaut de dépendre d'un interpréteur.
VALEUR=$("$PY" -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(32))')

# 3. Voir ce qui a réellement été saisi. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement.
printf '[%s]\n' "$VALEUR"

# 4. Mesurer, puis écrire SEULEMENT si la longueur est entre 32 et 256 octets. La mesure est une GARDE, pas un avis : la commande d'écriture vit DANS le `if`, donc coller le bloc entier ne peut pas écrire une valeur que la mesure vient de refuser. `printf` est une primitive du shell, donc la valeur ne devient l'argument d'aucun processus ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps` ; et `versions add`, jamais `secrets create` — le secret existe déjà.
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 32 ] && [ "$N" -le 256 ]; then
  VERSION=$(printf '%s' "$VALEUR" | gcloud secrets versions add flask-secret-key \
    --project=$PROJECT --data-file=- --format='value(name)')
  printf 'écrit : %s octets, version %s\n' "$N" "${VERSION##*/}"
else
  VERSION=""
  printf "longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été écrit\n" "$N"
fi

# 5. Effacer la variable de la session.
unset VALEUR

# 6. Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`. Si l'écriture avait échoué, `latest` désignerait la version précédente, qui a toutes les chances d'être de bonne longueur puisqu'elle fonctionnait : l'après-vol rassurerait alors sur une écriture qui n'a pas eu lieu.
if [ -z "$VERSION" ]; then
  printf 'aucune version écrite — rien à relire\n'
else
  N=$(gcloud secrets versions access "${VERSION##*/}" --secret=flask-secret-key \
    --project=$PROJECT | wc -c | tr -d ' ')
  if [ "$N" -ge 32 ] && [ "$N" -le 256 ]; then
    printf 'relu : %s octets — conforme\n' "$N"
  else
    printf 'relu : %s octets — NE CORRESPOND PAS\n' "$N"
  fi
fi
```

#### `portail-secret-key` — Clé de session du service « portail »

If this value is wrong or absent: le service « portail » NE DÉMARRE PAS.

```bash
# 1. Résoudre l'interpréteur en l'ESSAYANT, jamais en le cherchant. Sur Windows, `python3` est un raccourci du Microsoft Store : il EXISTE sur le PATH et sort en code 49 sans rien produire, si bien qu'un `command -v` le choisirait et que la valeur engendrée serait vide.
PY=""; for c in python3 python py; do "$c" -c '' >/dev/null 2>&1 && { PY=$c; break; }; done
if [ -n "$PY" ]; then
  printf 'interpréteur : %s\n' "$PY"
else
  printf 'aucun interpréteur Python utilisable — REFUSER\n'
fi

# 2. Frapper une valeur neuve. `sys.stdout.write` plutôt que `print` par discipline : aucune ligne de cette recette n'émet de saut de ligne. Et `secrets` plutôt qu'un tirage depuis `/dev/urandom` : c'est le générateur cryptographique de la bibliothèque standard, ce qui vaut de dépendre d'un interpréteur.
VALEUR=$("$PY" -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(32))')

# 3. Voir ce qui a réellement été saisi. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement.
printf '[%s]\n' "$VALEUR"

# 4. Mesurer, puis écrire SEULEMENT si la longueur est entre 32 et 256 octets. La mesure est une GARDE, pas un avis : la commande d'écriture vit DANS le `if`, donc coller le bloc entier ne peut pas écrire une valeur que la mesure vient de refuser. `printf` est une primitive du shell, donc la valeur ne devient l'argument d'aucun processus ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps` ; et `versions add`, jamais `secrets create` — le secret existe déjà.
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 32 ] && [ "$N" -le 256 ]; then
  VERSION=$(printf '%s' "$VALEUR" | gcloud secrets versions add portail-secret-key \
    --project=$PROJECT --data-file=- --format='value(name)')
  printf 'écrit : %s octets, version %s\n' "$N" "${VERSION##*/}"
else
  VERSION=""
  printf "longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été écrit\n" "$N"
fi

# 5. Effacer la variable de la session.
unset VALEUR

# 6. Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`. Si l'écriture avait échoué, `latest` désignerait la version précédente, qui a toutes les chances d'être de bonne longueur puisqu'elle fonctionnait : l'après-vol rassurerait alors sur une écriture qui n'a pas eu lieu.
if [ -z "$VERSION" ]; then
  printf 'aucune version écrite — rien à relire\n'
else
  N=$(gcloud secrets versions access "${VERSION##*/}" --secret=portail-secret-key \
    --project=$PROJECT | wc -c | tr -d ' ')
  if [ "$N" -ge 32 ] && [ "$N" -le 256 ]; then
    printf 'relu : %s octets — conforme\n' "$N"
  else
    printf 'relu : %s octets — NE CORRESPOND PAS\n' "$N"
  fi
fi
```

#### `firebase-api-key` — Clé d'API navigateur Firebase

If this value is wrong or absent: la page de connexion ne peut pas initialiser Firebase.

```bash
# 1. Coller la valeur remise par la console, puis Entrée. Rien ne s'affiche : `-s` la tait, et `IFS=` empêche le shell de manger les blancs — s'il y en a, on veut les VOIR, pas les perdre en silence. ⚠ `read` ne retient que la PREMIÈRE LIGNE : les six secrets sont d'une seule ligne par construction, donc un collage multiligne signifie qu'on a collé la mauvaise chose — et le collage tronqué a l'air complet à l'écran. C'est la mesure qui l'attrape, pas l'œil.
IFS= read -rs VALEUR

# 2. Voir ce qui a réellement été saisi. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement.
printf '[%s]\n' "$VALEUR"

# 3. Mesurer, puis écrire SEULEMENT si la longueur est entre 30 et 60 octets. La mesure est une GARDE, pas un avis : la commande d'écriture vit DANS le `if`, donc coller le bloc entier ne peut pas écrire une valeur que la mesure vient de refuser. `printf` est une primitive du shell, donc la valeur ne devient l'argument d'aucun processus ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps` ; et `versions add`, jamais `secrets create` — le secret existe déjà.
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 30 ] && [ "$N" -le 60 ]; then
  VERSION=$(printf '%s' "$VALEUR" | gcloud secrets versions add firebase-api-key \
    --project=$PROJECT --data-file=- --format='value(name)')
  printf 'écrit : %s octets, version %s\n' "$N" "${VERSION##*/}"
else
  VERSION=""
  printf "longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été écrit\n" "$N"
fi

# 4. Effacer la variable de la session.
unset VALEUR

# 5. Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`. Si l'écriture avait échoué, `latest` désignerait la version précédente, qui a toutes les chances d'être de bonne longueur puisqu'elle fonctionnait : l'après-vol rassurerait alors sur une écriture qui n'a pas eu lieu.
if [ -z "$VERSION" ]; then
  printf 'aucune version écrite — rien à relire\n'
else
  N=$(gcloud secrets versions access "${VERSION##*/}" --secret=firebase-api-key \
    --project=$PROJECT | wc -c | tr -d ' ')
  if [ "$N" -ge 30 ] && [ "$N" -le 60 ]; then
    printf 'relu : %s octets — conforme\n' "$N"
  else
    printf 'relu : %s octets — NE CORRESPOND PAS\n' "$N"
  fi
fi
```

#### `dav-password-hash` — Empreinte bcrypt du mot de passe DAV

If this value is wrong or absent: l'authentification DAV ne peut pas réussir — DavX5 cesse de synchroniser SANS message d'erreur.

```bash
# 1. Résoudre l'interpréteur en l'ESSAYANT, jamais en le cherchant. Sur Windows, `python3` est un raccourci du Microsoft Store : il EXISTE sur le PATH et sort en code 49 sans rien produire, si bien qu'un `command -v` le choisirait et que la valeur engendrée serait vide.
PY=""; for c in python3 python py; do "$c" -c '' >/dev/null 2>&1 && { PY=$c; break; }; done
if [ -n "$PY" ]; then
  printf 'interpréteur : %s\n' "$PY"
else
  printf 'aucun interpréteur Python utilisable — REFUSER\n'
fi

# 2. Vérifier que bcrypt est présent DANS cet interpréteur — et non ailleurs. L'ancienne ligne `-m pip install` ne marche pas partout : le venv de ce dépôt n'a pas `pip` du tout (« No module named pip », mesuré), et bcrypt y est déjà puisque c'est une dépendance épinglée. On constate donc au lieu d'installer à l'aveugle.
"$PY" -c 'import bcrypt' 2>/dev/null && printf 'bcrypt : présent\n' || printf 'bcrypt ABSENT de cet interpréteur — à installer avant de continuer\n'

# 3. Calculer l'empreinte. Le mot de passe n'est jamais un argument ni une variable — `getpass` le lit du terminal. L'EMPREINTE, elle, peut vivre dans une variable : c'est un condensé, et c'est ce qui permet de la mesurer AVANT de l'écrire, contrôle que la forme fusionnée précédente ne pouvait pas offrir.
EMPREINTE=$("$PY" -c 'import bcrypt, getpass, sys; sys.stdout.write(bcrypt.hashpw(getpass.getpass("Mot de passe DAV : ").encode(), bcrypt.gensalt()).decode())')

# 4. Mesurer, puis écrire SEULEMENT si la longueur est exactement 60. Une empreinte de 61 octets n'égalera jamais les 60 que `bcrypt.checkpw` recalcule, et DavX5 cesse alors de synchroniser sans un mot.
N=$(printf '%s' "$EMPREINTE" | wc -c | tr -d ' ')
if [ "$N" -eq 60 ]; then
  VERSION=$(printf '%s' "$EMPREINTE" | gcloud secrets versions add dav-password-hash \
    --project=$PROJECT --data-file=- --format='value(name)')
  printf 'écrit : %s octets, version %s\n' "$N" "${VERSION##*/}"
else
  VERSION=""
  printf "longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été écrit\n" "$N"
fi

# 5. Effacer l'empreinte de la session.
unset EMPREINTE

# 6. Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`, qui désignerait la version précédente si l'écriture avait échoué et rassurerait donc à tort.
if [ -z "$VERSION" ]; then
  printf 'aucune version écrite — rien à relire\n'
else
  N=$(gcloud secrets versions access "${VERSION##*/}" --secret=dav-password-hash \
    --project=$PROJECT | wc -c | tr -d ' ')
  if [ "$N" -eq 60 ]; then
    printf 'relu : %s octets — conforme\n' "$N"
  else
    printf 'relu : %s octets — NE CORRESPOND PAS\n' "$N"
  fi
fi
```

#### `cf-origin-secret` — Secret d'origine Cloudflare

If this value is wrong or absent: absent, vide ou illisible, le secret DÉSACTIVE le contrôle d'origine — l'accès direct à App Engine n'est plus bloqué, et origin_secret_disabled le signale par l'alerte « Sécurité — une protection est désactivée » ; présent mais différent de la Transform Rule, il fait refuser toute requête venue de Cloudflare (403).

```bash
# 1. Résoudre l'interpréteur en l'ESSAYANT, jamais en le cherchant. Sur Windows, `python3` est un raccourci du Microsoft Store : il EXISTE sur le PATH et sort en code 49 sans rien produire, si bien qu'un `command -v` le choisirait et que la valeur engendrée serait vide.
PY=""; for c in python3 python py; do "$c" -c '' >/dev/null 2>&1 && { PY=$c; break; }; done
if [ -n "$PY" ]; then
  printf 'interpréteur : %s\n' "$PY"
else
  printf 'aucun interpréteur Python utilisable — REFUSER\n'
fi

# 2. Frapper une valeur neuve. `sys.stdout.write` plutôt que `print` par discipline : aucune ligne de cette recette n'émet de saut de ligne. Et `secrets` plutôt qu'un tirage depuis `/dev/urandom` : c'est le générateur cryptographique de la bibliothèque standard, ce qui vaut de dépendre d'un interpréteur.
VALEUR=$("$PY" -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(32))')

# 3. Voir ce qui a réellement été saisi. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement.
printf '[%s]\n' "$VALEUR"

# 4. Mesurer, puis écrire SEULEMENT si la longueur est entre 32 et 128 octets. La mesure est une GARDE, pas un avis : la commande d'écriture vit DANS le `if`, donc coller le bloc entier ne peut pas écrire une valeur que la mesure vient de refuser. `printf` est une primitive du shell, donc la valeur ne devient l'argument d'aucun processus ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps` ; et `versions add`, jamais `secrets create` — le secret existe déjà.
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 32 ] && [ "$N" -le 128 ]; then
  VERSION=$(printf '%s' "$VALEUR" | gcloud secrets versions add cf-origin-secret \
    --project=$PROJECT --data-file=- --format='value(name)')
  printf 'écrit : %s octets, version %s\n' "$N" "${VERSION##*/}"
else
  VERSION=""
  printf "longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été écrit\n" "$N"
fi

# 5. Effacer la variable de la session.
unset VALEUR

# 6. Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`. Si l'écriture avait échoué, `latest` désignerait la version précédente, qui a toutes les chances d'être de bonne longueur puisqu'elle fonctionnait : l'après-vol rassurerait alors sur une écriture qui n'a pas eu lieu.
if [ -z "$VERSION" ]; then
  printf 'aucune version écrite — rien à relire\n'
else
  N=$(gcloud secrets versions access "${VERSION##*/}" --secret=cf-origin-secret \
    --project=$PROJECT | wc -c | tr -d ' ')
  if [ "$N" -ge 32 ] && [ "$N" -le 128 ]; then
    printf 'relu : %s octets — conforme\n' "$N"
  else
    printf 'relu : %s octets — NE CORRESPOND PAS\n' "$N"
  fi
fi
```

#### `graph-client-secret` — Secret client Microsoft Graph

If this value is wrong or absent: le courriel sortant est désactivé.

```bash
# 1. Coller la valeur remise par la console, puis Entrée. Rien ne s'affiche : `-s` la tait, et `IFS=` empêche le shell de manger les blancs — s'il y en a, on veut les VOIR, pas les perdre en silence. ⚠ `read` ne retient que la PREMIÈRE LIGNE : les six secrets sont d'une seule ligne par construction, donc un collage multiligne signifie qu'on a collé la mauvaise chose — et le collage tronqué a l'air complet à l'écran. C'est la mesure qui l'attrape, pas l'œil.
IFS= read -rs VALEUR

# 2. Voir ce qui a réellement été saisi. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement.
printf '[%s]\n' "$VALEUR"

# 3. Mesurer, puis écrire SEULEMENT si la longueur est entre 20 et 256 octets. La mesure est une GARDE, pas un avis : la commande d'écriture vit DANS le `if`, donc coller le bloc entier ne peut pas écrire une valeur que la mesure vient de refuser. `printf` est une primitive du shell, donc la valeur ne devient l'argument d'aucun processus ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps` ; et `versions add`, jamais `secrets create` — le secret existe déjà.
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 20 ] && [ "$N" -le 256 ]; then
  VERSION=$(printf '%s' "$VALEUR" | gcloud secrets versions add graph-client-secret \
    --project=$PROJECT --data-file=- --format='value(name)')
  printf 'écrit : %s octets, version %s\n' "$N" "${VERSION##*/}"
else
  VERSION=""
  printf "longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été écrit\n" "$N"
fi

# 4. Effacer la variable de la session.
unset VALEUR

# 5. Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`. Si l'écriture avait échoué, `latest` désignerait la version précédente, qui a toutes les chances d'être de bonne longueur puisqu'elle fonctionnait : l'après-vol rassurerait alors sur une écriture qui n'a pas eu lieu.
if [ -z "$VERSION" ]; then
  printf 'aucune version écrite — rien à relire\n'
else
  N=$(gcloud secrets versions access "${VERSION##*/}" --secret=graph-client-secret \
    --project=$PROJECT | wc -c | tr -d ' ')
  if [ "$N" -ge 20 ] && [ "$N" -le 256 ]; then
    printf 'relu : %s octets — conforme\n' "$N"
  else
    printf 'relu : %s octets — NE CORRESPOND PAS\n' "$N"
  fi
fi
```

Each block's last command does the verification itself — it reads the stored
value back and lets the SHELL compare the byte count, rather than printing a
number for you to check by eye.

**The byte count is the complete check, and `xxd | tail -1` is not.** This
document used to advise inspecting the last line of a hex dump for a trailing
`0a`. That only looks at the END. PowerShell's worst artefact is a 3-byte BOM
at the FRONT, which a tail inspection cannot see; a byte count sees both (3
added in front, 2 at the back). It also never prints the secret, which a hex
dump does.

The count is also the only thing that catches the paste failure an eye cannot:
`IFS= read -rs` keeps only the **first line** of a multi-line paste, and the
bracketed echo then displays a truncated value that looks perfectly complete.
Measured: 63 bytes pasted, 20 stored, brackets clean. All six secrets here are
single-line by construction, so a multi-line paste means the wrong thing was
copied.

Grant IAM. The two service accounts are the **App Engine default SA**
(`$PROJECT@appspot.gserviceaccount.com`) and whatever SA your **Cloud Build
trigger runs as** (see the note below):

```bash
AE_SA="$PROJECT@appspot.gserviceaccount.com"

# App Engine runtime SA:
gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$AE_SA" --role="roles/logging.logWriter"
gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$AE_SA" --role="roles/cloudtrace.agent"
# `graph-client-secret` was MISSING from this loop. `Config.GRAPH_CLIENT_SECRET`
# is read with required=False, which swallows a PERMISSION_DENIED into "" — so
# a missing binding here turns outbound email (invitations, accusés, intake
# confirmations) off SILENTLY. `portail-secret-key` is deliberately absent: it
# belongs to the `portail` service account, not to this one.
for s in flask-secret-key firebase-api-key dav-password-hash cf-origin-secret graph-client-secret; do
  gcloud secrets add-iam-policy-binding $s --member="serviceAccount:$AE_SA" \
    --role="roles/secretmanager.secretAccessor" --project=$PROJECT
done

# `portail-secret-key` is granted to the PORTAL service account instead — it
# belongs to the second App Engine service, whose least-privilege SA is the
# whole point of keeping the two session keys apart:
#   gcloud secrets add-iam-policy-binding portail-secret-key \
#     --member="serviceAccount:portail-svc@$PROJECT.iam.gserviceaccount.com" \
#     --role="roles/secretmanager.secretAccessor" --project=$PROJECT
#
# `cf-origin-secret` too, for the portal's OWN origin check (since
# 2026-09-30, client/security.enforce_edge). This grant is what ARMS it:
# without it the portal reads the secret as unreadable, fails OPEN and logs
# `origin_secret_disabled` once per worker. Grant it only AFTER the
# Cloudflare request tracer has shown the zone-wide Transform Rule injecting
# `X-Origin-Auth` on the portal host — arming first would 403 every client.
# New instances arm; running ones keep the value they read at startup.
# (Granted on the original deployment 2026-09-30, after the tracer proof.)
# Both services read the secret ONCE per gunicorn worker (one per
# instance), when the worker starts, so a ROTATION is three steps chained
# without a pause: a new version of the secret (the recipe above), the SAME
# value in the Cloudflare Transform Rule that injects `X-Origin-Auth`
# (§7 step 4), then `default` AND `portail` redeployed — by hand, from a
# clean checkout of the commit the services run (`origin/main`, once its
# last build is green): `cd athena && gcloud app deploy app.yaml
# portail.yaml --project=$PROJECT`. A push to `main` runs the test suite
# first, minutes more. Two 403 windows open, on requests through the edge
# only (crons, tasks and `/_ah/` pass): from the new secret version until
# the rule, a worker that STARTS reads the new value and refuses the old one
# the edge still sends; from the rule until a service's redeploy, its
# workers started before the new secret version refuse the new value.
# Expect 403s, some intermittent, for those minutes.
#   gcloud secrets add-iam-policy-binding cf-origin-secret \
#     --member="serviceAccount:portail-svc@$PROJECT.iam.gserviceaccount.com" \
#     --role="roles/secretmanager.secretAccessor" --project=$PROJECT
# The portal's other grants (its bucket, named database, queue and project
# roles) are in §6.9.

# REQUIRED for signed Storage URLs: the runtime SA must be able to sign as ITSELF
# (iam.signBlob self-impersonation). Without this, document & gabarit uploads and
# downloads silently fail to produce a signed URL in production.
gcloud iam service-accounts add-iam-policy-binding $AE_SA \
  --member="serviceAccount:$AE_SA" --role="roles/iam.serviceAccountTokenCreator" --project=$PROJECT
```

**Cloud Build SA note:** the original grants deploy rights to the Firebase Admin
SDK SA and configures the trigger to *run as* that SA. If you instead use the
default build identity, grant these to whichever SA your build actually runs as:

```bash
BUILD_SA="<the SA your Cloud Build trigger runs as>"
gcloud iam service-accounts add-iam-policy-binding $AE_SA \
  --member="serviceAccount:$BUILD_SA" --role="roles/iam.serviceAccountUser" --project=$PROJECT
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$BUILD_SA" --role="roles/appengine.appAdmin"
```

### 6.5 Firebase Auth — the single user + Phone MFA

In the Firebase console (there is no clean gcloud path for these):
1. **Authentication → Sign-in method:** enable **Email/Password**.
2. **Authentication → Sign-in method → Advanced:** enable **SMS Multi-factor**.
3. **Authentication → Users:** create **one** user whose email is exactly your
   `AUTHORIZED_USER_EMAIL`. Any other email is rejected at login with *"Accès
   non autorisé"*.
4. Log in once to enrol a phone as the second factor. With `REQUIRE_MFA=true`,
   any ID token lacking a second factor is refused.

> **Lockout warning:** losing the enrolled phone locks you out. Keep Firebase
> console access as your recovery path.

#### 6.5.1 Recovering from a lockout

With `REQUIRE_MFA=true`, `auth.py` refuses any ID token lacking
`sign_in_second_factor`. So with zero enrolled factors, sign-in succeeds at
Firebase and then `/auth/verify-token` refuses the session — and because
`/parametres/securite` is behind `@login_required`, **the application cannot
repair its own lockout.** Recovery is always out of band, in this order:

1. **Fastest, and the only path that needs nothing but deploy access.** Set
   `REQUIRE_MFA: "false"` in `app.yaml`, deploy, log in with the password
   alone, open **Paramètres → Sécurité**, enrol a number, then set it back to
   `"true"` and deploy again. The account is password-only for the duration
   of that window.
2. **Firebase console** → Authentication → Users → the account → manage
   second factors.
3. **Both paths run through the Google account**, whose own 2FA and recovery
   codes are the real single point of failure. Print those codes and store
   them off this machine. Athena's lockout risk is downstream of that, not
   independent of it.

The application is built so it can only ever *refuse* to create this state:
the last enrolled factor cannot be removed while `REQUIRE_MFA` is on, and
changing a number is additive (enrol the new one, verify by logging in, only
then remove the old).

#### 6.5.2 Manual verification of « Paramètres → Sécurité »

Nearly all of this page is client-side JavaScript, which the pytest suite
**cannot execute** (there is no jest/jsdom/Playwright in this repo). The
automated tests cover routing, CSRF, nonce discipline, config plumbing and
the journal contract — **not one line** of the re-auth dance, the enrolment
ordering, or the guards. This list is what covers the rest.

Run it on a quiet day, with the Firebase console open in another tab and
§6.5.1 printed on paper. **Never exercise the destructive paths against the
real `AUTHORIZED_USER_EMAIL`** — mint a throwaway Firebase user and point
`AUTHORIZED_USER_EMAIL` at it on a `gcloud app deploy --no-promote` version.

| # | Check |
|---|---|
| **M0** | **GATE — run before the removal branch is trusted.** Enrol a **second** phone factor while the first is enrolled. Record `enrolledFactors.length` and whether the next login offers a choice of hints. If enrolling a second factor fails on this project, the in-app number change must not be used at all — correct from the console instead. |
| M1 | Measure the recent-login window: unlock, wait 6 min, attempt an enrol. The page re-locks after 5 min by design; confirm the message is legible rather than a raw error. |
| M2 | Local dev with `RECAPTCHA_ENTERPRISE_SITE_KEY` **unset**: the page loads, the console shows no `firebase is not defined`, and one of the four state panels renders. |
| M3 | Key **set**: the page loads **and** App Check still works elsewhere — load `/dossiers` and confirm an htmx request still carries `X-Firebase-AppCheck`. Proves the guarded `initializeApp` did not create a second app and orphan `appcheck-boot`'s App Check instance. |
| M4 | Delete IndexedDB `firebaseLocalStorageDb` in DevTools **without** touching cookies, reload → must show « Reconnexion nécessaire », never « aucun facteur ». |
| M5 | Tamper with the rendered `athenaUid` → « Identité différente », every mutation refused. |
| M6 | Password change happy path, then **log out and log back in with the new password** — the only real proof. |
| M7 | Wrong current password → « Mot de passe actuel incorrect. » and **no SMS is sent** (a wasted SMS costs money and is an abuse vector). |
| M8 | Three wrong SMS codes then the right one → succeeds, **without** needing a resend. |
| M9 | Let a code expire → resend becomes immediately available → the resent code works. Confirm in the Network tab that a **new** reCAPTCHA token was minted. |
| **M10** | **Full number change:** unlock → add new → confirm the echo → code → **two factors visible** → log out → log in with the **NEW** number → return → remove the old → log out → log in again. Two complete login cycles. |
| M11 | Typo'd new number (a valid number you also control): the code never arrives, abandon → **the old factor is still enrolled and login still works.** |
| M12 | With one factor and `REQUIRE_MFA=true`: the Retirer control is disabled with a visible reason — then **re-enable it in DevTools and click it**; the JS function itself must refuse. |
| M13 | Six SMS in a few minutes → « Trop de demandes de code » and the cooldown **re-arms**. |
| M14 | Cloud Logging: one `pallas.auth` entry per event; no phone number, email or token in `jsonPayload`. |
| M15 | Zero CSP violations in the console; nothing arrives at `/csp-report`. |
| M16 | 375 px: the six code inputs fit and every touch target clears 44 px. |
| M17 | Profile: change the fax, then generate a budget PDF **and** a gabarit containing `{{cabinet.telecopieur}}` — both must show it. Confirm the accusé bordereau reads « Montréal (Québec) » and the phone still reads `(514) 737-2525` with no `+1 `. |
| M18 | `python -m scripts.check_config` still passes. |
| **M19** | **Look at the sidebar in a browser and confirm a cog, not the literal word « settings ».** No test can catch this: `tests/test_icons.py` compares templates against the Python set and never reads the font's glyph table. |

Get `FIREBASE_APP_ID` and the browser API key from **Project settings → Your
apps → Web app** (register one if none exists). Put the app id in `app.yaml`
and the API key in the `firebase-api-key` secret.

### 6.6 Firebase Storage

Initialize the default bucket (Firebase console → **Storage → Get started**).
Its name must equal `FIREBASE_STORAGE_BUCKET`.

**Then, and only then, deploy its rules** — the bucket has to exist first:

```bash
# From the repo root:
firebase deploy --only storage --project $PROJECT
```

The rules are deny-all; files are served only via 15-minute signed URLs, which
bypass rules because they are produced by the Admin SDK. Nothing in the
application reads Storage through a client SDK.

**Then give the bucket its ONE lifecycle rule: delete objects under
`staging/` after 7 days.** Every direct-to-GCS upload lands under
`staging/{uid}/…` first — the web upload form, a zip export, and (lot 2A) the
connector's upload tickets under `staging/{uid}/mcp/…` — and anything never
finalized stays there until this rule sweeps it. The connector's ticket
depends on it outright: a file PUT after its ticket's hour is never filed, and
this rule is what erases it. Check it (read-only — it changes nothing):

```bash
# The value of FIREBASE_STORAGE_BUCKET in athena/app.yaml.
FIREBASE_STORAGE_BUCKET=your-bucket-name
gcloud storage buckets describe "gs://${FIREBASE_STORAGE_BUCKET:?}" \
  --project=$PROJECT --format="json(lifecycle_config)"
```

The answer must hold a `Delete` rule with `"age": 7` and
`"matchesPrefix": ["staging/"]` — and **no other `Delete` rule that can reach
a live object**: this bucket holds the clients' documents under `users/`, so a
rule without a prefix, or on any other prefix, deletes them. (A rule limited to
NONCURRENT versions — `daysSinceNoncurrentTime`, `numNewerVersions`,
`isLive: false` — touches no live object and is fine.) If the staging rule is
missing, add it in the console (Cloud Storage → the bucket → *Lifecycle* →
*Add a rule*: delete, age 7 days, object name prefix `staging/`) rather than by
`gcloud storage buckets update --lifecycle-file`, which REPLACES the whole
configuration. `python -m scripts.provision --project=$PROJECT` reports it as
`cycle-de-vie-staging`, with the same two checks.

### 6.7 App Check + reCAPTCHA Enterprise (recommended)

```bash
gcloud recaptcha keys create --web --display-name=athena-appcheck \
  --domains=yourdomain.example --integration-type=score --project=$PROJECT
```

Register the Web App in **Firebase console → App Check** with the reCAPTCHA
Enterprise provider, and put the site key in `RECAPTCHA_ENTERPRISE_SITE_KEY`
(`app.yaml`). App Check is **fail-open** if unset — the app still runs, but logs
a loud warning in production and does not verify HTMX requests. For local dev,
register an App Check **debug token** and set `APPCHECK_DEBUG_TOKEN`.

### 6.8 Data residency — keep application logs in your region (optional)

After §6.2/§6.3, your **primary data stores are already regional**: App Engine,
Firestore, and (from §6.6) the Storage bucket all live in
`northamerica-northeast1` (Montréal), and those regions are **permanent**. Two
Google-managed telemetry sinks, however, default to **`global`**:

| Sink | Default location | Movable? |
|---|---|---|
| Cloud Logging `_Default` bucket (your `pallas-athena` logs) | `global` | ✅ redirect to a regional bucket (below) |
| Cloud Logging `_Required` bucket (Admin Activity / System Event audit logs) | `global` | ❌ **permanent** — Google-imposed |
| Cloud Trace | Google-managed (global) | ❌ no regional-storage control |

A log bucket's location is fixed at creation, so the existing `global` buckets
can't be *moved* — you create a **new regional bucket** and route future logs to
it. Application logs are PII-redacted at source (`RedactionFilter`), so this is a
residency-hygiene step, not a leak fix.

```bash
# 1. Create a Montréal log bucket:
gcloud logging buckets create athena-mtl \
  --location=northamerica-northeast1 --retention-days=30 --project=$PROJECT

# 2. Repoint the _Default sink at it (new logs land in Montréal):
gcloud logging sinks update _Default \
  logging.googleapis.com/projects/$PROJECT/locations/northamerica-northeast1/buckets/athena-mtl \
  --project=$PROJECT

# 3. Verify:
gcloud logging sinks describe _Default --project=$PROJECT
gcloud logging buckets list --project=$PROJECT
```

- Only logs written **after** the change go to Montréal; the old `global`
  `_Default` contents age out on their retention window.
- The `_Required` audit bucket stays `global` — it holds project-admin metadata
  (who called which API), never client data. There is no supported way to
  regionalize it on an existing project.
- Cloud Trace has no equivalent regional-bucket control; traces are
  PII-sanitized (`tracing_setup.py`) and carry IDs/counts only.

### 6.9 The `portail` service (client portal)

The second App Engine service (`athena/portail.yaml`, entrypoint
`client.wsgi:app`) runs as its own least-privilege service account,
`portail-svc`, and brings three resources of its own — a quarantine bucket,
the named Firestore database `portail` and the Cloud Tasks queue `portail` —
which the main service also uses: it writes the invitations, archives and
purges the uploads, and handles and re-enqueues the queue's tasks (its grants
are in step 5). **Steps 1–5 must exist before the first Cloud Build run**:
the trigger deploys `portail.yaml` second and fails if `portail-svc` does
not exist or the build's service account cannot act as it (§8). Steps 6
and 8 land with the Cloudflare edge (§7, §5 step 12), and step 10 runs once
the first CI build has deployed both services and the host resolves. The
two APIs it adds (`cloudtasks`, `cloudscheduler`) are already in §6.1's
block. `python -m scripts.provision --project=$PROJECT` reports each
resource below by its id — `sa-portail`, `firestore-portail`,
`seau-quarantaine`, `cycle-de-vie-quarantaine`, `file-portail`,
`pare-feu-interne` — and writes nothing.

1. **Service account:**

   ```bash
   gcloud iam service-accounts create portail-svc --project=$PROJECT
   ```

2. **Named database**, in the App Engine region. Its location is permanent
   (§5):

   ```bash
   gcloud firestore databases create --database=portail \
     --location=northamerica-northeast1 --type=firestore-native --project=$PROJECT
   ```

3. **Queue**, then the limits `scripts.provision` checks:

   ```bash
   gcloud tasks queues create portail --location=northamerica-northeast1 --project=$PROJECT
   gcloud tasks queues update portail --location=northamerica-northeast1 --project=$PROJECT \
     --max-attempts=10 --min-backoff=10s --max-backoff=600s \
     --max-concurrent-dispatches=3 --max-dispatches-per-second=5
   ```

4. **Quarantine bucket** `<project>-portail-quarantaine` (`PORTAIL_BUCKET`,
   §4.1): in the App Engine region (any other region bills egress on every
   uploaded byte), uniform bucket-level access on, public access prevention
   enforced. Give it two lifecycle rules, in the console as in §6.6: delete
   objects under `submissions/` after 90 days and under `archive/` after 365
   — the values of `QUARANTINE_LIFECYCLE` in
   [`athena/utils/deployment_inventory.py`](athena/utils/deployment_inventory.py).
   Without them a client's privileged files stay in quarantine for ever.

5. **IAM.** Exactly these grants. The portal's are also listed in
   `portail.yaml`'s header; it gets nothing else — no Graph, no Firestore
   write, no bucket read.

   | Principal | Role | On |
   |---|---|---|
   | `portail-svc` | `roles/storage.objectCreator` | the quarantine bucket |
   | `portail-svc` | `roles/datastore.viewer`, conditioned on `resource.name.startsWith("projects/<project>/databases/portail")` | the project |
   | `portail-svc` | `roles/cloudtasks.enqueuer` | the `portail` queue |
   | `portail-svc` | `roles/appengine.appViewer` | the project |
   | `portail-svc` | `roles/logging.logWriter`, `roles/cloudtrace.agent` | the project |
   | `portail-svc` | `roles/secretmanager.secretAccessor` | `portail-secret-key` and `firebase-api-key`; `cf-origin-secret` only after the edge proof (§6.4) |
   | App Engine default SA | `roles/storage.objectAdmin` | the quarantine bucket |
   | App Engine default SA | `roles/cloudtasks.enqueuer` (the reconciliation cron re-enqueues) | the `portail` queue |
   | the SA your Cloud Build trigger runs as | `roles/iam.serviceAccountUser` | `portail-svc` |

   ⚠ **`roles/appengine.appViewer` is not optional.** A task of type
   `app_engine_http_request` needs `appengine.applications.get`, which
   `cloudtasks.enqueuer` does not carry. Without it every direct enqueue fails
   `PERMISSION_DENIED`, the client still gets the confirmation (the envelope
   is durable), and the lot reaches Réception only through the 15-minute
   reconciliation — so the failure reads as slowness, not as an error. The
   main service enqueues too and needs the same permission; on the original
   deployment its Editor role carries it.

6. **Firewall:** the `0.1.0.2/32` allow of §7 also carries this queue's
   dispatches. Without it no task reaches the main service.

7. **Firebase Auth and App Check:** enable the **email link** sign-in
   provider and add the portal host to the authorized domains (§17
   `firebase-auth`); add the portal host to the reCAPTCHA Enterprise key (§17
   `app-check`).

8. **Host and edge:** map `portail.<your-domain>` as an App Engine custom
   domain (`dispatch.yaml` routes it to the service), with a proxied CNAME in
   Cloudflare under Full (Strict). Put **no** Cloudflare Access application on
   it — clients reach it publicly — and add a rate-limiting rule of about 120
   requests per minute per IP. The zone-wide Transform Rule of §7 already
   covers the host.

9. **Outbound email (Microsoft Graph):** an Entra app registration with the
   `Mail.Send` application permission and admin consent — plus
   `Calendars.ReadWrite` for the Bookings sync and the Outlook mirror —, its
   secret in `graph-client-secret` and its ids in the `GRAPH_*` variables
   (§4.1, §17 `entra`). ⚠ An application permission granted in Entra is
   **tenant-wide**: it reaches every mailbox of the organisation. Exchange's
   RBAC for Applications narrows a permission to chosen mailboxes only when
   Entra does not also grant it — the two ADD UP, and the scope is decorative
   while the Entra grant stands (the legacy `ApplicationAccessPolicy` works
   per application, not per permission). A permission change takes 30 minutes
   to 2 hours to leave the Exchange cache (`Test-ServicePrincipalAuthorization`
   verifies it), and Exchange PowerShell accepts only certificate
   authentication for app-only access, so this administration stays manual.

10. **End-to-end test** with an **alias** of yours — never
    `AUTHORIZED_USER_EMAIL`, which the portal refuses to invite: invite it
    from Réception, follow the link, upload a file, and check the
    acknowledgment email and its copy in the sender's « Éléments envoyés ».
    Then the outage drill: pause the queue
    (`gcloud tasks queues pause portail --location=northamerica-northeast1 --project=$PROJECT`),
    submit again — the client still gets the confirmation — then `resume` it
    and watch the lot reach Réception.

### 6.10 Alertes de sécurité (Cloud Monitoring)

Security events of [`athena/OBSERVABILITY.md`](athena/OBSERVABILITY.md)
(`log_security_event`) feed **log-based counter metrics**, and every counter
an **alert policy** that emails you, except where the table below says why
not. They are the only signal of an
edge control that has switched itself off, or of someone probing the edge:
none of it shows on any page — which is how `cf-origin-secret` came to be
missing for months with nothing signalling it. Create them **once, after the
edge is armed** (§5 step 14): while `cf-origin-secret` or the App Check key
is unset, `security_control_disabled` fires on every fresh instance, which is
the normal state *during* provisioning.

**The files are the reference.** Each counter and each policy is one JSON
file under [`monitoring/`](monitoring/), at the repo root:
`monitoring/metrics/<counter>.json`, and `monitoring/policies/<counter>.json`
for the policy that watches it. Every command below reads them — run it from
the repo root. They were read back from the original deployment and found
equal to it when they were written (November 2026), and they stay equal only
if every change goes through them: the policies' French documentation, the
text of the email, lives only there. To change a counter or a policy, change
its file, then update the live resource FROM it (step 5); never re-create
it, since `create` does not deduplicate. `scripts.provision` compares each
counter's filter with its file; for a policy it checks only that ONE watches
the counter, enabled, with a channel — nothing compares a policy's condition
or text with its file.

⚠ **Run these blocks in bash** (Git Bash on Windows, Cloud Shell), like
every block of this document — never in PowerShell. The guards below
(`${CHANNEL:?…}`) are bash: PowerShell reads `"${CHANNEL:?…}"` as a variable
that does not exist and passes an EMPTY value without an error, even with
`$CHANNEL` set — and `policies create` then makes a policy that emails no
one.

`SECURITY_METRICS` in
[`athena/utils/deployment_inventory.py`](athena/utils/deployment_inventory.py)
names them, and `athena/tests/test_monitoring_definitions.py` ties each file
to the code: the application's log name, the project, events that are
declared AND emitted, a policy that fires on one event. The table and the
commands below are GENERATED from `SECURITY_METRICS` and pinned by a test.
`python -m scripts.provision --project=$PROJECT` reports each one by its id
(the table's last column) and writes nothing.

⚠ **The files name the original deployment's project** — in each counter's
filter (`logName="projects/…/logs/pallas-athena"`) and in the text of the
appspot policy. An adopter replaces it first (§4.3 lists the files;
`python -m scripts.check_config` names the value): a counter created with
another project's `logName` counts nothing, silently.

| Counter | Counts `jsonPayload.event` | Alert policy | Id |
|---|---|---|---|
| `security_redirect_rejected` | `redirect_rejected` | « Sécurité — redirection refusée » | `metrique-redirection-refusee`, `alerte-redirection-refusee` |
| `security_control_disabled` | `appcheck_disabled` OR `origin_secret_disabled` | « Sécurité — une protection est désactivée » | `metrique-controle-desactive`, `alerte-controle-desactive` |
| `security_appspot_blocked` | `appspot_blocked` | « Sécurité — accès direct à appspot.com » | `metrique-appspot-bloque`, `alerte-appspot-bloque` |
| `security_request_too_large` | `request_too_large` | none yet — son volume est inconnu : un robot peut poster de gros corps, une ligne par tentative. Mesurer d'abord, puis choisir un seuil | `metrique-requete-trop-volumineuse` |

Each counter filters the application's log, `pallas-athena`, which **both
services share**: it counts the portal's lines too, and
`resource.labels.module_id` (`default`, `portail`) tells them apart. It is
project-level (no `--bucket-name`), so §6.8's regional log bucket changes
nothing, and it counts only the lines written after its creation.

**Why `request_too_large` has no alert yet.** Its volume is unknown: the
event is new (2026-11-08), it logs ONE line per refused request with no cap,
and anyone on the internet can trigger it by posting a large body, so an
alert on « more than zero » could email you for every bot. The counter is
there to measure it: after a week, read it in Metrics Explorer
(`logging.googleapis.com/user/security_request_too_large`) or with
`gcloud logging read 'jsonPayload.event="request_too_large"' --project=$PROJECT --freshness=7d`,
then choose a threshold — or none — in a new policy file.

Every policy is « **count > 0 over 5 minutes** »: the counts summed over
300-second windows (`ALIGN_SUM`), then across every series (`REDUCE_SUM` —
both services, every version), compared `COMPARISON_GT 0`. It emails your
existing notification channel, closes itself after one day without a new
line, and its French documentation — in the email — says what a firing
means and what to do; « Reading an alert », below, says more.

**1. The counters — one-time, from the repo root.** Re-running a `create`
answers `ALREADY_EXISTS` and changes nothing.

```bash
gcloud logging metrics create security_redirect_rejected --config-from-file=monitoring/metrics/security_redirect_rejected.json --project=$PROJECT
gcloud logging metrics create security_control_disabled --config-from-file=monitoring/metrics/security_control_disabled.json --project=$PROJECT
gcloud logging metrics create security_appspot_blocked --config-from-file=monitoring/metrics/security_appspot_blocked.json --project=$PROJECT
gcloud logging metrics create security_request_too_large --config-from-file=monitoring/metrics/security_request_too_large.json --project=$PROJECT
```

Then **wait a few minutes**: a policy is refused on a counter whose metric
descriptor does not exist yet.

**2. Your notification channel — read-only, any time.** The policies email
the channel you already have (console: Monitoring → Alerting → Edit
notification channels; no channel yet? add an Email channel on that page
first). List its id with the command below — the channels group exists only
under `beta` and `alpha` in the Cloud SDK — or read it off an existing
security policy: the third column of step 4's `policies list`. `CHANNEL`
lives in this terminal only: set it again in a new one, before step 3 or 5.

```bash
gcloud beta monitoring channels list --project=$PROJECT --format="value(name,type,displayName)"
CHANNEL=projects/$PROJECT/notificationChannels/YOUR_CHANNEL_ID
```

**3. The policies — one-time, from the repo root**, once step 1's
descriptors exist. The channel is passed here, never written in the file (the
repository is public), and guarded: `${CHANNEL:?…}` makes the shell refuse
the command when `CHANNEL` is unset or empty, because gcloud would take an
empty `--notification-channels=` as « no channel », without an error, and
create a policy that emails no one. ⚠ **`policies create` does not
deduplicate**: running it twice makes two policies and two emails per firing,
which `scripts.provision` reports as drift — list first (step 4) before you
re-run anything.

```bash
gcloud monitoring policies create --policy-from-file=monitoring/policies/security_redirect_rejected.json --notification-channels="${CHANNEL:?step 2 sets CHANNEL}" --project=$PROJECT
gcloud monitoring policies create --policy-from-file=monitoring/policies/security_control_disabled.json --notification-channels="${CHANNEL:?step 2 sets CHANNEL}" --project=$PROJECT
gcloud monitoring policies create --policy-from-file=monitoring/policies/security_appspot_blocked.json --notification-channels="${CHANNEL:?step 2 sets CHANNEL}" --project=$PROJECT
```

**4. Verify — read-only, any time.** These are the commands
`scripts.provision` runs: each counter must exist and print, after its name,
its file's filter with YOUR project in its `logName` — a counter that names
another project's log counts nothing — and no third column: `True` there
means the counter is DISABLED and counts nothing (step 5 says how to enable
it). Each policy must come back ONCE, enabled (`True`), with your channel.

```bash
gcloud logging metrics describe security_redirect_rejected --project=$PROJECT --format="value(name,filter,disabled)"
gcloud logging metrics describe security_control_disabled --project=$PROJECT --format="value(name,filter,disabled)"
gcloud logging metrics describe security_appspot_blocked --project=$PROJECT --format="value(name,filter,disabled)"
gcloud logging metrics describe security_request_too_large --project=$PROJECT --format="value(name,filter,disabled)"
gcloud monitoring policies list --project=$PROJECT --filter="conditions.conditionThreshold.filter~security_redirect_rejected" --format="value(name,enabled,notificationChannels)"
gcloud monitoring policies list --project=$PROJECT --filter="conditions.conditionThreshold.filter~security_control_disabled" --format="value(name,enabled,notificationChannels)"
gcloud monitoring policies list --project=$PROJECT --filter="conditions.conditionThreshold.filter~security_appspot_blocked" --format="value(name,enabled,notificationChannels)"
```

The policies restrict `resource.type="gae_app"`, the monitored resource
`CloudLoggingHandler` detects on App Engine. Check it once: it must print
`gae_app`. It looks back thirty days, because the default single day prints
nothing on a day without a line — which would read as a wrong type.

```bash
gcloud logging read "logName=\"projects/$PROJECT/logs/pallas-athena\"" --project=$PROJECT --freshness=30d --limit=1 --format="value(resource.type)"
```

To see one alert end to end without waiting for an attack, open
`https://<your-host>/audiences/new?return_to=https://example.com` in your
signed-in browser: the hearing form opens normally and writes nothing, the
refused `return_to` logs one `redirect_rejected`, and the email of
« Sécurité — redirection refusée » arrives within about ten minutes (the
counter's own delay plus the 5-minute window). The policy then closes by
itself a day later.

**5. Change a counter or a policy — from its file, never by hand.** Edit the
file, then update the live resource from it, from the repo root. A counter:
`update` changes only the fields the file names.

```bash
M=security_redirect_rejected   # the counter whose file you changed
gcloud logging metrics update $M --config-from-file=monitoring/metrics/$M.json --project=$PROJECT
```

A counter step 4 shows disabled: its file does not carry that field, so the
update above leaves it as it is. Enable it with a one-field file:

```bash
M=security_redirect_rejected   # the disabled counter
printf '{"disabled": false}\n' > enable-metric.json
gcloud logging metrics update $M --config-from-file=enable-metric.json --project=$PROJECT
rm enable-metric.json
```

A policy: `M` names the counter it watches (its file's name); `CHANNEL` is
step 2's, set again in a new terminal — unset, the guard stops the command.

```bash
M=security_redirect_rejected   # the counter the policy watches
POLICY=$(gcloud monitoring policies list --project=$PROJECT --filter="conditions.conditionThreshold.filter~$M" --format="value(name)")
echo "$POLICY"   # exactly ONE name; two lines are a duplicate: gcloud then refuses the update (Invalid value) — see below
gcloud monitoring policies update "${POLICY:?no policy found}" --policy-from-file=monitoring/policies/$M.json --set-notification-channels="${CHANNEL:?step 2 sets CHANNEL}" --project=$PROJECT
```

Two names are a duplicate from a second `create`: delete the one that does
not carry your channel (any one if both do) with
`gcloud monitoring policies delete <that name> --project=$PROJECT`, then run
the block again.

⚠ **Sans `--set-notification-channels` — ou avec un `CHANNEL` vide, que le
garde `${CHANNEL:?…}` refuse —, cette mise à jour remplace toute la
politique par le fichier, qui ne nomme aucun canal : l'alerte reste activée,
mais ne prévient plus personne.** (Without `--fields`, gcloud replaces the
policy with `--policy-from-file`.) Step 4 then shows an empty third column,
and `scripts.provision` reports « aucun canal de notification ».

**Reading an alert.** The email's text is the policy's documentation; the
log entry says more. In Logs Explorer, filter
`jsonPayload.event="<the event>"`: the entry carries the event's own fields —
ids and codes only (OBSERVABILITY.md) — and, like every line logged inside a
request, `jsonPayload.route` (the raw path when no route matched) and
`httpRequest` (`requestUrl` without its query, `userAgent`).

- « Sécurité — redirection refusée » — read `reason`, the time and the
  route. Every caller sits behind your sign-in, so a line means your
  signed-in browser opened a crafted link, or an Athena page built a bad
  target. If, at that time, you had opened a link that came from elsewhere
  (an email, a message, another site), treat it as phishing: do not follow
  that link again, and do not sign in from it. Otherwise the route is an
  Athena page you opened normally: a defect to fix. Step 4's drill makes one
  on purpose.
- « Sécurité — une protection est désactivée » — read `reason`,
  `error_type` and `resource.labels.module_id` (`default`, `portail`). The
  event is logged once per process, at the first request that reaches the
  control: up to two lines per instance, and again whenever a process
  restarts. On the portal, `reason` separates an EMPTY value
  (`cf_origin_secret_unset`) from a read that FAILED
  (`cf_origin_secret_unreadable`, the error's class in `error_type`): a
  secret that does not exist (`NotFound`), a disabled version
  (`FailedPrecondition`) and a missing grant (`PermissionDenied`) are all
  failed reads. The main service logs every case as
  `cf_origin_secret_unset`.
  - `origin_secret_disabled` — the secret first: the FIRST row of
    `gcloud secrets versions list cf-origin-secret --project=$PROJECT`, the
    newest (`latest`, the one both services read), must be `enabled`;
    `python -m scripts.check_config --prod` (from `athena/`) reads `latest`
    as they do, with your access. **Disabled**: enable it again
    (`gcloud secrets versions enable <its number> --secret=cf-origin-secret --project=$PROJECT`)
    — it normally carries the value the Transform Rule already sends; unless
    it was disabled to undo a new value the rule never received: then
    enabling it is finishing that rotation (its value in the Transform Rule,
    then `default` AND `portail` redeployed, §6.4's rotation note), which is
    also what a 403 on the confirming page below means.
    **Destroyed, or the secret deleted**: that is a ROTATION — a new version
    of the secret, the same value in the Transform Rule, then `default` AND
    `portail` redeployed (§6.4's rotation note; a deleted secret first needs
    §6.4's container and grants again). Never a new value without the rule:
    every request through Cloudflare would answer 403. **Enabled, and still
    failing**: its access — the portal's is the commented grant in §6.4,
    given only once the tracer shows the rule firing on the portal host
    (§5 step 13).
  - `appcheck_disabled` — the setting: `RECAPTCHA_ENTERPRISE_SITE_KEY` in
    the service's yaml (§4).

  Then redeploy each service the lines name (`module_id`) — after a change
  to the secret itself, both, since both read it: a worker reads its secrets
  when it starts. To confirm, open a page of the NEW version through its
  domain — it must DISPLAY: a 403 means the value differs from the Transform
  Rule — then search the event under that service's
  `resource.labels.module_id` AND the new `resource.labels.version_id`: no
  line means it is fixed. A cron or a task cannot confirm it: they skip the
  comparison with the header (on `portail`, the whole check); nor can a
  line from an older version. The portal's App Check speaks only on a
  client's POST, so for a `portail` `appcheck_disabled` check its key in
  `portail.yaml` instead. While the incident stays open (it closes after a
  day without a line), a new line sends no new email. Paramètres →
  Configuration sees only the main service, and `check_config --prod` only
  your own access and your `.env`.
- « Sécurité — accès direct à appspot.com » — read `service` (`default`,
  `portail`) and `endpoint`. The firewall admits every Cloudflare range (§7),
  so anyone's Cloudflare zone can relay a request to an `*.appspot.com` host:
  the refusal is the tripwire.
  - `service=portail`: the portal tests the host **before** its origin
    secret, so a request relayed by another zone ends there — the tripwire
    working. Check that the firewall rules are unchanged
    (`gcloud app firewall-rules list --project=$PROJECT`, §7).
  - `service=default`: with the origin secret armed, the main service checks
    the secret **before** the host, and refuses a request without it with no
    line at all. A `default` line therefore comes from a request that
    **carried the secret**: either the secret leaked, or your own zone routes
    to the appspot host. Check your zone's origin rules, then rotate
    `cf-origin-secret` (§6.4: its rotation note — the three steps chained,
    the 403 windows they open, `default` AND `portail` redeployed by hand;
    §7 step 4 for the Transform Rule that carries the value). The one
    exception: a process of the same `default` version logged
    `origin_secret_disabled` (« Sécurité — une protection est désactivée »)
    — its disarmed check lets any relayed request through to the refusal;
    follow that alert instead, no rotation. An `appcheck_disabled` does not
    count: App Check runs after the origin check.

  To confirm, search `appspot_blocked` lines from `default` after the
  rotation: one more means the route to the appspot host is still there —
  your own zone's included, since the Transform Rule is zone-wide and
  carries the new value too. While the incident stays open (it closes after
  a day without a line), a new line sends no new email.

The internal chat's alert « Clavardage — charte en repli » and its counter
`chat_charter_repli`, which could no longer fire once the chat left (§11b),
were deleted from the original deployment on 2026-11-08; a deployment that
never ran the chat has neither.

---

## 7. Cloudflare edge

All traffic must reach App Engine **through** Cloudflare; the app actively
rejects direct access. Set up, in order:

1. **DNS:** add your zone in Cloudflare, then a **proxied** (orange-cloud)
   record for `yourdomain.example` pointing at your App Engine custom-domain
   target (map the domain first under App Engine → Settings → Custom domains).
2. **SSL/TLS:** set the mode to **Full (Strict)** and install an **Origin
   Certificate** on App Engine's custom domain.
3. **App Engine firewall:** allow **Cloudflare's published IP ranges**, and
   **`0.1.0.2/32` at a higher priority**, then set the `default` rule to DENY —
   in that order. ⚠ This step said « Cloudflare ranges **only** » until
   2026-09-12, and following it literally **kills all three cron jobs and the
   Cloud Tasks queue**: App Engine dispatches them from the internal address
   `0.1.0.2`, which is not a Cloudflare range. The live deployment carries 24
   rules — `0.1.0.2/32` at priority 10, 22 Cloudflare ranges at 100–360, and
   the implicit `default` DENY (read 2026-09-12). Set the deny LAST: flipping
   it before the allows locks you out of your own origin. Cloudflare publishes
   new ranges over time and this repository carries no copy of the list — take
   it from `cloudflare.com/ips` at provisioning time, and never delete a range
   you do not recognise, since it may be your own office IP.
4. **Origin secret (Transform Rule):** add a request Transform Rule that injects
   header `X-Origin-Auth: <cf-origin-secret value>` on every request, zone-wide.
   The app checks it (`security.py`) when `CF_ORIGIN_SECRET` is set. This is the
   second layer that defeats a spoofed-Host direct hit.
   ⚠ **Do this while the secret is still an EMPTY container, and prove the rule
   fires before giving it a value** — Cloudflare's request tracer
   (`POST /accounts/{id}/request-tracer/trace`), never inference. The reverse
   order 403s the entire zone with no deploy to explain it; see §5 step 13.
   That order is for the FIRST provisioning. On a rotation, follow §6.4's
   rotation note instead: a new version of the secret, then this rule, then
   both services redeployed.
5. **Rocket Loader:** leave it **off** — the original enabled it, then disabled
   it at the edge on 2026-07-11, and it is not returning. ⚠️ The end-of-`<body>`
   script order in `base.html` is still load-bearing: the App Check boot runs synchronously and htmx/Alpine
   are `defer`, but all resolve in document order — don't reorder them.
6. **Early Hints:** enable it (the app emits the `Link` preload headers).
7. Point `MCP_CANONICAL_ORIGIN` and `AUTHORIZED_USER_EMAIL`'s domain at
   `yourdomain.example`, and update the legal pages / README / SECURITY.
8. **TLS and HSTS at the edge:** set the minimum TLS version to 1.3, and give
   the edge's HSTS the same max-age as the origin's (two years, set in
   `security.py`) so the two never disagree. The portal host also gets its
   rate-limiting rule (§6.9).

---

## 8. First deploy + smoke test

**Deploy** either manually, or by connecting a Cloud Build trigger on push to
`main`.

```bash
cd athena
gcloud app deploy app.yaml --project=$PROJECT
```

⚠ **The trigger does not do one deploy, it does four**, and this section said
one until 2026-09-12. `cloudbuild.yaml` runs the pytest gate, then deploys
`app.yaml`, `portail.yaml`, `dispatch.yaml` and `cron.yaml` in that order, then
prunes old versions **per service**. Three consequences for a first build:

- **Step 2b fails if `portail-svc` does not exist.** The portal's service
  account and its IAM must be provisioned before the trigger is ever fired
  (§6.9).
- **Step 2d needs `cloudscheduler.googleapis.com`** (§6.1). Without it the
  build fails `SERVICE_DISABLED` *after* three services are already deployed.
- **`dispatch.yaml` and `cron.yaml` each REPLACE their whole table.** The two
  files at the repo root are the single complete source; a partial file
  silently removes the rules or jobs it omits.

Deploying `app.yaml` by hand, as above, is the safe way to get the first
service up before any of that exists.

**Smoke test:**
- Visit `https://yourdomain.example` — the login page loads through Cloudflare.
- `gcloud app browse` (direct `*.appspot.com`) should be **403** — proof the
  edge defenses work.
- Log in as the single user; complete Phone MFA.
- Create a dossier, upload a document (verifies signed URLs / the signBlob role
  from §6.4), and confirm it downloads.
- Check **Cloud Logging** for the log name `pallas-athena`.

---

## 9. Seed Québec reference data

Populates courthouse (`ref_greffes`) and tribunal (`ref_juridictions`)
collections used by court-file-number parsing. Idempotent.

```bash
cd athena
# Needs Application Default Credentials — this script does NOT read .env:
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
python -m scripts.seed_reference_data
```

> If you are adapting to another jurisdiction (§13), edit the data in
> `scripts/seed_reference_data.py` first.

---

## 10. Optional — DavX5 calendar/contacts sync

Skip this if you don't need Android sync. If you keep it:
- In DavX5, add the account by URL (`https://yourdomain.example/`), with HTTP
  Basic credentials: username = `AUTHORIZED_USER_EMAIL`, password = the plaintext
  whose bcrypt hash is in `dav-password-hash`.
- `/dav/*` is defended by the App Engine firewall, the origin-secret check, the
  bcrypt Basic auth and a per-IP brute-force brake. **Do not put Cloudflare
  Access in front of it** without reading this first: DavX5 has no
  custom-header setting, so a service token cannot be presented (verified on
  device, 2026-08-11). Its only mTLS-capable path is a *client certificate*
  (Advanced login → select a certificate from the Android KeyChain) — and
  Cloudflare enables mTLS **per hostname, not per path**, so switching it on
  for a host that also serves the web UI and the MCP endpoint affects every
  client of that host. If you want Access on DAV, give DAV its own hostname.

---

## 11. Optional — MCP connector for Claude

There is no switch to turn it off: `MCP_ENABLED`, `MCP_WRITE_ENABLED` and
`MCP_COMPTABILITE_ENABLED` were removed on 2026-10-05 (the lawyer's
decision), so the `/mcp` and `/oauth/*` routes are always served. Never
added in claude.ai, the connector reaches nothing: no token exists until you
complete its consent screen yourself — behind your login and MFA —, and
`/mcp` answers `401` to any call without one. To cut off a connector you did
add, revoke its tokens:
`python -m scripts.revoke_mcp_tokens` — a write re-reads its token
(`bearer.revalidate_for_write`) and stops at once, a read stops within the
bearer cache's five minutes, and Claude must then be authorized again. If
you keep it:

```bash
gcloud firestore fields ttls update expire_at --collection-group=oauth_codes  --enable-ttl --project=$PROJECT
gcloud firestore fields ttls update expire_at --collection-group=oauth_tokens --enable-ttl --project=$PROJECT
```

(TTL is garbage collection only — expiry is enforced in code regardless.)

- If you use Cloudflare Access at all, ensure it **does NOT cover** `/mcp`, `/oauth/*`, or
  `/.well-known/oauth*` (only `/dav/*`).
- Add a Cloudflare **Configuration Rule** disabling Browser Integrity Check on
  those paths (Claude's server is not a browser); watch Security → Events for
  Super Bot Fight Mode challenging Anthropic's egress.
- In claude.ai: **Settings → Connectors → Add custom connector →**
  `https://yourdomain.example/mcp`, then complete Firebase login + MFA on the
  consent screen and click **« Autoriser »**.
- **Read the consent screen before ticking.** It is short since 2026-11-10
  (your request: « cut to the chase »): what is read, in four lines; then
  the write box, one line per write family — the accounting registers'
  among them —, and one short clause per thing the connector can NEVER do.
  The write lines and the « jamais » clauses are assembled from
  `athena/mcp/disclosure.py`, the same registry INSTRUCTIONS come from, and
  each « never » is backed by a sweep of the connector's code; the detail
  of each tool's rules is in its description, which Claude reads.
  `tests/test_mcp_oauth.py` holds the screen to a word budget. ONE box,
  « Autoriser aussi les écritures », grants every write tool, and
  it is always offered: no switch hides it any more. The scope is frozen
  when you click « Autoriser »: a later release that changes what a write
  tool can do (a new family, a lifted « never », or a behaviour change such
  as lot 0a's `complete_task` refusing to reopen a closed task) reaches the
  token you already hold **silently**. So for such a release:
  `python -m scripts.revoke_mcp_tokens` and remove the connector in
  claude.ai BEFORE pushing, deploy, then re-add it and tick the box under
  the new text. **That manual revoke → deploy →
  re-consent sequence is the ONLY control that keeps a token granted before
  the MCP write-expansion program (lots 0a to 5) from reaching its new write
  tools**: there is no code gate — no version stamped on a token, no check
  at `tools/call` that a grant predates the tool it calls — by the lawyer's
  decision D19 (2026-09-29). Skip it, and every new `athena:write` tool is
  live for the token in force the moment the deploy lands. §15
  « Déploiement unique (D22) », step 5, is where this release runs it — and
  the bulk creators' release (2026-09-30) runs it again, §15 « Bulk
  creators », step 2. The accounting tools were the one exception by
  construction until 2026-10-05: they needed a scope of their own,
  `athena:comptabilite`, behind its own box and switch. The lawyer removed
  all three that day and, his explicit choice the same day, SKIPPED this
  sequence for that release: the token in force, `athena:read
  athena:write`, had been granted under a screen that said the connector
  would NEVER write to the trust register or the administration ledger,
  nor record a payment — and from that deploy on it reaches the accounting
  writes, with no re-consent (§15 « No MCP switch (2026-10-05) »). Running
  this sequence at any time puts the grant back in step with the screen.
  No switch is left to keep on, or to arm, around a re-consent: the write
  box is the only thing to tick.
- **The upload ticket needs an organisation setting of claude.ai** (lot 2A,
  plan D4): `begin_upload` hands Claude a WRITE-only upload link whose
  ticket files bytes for one hour only (the GCS session itself outlives it;
  a late PUT is never filed and the `staging/` rule sweeps it), and only the
  code sandbox can PUT the bytes to it — so the claude.ai
  organisation must allow code-execution network egress to
  `storage.googleapis.com` (§15 « Lot 2A », step 7). Without it the tickets
  open, every PUT fails, and they expire unused: nothing is ever filed.
  Since 2026-11-10 a ticket also brings an administration entry's pièce
  justificative (purpose `piece_justificative`): it attaches one, or
  REPLACES the one the entry carries — the replaced file KEPT, never
  deleted, unlike the web's « Remplacer » — and a dépense of a dossier
  gets its copy in « Mandat › Déboursés », as on the web (§15 « Pièces
  justificatives through the connector »).
- **An invoice the connector issues consumes a REAL number** (lot 3b,
  plan D3): `create_invoice` takes the year's next `YYYY-F###` inside the
  invoice's own transaction — a refusal consumes none, a success one for
  ever, voided or not. Never smoke-test it: §15 « Lot 3 », step 7, verifies
  a deploy with `preview_invoice` and no-op calls, and says how to check
  that `counters/invoices-{year}.seq` advances by exactly one per invoice
  really issued.
- **A dossier status set through the connector moves your PHONE, and a
  compliance check it writes is only PRESUMED** (lot 4b, plan D6 and D7):
  `set_dossier_status` runs the same DavX5 drain as the dossier form's save
  — closing or archiving takes the dossier's tasks, notes and events off
  the phone; when that drain does not finish the result says so, and asking
  the same status again (or « Resynchroniser le téléphone » on the dossier's
  page) repairs it. `record_kyc_status` stores an identity or conflict check
  as « inscrit par Claude — à confirmer »: it counts as NOT done — the
  coverage report keeps it open — until you click « Confirmer » on the
  contact's fiche, and it is refused over a check you decided yourself.
  §15 « Lot 4 » verifies both on test data.
- **The accounting registers are written under the same box as every
  other write**, « Autoriser aussi les écritures » — the ACCOUNTING family
  (`record_trust_entry`, `record_admin_entry`, `update_admin_entry`,
  `clear_register_entries`, `reverse_register_entry`); their reads,
  `get_admin_ledger` and — since 2026-11-10 — `get_admin_receipt` (an
  entry's pièce justificative: a PDF's or an email's text, a photo as
  the image itself), are ordinary reads like the trust reads. Until
  2026-10-05 they had a scope of their own, `athena:comptabilite`, a second
  box « Autoriser la comptabilité » and a switch that defaulted to off; the
  lawyer removed all three that day. The
  write block's ACCOUNTING line names what they do — inscribing,
  correcting, clearing, reversing —, and the « jamais » list every write
  shares names, in a few words each (since 2026-11-10), what they never do;
  in full, as INSTRUCTIONS and the tools' descriptions state it (a fee
  payment, an encaissement: each RECORDS A PAYMENT on the invoice): delete
  an entry (a trust entry is corrected only
  by a reversal, an administration entry while it stays editable, then by
  a reversal), start, complete or abandon a reconciliation, attach,
  replace or remove its bank statement, create or modify an account,
  transfer funds between dossiers
  (or reverse one leg of such a transfer), withdraw trust funds in cash,
  back a fee payment with a paper invoice, an invoice not yet sent, one
  that imputes a provision, another client's invoice or — in a dossier of
  several clients — one naming none (D21), make a fee payment to anyone but
  you or your firm as « Paramètres » names you (D23), show a bank number.
  (Attaching a receipt left that list on 2026-11-10: the connector now
  attaches or replaces one, the replaced file kept. A reconciliation's
  bank statement joined it the same day: only the lawyer attaches it.) A write token lists
  them the moment it is granted — **90** tools (34 read, 56 write) since
  2026-11-10 —, a read-only one the **34** reads, `get_admin_ledger` and
  `get_admin_receipt` among them. Before the first real bank movement
  recorded through the
  connector, §15 « Lot 5 », steps 6 and 7, are the recipe for a supervised
  pilot on a TEST administration account — never the trust register.
- **Run no MCP write during a deploy window — and none after a rollback
  past the idempotency claim** (plan lot 0a: `mcp/write_support.py` with its
  `pending` status). Since that release a write CLAIMS its `idempotency_key`
  before it executes: the `mcp_idempotency` entry is created `pending`, then
  finalized `committed` (with the result) or `partial` (the write committed,
  a later step failed — the caller was told « NE PAS RÉESSAYER »). Code from
  before that release knows neither state: it reads a `pending` or `partial`
  entry as « no stored result », executes the write again, and overwrites
  the entry. So while two versions serve traffic side by side (a deploy in
  progress, a traffic split) or after migrating traffic back to an older
  version, a same-key retry that the new code would refuse or re-report can
  be DUPLICATED by the old one. The window is short and needs a retry to
  land on the other version, but a scheduled Claude job retries by design:
  pause those jobs for the deploy, and after a rollback past this release let
  the 24 h `expire_at` run out (or revoke the connector) before writing
  again. Nothing to migrate in the other direction — the new code replays a
  legacy entry (no `status`) unchanged.

---

## 11b. Retiré — Assistant IA interne (Vertex, 2026-08-26 → 2026-09-02)

Cette section décrivait la mise en service d'un client de conversation
Claude/Gemini sur **Vertex AI** sous le projet du cabinet, sur un TROISIÈME
service App Engine, avec sa file Cloud Tasks `chat-turns`, son entrée cron,
ses deux secrets de Workers juridiques et son `roles/aiplatform.user`.

**Il a été retiré le 2026-09-02** : le cabinet est passé à un compte **Claude
for Work couvert par une entente de traitement des données**, ce qui retire à
ce clavardage sa seule raison d'être (que le matériel privilégié ne transite
pas par un produit grand public). Le service, la file, l'entrée cron et les
données ont été supprimés ; il n'y a plus rien à provisionner ici.

**Ce qui SURVIT de cette section, et qu'il faut garder :** le **Firestore
Data Access audit logging** activé à son étape 1. Il était le contrôle
compensatoire du registre ; il est désormais la trace de son effacement, et
il ne coûte rien. Ne pas le désactiver.

L'analyse documentaire, elle, ne dépend plus de rien ici : la compétence se
colle dans un Skill claude.ai, et les outils MCP `get_document_text` +
`record_document_analysis` (§11) suffisent à la boucle complète.

---

## 12. Optional — Android TWA

Only if you want an installable Android app. Build the TWA (e.g. with
[Bubblewrap](https://github.com/GoogleChromeLabs/bubblewrap)), enrol it in Play
App Signing, then replace the `package_name` and `sha256_cert_fingerprints` in
the `assetlinks.json` route in [main.py](athena/main.py) with **your** package
name and Play App Signing SHA-256, and redeploy.

---

## 13. Adapting to another jurisdiction / language / user model

This app is Québec-, French-, and single-user-specific. Honest scope:

- **Taxes:** `models/invoice.py` hardcodes GST 5 % / QST 9.975 % (non-compounded).
  Replace with your jurisdiction's rates/rules.
- **Judicial deadlines:** `utils/deadlines.py` implements art. 83 C.p.c. + Québec
  statutory holidays. Replace with your rules.
- **Court files & reference data:** `models/reference.py` +
  `scripts/seed_reference_data.py` (courthouse/tribunal parsing and data).
- **Protocol templates:** `models/protocol.py` (CQ/CS templates).
- **Language:** all UI text is hardcoded French with no i18n framework —
  translating is a template-wide effort, not a config toggle.
- **Multi-user:** the single-user model is enforced server-side and the Firestore
  collections are flat (not user-scoped). Multi-tenancy is a substantial rewrite.

---

## 14. Local development

```bash
cp .env.example .env        # then fill it in (see §4.1); PowerShell: Copy-Item
pip install -r athena/requirements.txt
pip install -r athena/requirements-dev.txt

cd athena
python -m scripts.check_config       # sanity-check your .env
flask --app main run --debug         # http://127.0.0.1:5000
python -m pytest tests/ -q
```

⚠ **`flask run --debug` alone does not work here, and its error message is
the giveaway.** There is no `app.py`, no `wsgi.py` and no `FLASK_APP`, so
Flask answers *« Could not locate a Flask application »*. The app factory
lives in `main.py`, hence `--app main`. Measured 2026-09-12, both forms.

⚠ **`gunicorn` does not run on Windows** — it imports `fcntl`, which is
POSIX-only (`ModuleNotFoundError: No module named 'fcntl'`, measured). The
production-like check is a Linux/macOS/WSL step; on Windows the deploy
itself is the first place the `app.yaml` entrypoint is exercised. On a POSIX
box it is `gunicorn -b :8080 main:app`, run from `athena/`.

Notes:
- **Firestore emulator** (`gcloud emulators firestore start`) requires exporting
  `FIRESTORE_EMULATOR_HOST` before running Flask/scripts — otherwise the Admin
  SDK targets **live** Firestore via Application Default Credentials.
  ⚠ And point it at something that is actually **listening**: `main.py`
  registers a context processor for the Réception badge that runs a Firestore
  aggregation on **every template render**, and its fail-open catches
  exceptions, not an unreachable host. Aimed at a dead port, every page of the
  application therefore **hangs** instead of erroring — measured 2026-09-12,
  stack captured at `models/portail_invitation.compter_soumises`.
- `scripts/seed_reference_data.py` does **not** read `.env`; it needs
  `GOOGLE_APPLICATION_CREDENTIALS` / ADC.
- For local MCP testing, run on `:8080` (or set `MCP_CANONICAL_ORIGIN` to match
  your port) and mint a dev token: `python -m scripts.mint_dev_token`.
- If you have not enrolled Phone MFA locally, set `REQUIRE_MFA=false` in `.env`.

---

## 15. Operations

- **Backups / DR:** see [SECURITY.md](SECURITY.md#data-handling). Configure
  Firestore scheduled backups (or Point-in-Time Recovery) and Firebase Storage
  object versioning for *your* project — they are per-project settings, not code.
- **Monitoring:** Cloud Logging (log name `pallas-athena`) and Cloud Trace. The
  event/span vocabulary is in [OBSERVABILITY.md](athena/OBSERVABILITY.md). Logs
  default to the `global` region — see §6.8 to keep them in Montréal.
- **CSP:** **enforced** (since 2026-07-11, after a report-only window showed
  only `script-src` violations). `script-src` uses a per-request nonce
  (`build_csp` in `security.py`, no `'unsafe-inline'`); violations still post to
  `/csp-report` (`report-uri`) and log as `csp_violation`.
- **Rollback:** Cloud Build keeps the 3 most-recent non-serving versions.
  `gcloud app versions list`, then migrate traffic:
  `gcloud app services set-traffic default --splits=<VERSION>=1`.
  ⚠ Rolling back past the idempotency claim (lot 0a) hands the MCP entries to code
  that does not know the `pending`/`partial` states and will execute a
  same-key retry again — see the last bullet of §11 before writing through
  the connector on the older version.
- **Déploiement unique (D22) — the whole MCP write-expansion program in ONE
  release (branch `mcp-ecriture-finitions`: lots 0a to 5 and the finitions).
  The push is DONE: `319a064` (the merge of step 4) serves in production.
  What remains is the lawyer's to run — the checks after the push, steps 9
  to 15 — and no result of them is recorded here yet; the accounting train
  of steps 16 to 18 was SUPERSEDED on 2026-10-05, its switch never armed.
  The releases that follow (« Bulk creators », « Arborescence par défaut »
  and « No MCP switch (2026-10-05) », below) run their own trains on top of
  this one.**
  **Since 2026-10-05 the connector has no switch and no separate
  accounting scope** (« No MCP switch (2026-10-05) »): wherever this
  runbook — or a lot's train below it — sets, keeps or flips an
  `MCP_*_ENABLED` value, offers or ticks « Autoriser la comptabilité », or
  counts tools with and without that grant, it records how its release
  shipped; today ONE write box grants every write tool, the accounting ones
  included. The lawyer's decision D22 (2026-09-29): the
  final branch is pushed ONCE, under ONE consent train, and the accounting
  switch is flipped LATER, on its own, after a supervised pilot. The per-lot
  bullets below keep their detail and their recipes; each step here names
  the one it runs. Where they differ — each lot's own « push the lot as ONE
  deploy », its own revocation and its own re-consent — **this order
  wins**: the revocations of lots 1b to 4 collapse into step 5, every
  lot's push into step 6, their re-consents into step 7 — lot 5's arming,
  revocation and re-consent are steps 16 and 17 (superseded on
  2026-10-05). What the release ships: **80** tools under the
  write grant (31 read, 49 write) and six accounting tools hidden behind
  `MCP_COMPTABILITE_ENABLED: "false"`; one TTL `fieldOverride`
  (`mcp_upload_tickets.expire_at`) and no composite index; no dependency;
  no `cron.yaml` or `firestore.rules` change; no DavX5 account re-add.
  Already done, nothing to redo (plan « Ops », 2026-09-29): the claude.ai
  organisation allows code-execution egress to `storage.googleapis.com`
  (« Lot 2A », step 7), and the canonical bucket's `staging/` 7-day rule was
  verified on 2026-09-28 (« Lot 2A », step 3). Left as they are, by the
  lawyer's decision: the two fee payments written before the D-4 rule
  (trust sequences 28 and 42, 1 000,00 $) — `verify_trust_integrity`
  reports them as NOTES, never repaired. Deferred by the lawyer, not a
  blocker: the lot 2B pilot on one real letter (« Lot 2B », step 4) — until
  it has run, do not rely on `create_template` with `substitutions` for a
  gabarit the practice will send from.

  **Before the push** — read-only first, then the two prerequisites that
  must precede the code:
  1. *Measure, read-only*, the day of the push (the environment of the T3
     recipe below: ADC, inline variables, never `ENV=production`). Each was
     clean on 2026-09-28/29; the registers have moved since, so run them
     again:
     - `python -m scripts.verify_trust_integrity` — exit `0`, or `2` with
       every note read with the lawyer (the two pre-D-4 fee payments above
       among them; the notes of check 8 describe history — « Finitions »,
       item 5);
     - `python -m scripts.verify_admin_integrity` — exit `0`; check 8
       (`amount_paid` == Σ receipts, over EVERY invoice carrying a payment)
       and check 9 (a kind stored against its direction) above all
       (« Administration direction (lot 0b) », « Lot 3 » step 2, « Lot 5 »
       step 0);
     - the lot 4 measurement (« Lot 4 », step 1: a contact on both sides of
       a dossier, a representation the forward rule refuses) — no output is
       the expected answer;
     - the storage-identity listing (« Storage identity (lot 0a) »:
       `users/unknown/`, `staging/unknown/`) — « matched no objects »;
     - « Paramètres → Profil du cabinet » carries both tax numbers
       (« Lot 3 », step 1) and the lawyer's and the firm's names (D23: a
       profile naming neither refuses every fee payment — « Finitions »,
       item 5).
     Any écart: stop, and decide it with the lawyer before going further.
  2. *The index file*, from the repo root of THIS branch:
     `firebase deploy --only firestore:indexes --project $PROJECT`, then the
     read-only TTL listing until it reads `ACTIVE` (« Lot 2A », step 1).
     Garbage collection only — first so it is never forgotten.
  3. *The designation of the active gabarits, against production, from a
     checkout of THIS branch* — the script ships with the change it
     prepares, so `main` does not have it until the push:
     `python -m scripts.designer_gabarits_actifs` (simulation), `--apply`,
     then the simulation again — every kind « déjà désigné », no « [!] »
     line (the recipe: « Active gabarits (lot 2A, step T3) »). The old code
     ignores the field, so this opens no outage; pushing FIRST would — every
     note d'honoraires and note print refuses until it runs. From here to
     the push, **nobody edits or uploads a note-d'honoraires or note-print
     template** (the old code still picks by recency, and the new one will
     print the designated one); re-run the simulation right before step 6.
  4. *Merge `main` into the branch.* `main` carries one commit the branch
     lacks, `f69663e` — the same change as the branch's first commit
     `861c6b8` (the same parent `21012c0`, identical trees), so the merge
     brings no content and cannot conflict. Run the suite on the merge
     result (`python -m pytest tests/ -q -p no:cacheprovider`, from
     `athena/`) BEFORE step 5: Cloud Build runs it again as the gate, but a
     red build after the revocation leaves the connector down.
  5. *Revoke and disconnect — BEFORE the push*:
     `python -m scripts.revoke_mcp_tokens`, and remove the connector in
     claude.ai. **This step is the ONLY control that keeps a token granted
     before the program from reaching its new write tools** — there is no
     code gate (the lawyer's decision D19, 2026-09-29; §11): skipped, every
     tool of lots 1b to 4 is live for the token in force the moment the
     deploy lands, under a consent screen that never described it. Pause
     any scheduled Claude job for the deploy window (§11, last bullet).
     `MCP_WRITE_ENABLED` stays `"true"`.

  **The push:**
  6. *ONE push of the merged branch to `main`*, `app.yaml` reading
     `MCP_ENABLED: "true"`, `MCP_WRITE_ENABLED: "true"` and
     `MCP_COMPTABILITE_ENABLED: "false"` (the branch ships that way — check
     it). Cloud Build runs the suite as the gate. Never lot by lot: each
     push would put new tools under the text consented to before it.
  7. *Re-add the connector and READ the new screen before ticking
     « Autoriser les écritures »*: the blocks each lot's train lists
     (« Lot 1b » step 3, « Lot 2A » step 6, « Lot 2B » step 3, « Lot 3 »
     step 6, « Lot 4 » step 5). The accounting box is NOT offered — its
     switch is off — and that is expected. Re-consenting while
     `MCP_WRITE_ENABLED` is `"false"` would yield a read-only grant without
     a word.
  8. *Verify `tools/list`*: **80** tools (31 read, 49 write), no accounting
     tool, `decide_rendez_vous` alone carrying `openWorldHint: true`; and
     the `initialize` text opens on the SAFETY CORE (« Pallas Athena
     (single-user Quebec civil litigation practice manager). SAFETY CORE —
     read before ANY write. NEVER, whatever the tool: … »), the CONVENTIONS
     (« CONVENTIONS: French data; … ») ending within its first 2 048
     characters,
     8 000 bytes at most. The deploy gate pins both, and the descriptor
     budget (`tests/test_mcp_descriptor_budget.py`: about 248 KB of its
     280 KB cap).

  **After the push** — checks that write nothing, or only on test data,
  but for ONE deletable draft (step 12). They share their test data, so
  keep it until step 14 says to delete it:
  9. *DavX5, on the wire then on the device* (Change Impact item 2 — it
     fails silently): on TWO test dossiers, both `actif`, the first with
     one task, one note and one confirmed event and a TEST contact of role
     « client » as its only client (the set-up of « Lot 4 », step 2) — the
     lot 4 curl checks (« Lot 4 », step 6: `root` — 207, as many dossier
     collections as actif + en_attente dossiers —, the Depth:0 PROPFIND —
     207 —, and the `report` counts); a PROPFIND Depth:1 on one active
     dossier collection and on `/dav/addressbook/` (« Finitions », item 1);
     and the wire checks of lot 0a/0b — a test task moved from the first
     test dossier to the second (« DAV relocation »), a contact and a task
     created by PUT (« Contacts created on the phone », « Tasks,
     hearings… » item 1).
  10. *A phone edit*, on the test dossiers' events and tasks only:
      « Finitions » item 3 (a line typed after the metadata block of an
      event and of a task survives, and no block is re-imported; the event
      moved from one test dossier's calendar to the other's), and « Tasks,
      hearings… » item 3 (a « Reportée » test hearing moved on the phone
      stays « Reportée »).
  11. *The Outlook mirror*: « Lot 1b », C1, on a scratch event of a test
      dossier — a reschedule through `update_hearing` moves its Outlook
      copy at the mirror's next run (`outlook_mirror: "follows"`; Cloud
      Scheduler's « Force run » on an `outlook` job runs it at once), a
      cancellation removes it.
  12. *Word opens every generated document WITHOUT repair* (Change Impact
      item 3 — no test sees Word's prompt): the « Lot 2A » step 9 list, on
      a test dossier; a document drawn from a SCRATCH template whose first
      version was restored on its page (« Active gabarits »); and the note
      d'honoraires of « Lot 3 » step 3 (the web) and step 7e (the
      connector). That note needs a REAL invoice — a test invoice burns a
      number for ever (step 14) — so it files ONE draft into that invoice's
      dossier, in « Projets » — « Mandat › Factures » once « Arborescence
      par défaut » below is deployed — (the connector's call returns the same note
      when it is identical, `reused: true`): the one write outside the test
      data. Delete it in the application once opened.
  13. *The upload ticket, once*, on a test dossier (« Lot 2A », step 8):
      one PDF through `begin_upload` → PUT → `finalize_upload`, and one
      deliberately wrong `md5_base64` that files nothing — note which of the
      two refusals happened.
  14. *No invoice number burned*: read `counters/invoices-{year}` before
      and after the « Lot 3 » step 7 smoke test (`preview_invoice`, the
      same-status `update_invoice` no-op, `get_budget` → an unchanged
      `create_budget_version`) — never `create_invoice`; the counter must
      read exactly what it read. Then delete the test data of steps 9 to 13
      in the application, in the order the deletion guards allow: the
      tasks, notes and events, the documents and their folders, then the
      two test dossiers (a dossier with a child record is refused), then
      the test contacts (a contact a dossier still names is refused), and
      the scratch template.
  15. *Tell the lawyer* what changed on the web (each lot's « Changements
      web à annoncer » in the Phase History of `git show abc63b4:CLAUDE.md`;
      « Finitions », items 4-6), and that the next REAL fee payment is the
      pilot of the one-transaction fee payment (« The fee payment is ONE
      transaction », its last paragraph — never a test at trust). The
      other per-lot device checks (lot 0b items 2-5, lot 1a L2-L4, lot 1b
      A-D) stay available:
      run first the ones he relies on. Watch the first week: the
      finitions' `unexpected` messages and the first void (« Invoice void
      (lot 0b) »). Update BOTH copies of the claude.ai skill `pallas-athena`
      the same day, with the lists of « Lot 1b », « Lot 2A », « Lot 2B »,
      « Lot 3 », « Lot 4 » and « Finitions » at once — every count read 80
      (31 + 49) as this release shipped, and reads **88** (32 + 56) for
      every write token since 2026-10-05, the accounting tools included;
      the accounting disciplines (« Lot 5 », step 9), which were to wait for
      step 17, go in with the rest since that date — in the claude.ai
      plugin « athena » (1.1.0), which replaced the skill `pallas-athena`:
      remove that one, never update it (« No MCP switch (2026-10-05) »,
      step 5). The same day, re-export
      and re-paste the claude.ai skill « Analyse documentaire » (`python -m
      scripts.exporter_competence_analyse` — « Finitions », its D25
      paragraph): it is generated, and any copy exported before the review
      of D25 says the analysis « remplace » the stored category without
      exception.

  **Later — the accounting switch, its own train, once the release has run
  clean: SUPERSEDED on 2026-10-05, the switch never armed.** The lawyer
  removed the switch (`MCP_COMPTABILITE_ENABLED`), the `athena:comptabilite`
  scope and its box that day (« No MCP switch (2026-10-05) », below): the five
  accounting writes became ordinary tools of the write grant and
  `get_admin_ledger` an ordinary read, so the token in force, `athena:read
  athena:write` — granted under a screen that said the connector would
  never write to either register nor record a payment — reaches them from
  that deploy on: no arming and, by the lawyer's explicit choice, no
  re-consent; both integrity scripts ran clean against production on
  2026-10-05, before it. Steps 16 to 18 stay as
  the record of what was planned: there is no switch to flip, no box to
  tick, and a write token lists **88** tools (32 read, 56 write), not the
  86 below. What of them still serves: the supervised pilot's recipe
  (« Lot 5 », steps 6 and 7) for a first accounting write the lawyer
  wants to watch, and the accounting lists (« Lot 5 », step 9), due with
  step 15's — in the plugin « athena » 1.1.0.
  16. *(Superseded.)* First both integrity scripts again, ON the deployed
      version, before anything is armed (« Lot 5 », step 2 — the connector
      will write into these registers, and an écart there first could not
      later be told from its own). Then `MCP_COMPTABILITE_ENABLED: "true"`
      in `app.yaml`, and deploy (« Lot 5 », step 3). Nothing changes for
      the token in force: it lacks the scope, and sees neither the tools
      nor the box's text.
  17. *(Superseded.)* Revoke and re-consent, ticking « Autoriser les
      écritures » AND « Autoriser la comptabilité » after reading its block
      (« Lot 5 », step 4); verify **86** tools (32 read, 54 write) and the
      other token shapes (« Lot 5 », step 5). Then the skill's accounting
      lists (« Lot 5 », step 9).
  18. *(Superseded.)* The supervised pilot on a TEST administration
      account — never the trust register, never the real operations
      account (« Lot 5 », step 6) —, then both integrity scripts again
      (« Lot 5 », step 7). Only then may the first real bank movement be
      recorded through the connector.

  **Emergency switches** — none since 2026-10-05: the three `app.yaml`
  switches (`MCP_ENABLED`, `MCP_WRITE_ENABLED`, `MCP_COMPTABILITE_ENABLED`)
  were removed that day, by the lawyer's decision, and nothing stops the
  writes alone, or the accounting tools alone, any more. The break-glass is
  `python -m scripts.revoke_mcp_tokens`, without a deploy — a write is
  refused at once (it re-reads its token, bypassing the cache), a read
  within the 5-minute success cache, and the connector stays off until it
  is authorized again. A rollback: « Rollback » above — past lot 0a an
  older version executes a same-key retry again (§11, last bullet), past T3
  it picks templates by recency again (« Active gabarits », last
  paragraph), and past 2026-10-05 it reads its own `app.yaml` switches
  again: the accounting tools hidden behind `MCP_COMPTABILITE_ENABLED:
  "false"`, under a scope no token holds.
- **Storage identity (lot 0a, 2026-09-25):** every Storage path is now built
  under a uid that `utils/storage_identity.py` has validated, and nothing can
  write under `users/unknown/` or `staging/unknown/` any more — the routes used
  to read the uid as `session.get("user_id", "unknown")`, and
  `doc_template.update_template` fell back to `"unknown"` on a malformed stored
  path. Before deploying that change, and whenever you want to confirm nothing
  was ever written there, run this **read-only** check (it lists, it never
  deletes; « One or more URLs matched no objects » is the GOOD answer):

  ```bash
  # The value of FIREBASE_STORAGE_BUCKET in athena/app.yaml — this shell does
  # not read app.yaml. Left unset, the ":?" stops the command instead of
  # listing "gs:///users/unknown/", whose error is NOT the good answer.
  FIREBASE_STORAGE_BUCKET=your-bucket-name
  gcloud storage ls --recursive "gs://${FIREBASE_STORAGE_BUCKET:?}/users/unknown/" --project=$PROJECT
  gcloud storage ls --recursive "gs://${FIREBASE_STORAGE_BUCKET:?}/staging/unknown/" --project=$PROJECT
  ```

  If either lists objects, **stop and inventory them before deploying**: their
  Firestore `storage_path` still resolves (downloads keep working), but a
  template stored there can no longer have its FILE replaced — the model now
  refuses, rather than writing the new file under the same fallback prefix.
  Nothing here moves or deletes them for you.
- **DAV relocation (lot 0a, 2026-09-25):** moving a task or a note to another
  dossier in the web app now runs through `dav.sync.relocate_resource` (the
  same calls in the same order as the hand-written blocks it replaced — pinned
  by `tests/test_mcp_dav_resync.py`, but DavX5 fails silently, so check the
  wire once after the deploy). Move a test task from dossier `D1` to `D2` in
  the app (both `actif` or `en_attente` — a closed dossier's collection is
  drained, not listed), then ask both collections for a full `sync-collection` REPORT (curl
  prompts for the DAV password; it never goes on the command line):

  ```bash
  D1=first-dossier-id D2=second-dossier-id TASK=the-task-id
  DAV_USER=you@yourdomain.example   # the AUTHORIZED_USER_EMAIL of app.yaml
  BODY='<?xml version="1.0" encoding="utf-8"?><d:sync-collection xmlns:d="DAV:"><d:sync-token/><d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop></d:sync-collection>'
  for D in "$D1" "$D2"; do
    echo "== dossier-$D"
    # The multistatus is ONE line: -o keeps just this task's <D:response>.
    curl -s -u "${DAV_USER:?}" -X REPORT \
      -H "Content-Type: application/xml; charset=utf-8" --data "$BODY" \
      "https://yourdomain.example/dav/dossier-$D/" \
      | grep -o "<D:href>[^<]*$TASK\.ics</D:href>.\{0,120\}"
  done
  ```

  Expected: under `D1` the task's href is followed by
  `<D:status>HTTP/1.1 404 Not Found</D:status>` (its tombstone); under `D2` by
  a `<D:propstat>` carrying its `getetag` (live). A task still live under `D1`,
  or absent from `D2`, means the phone will keep the stale copy — stop and
  investigate before relying on DavX5.
- **Administration direction (lot 0b):** the model now DERIVES an
  administration entry's direction from its kind, and a web edit — which
  always names the kind — re-derives it. An « Autre recette » stored as a
  déboursé by an older direct model call would therefore have its sign
  flipped, and the operations balance moved by twice its amount, the first
  time anyone edits it. Before deploying lot 0b, run the **read-only**
  integrity script and read its check n° 9:

  ```bash
  python -m scripts.verify_admin_integrity
  ```

  A line « type recette_autre inscrit en déboursé » (or any kind/direction
  mismatch) names an entry to decide on by hand before the deploy; the script
  repairs nothing. No such line is the expected answer — every writer so far
  derived or passed the right direction.
- **Contacts created on the phone (lot 0b, B7):** a vCard created in DavX5
  is now stored under the id its URL names and the UID it carries (before,
  it got a server id: its own href 404'd and a duplicate synced back). The
  suite pins it through the real routes, but DavX5 fails silently, so check
  the wire once after the deploy with a throw-away contact (curl prompts for
  the DAV password):

  ```bash
  DAV_USER=you@yourdomain.example   # the AUTHORIZED_USER_EMAIL of app.yaml
  RID=$(python -c "import uuid; print(uuid.uuid4())")
  printf 'BEGIN:VCARD\r\nVERSION:4.0\r\nUID:curl-%s\r\nFN:Test Curl\r\nN:Curl;Test;;;\r\nEND:VCARD\r\n' "$RID" > /tmp/test.vcf
  curl -s -o /dev/null -w '%{http_code}\n' -u "${DAV_USER:?}" -X PUT \
    -H "Content-Type: text/vcard; charset=utf-8" -H "If-None-Match: *" \
    --data-binary @/tmp/test.vcf "https://yourdomain.example/dav/addressbook/$RID.vcf"
  curl -s -u "${DAV_USER:?}" "https://yourdomain.example/dav/addressbook/$RID.vcf" | grep UID
  ```

  Expected: `201`, then `UID:curl-<the same id>`. A 404 on the GET means the
  fix is not live. Delete the test contact in the app afterwards. Contacts
  created on the phone BEFORE this deploy keep their server id — their
  phone-side copy is the orphan. Then, on the device, create one contact in
  DavX5's address book, sync twice, and confirm it appears ONCE.
- **Prescription alerts include « en attente » dossiers (lot 0b, B7):** the
  dashboard and the MCP `get_agenda` briefing (the 07:00 scheduled run too)
  now list every dossier `actif` OR `en_attente` whose prescription falls
  within 60 days. Expect NEW rows the first morning if any dossier on hold
  carries a prescription date — they were silenced before, not absent. No
  index to deploy: the same `(status, prescription_date)` index serves both
  statuses.
- **DAV sync tokens (lot 0b, B7):** a failed read of a collection's CTag no
  longer rewrites it with a fresh token (which forced every client into a
  full resync, silently). During a Firestore blip a PROPFIND/REPORT now
  answers 500 and DavX5 retries; tombstones are still written. Nothing to do
  at deploy time.
- **Trust register (lot 0b, B5):** before deploying, run the **read-only**
  integrity script:

  ```bash
  python -m scripts.verify_trust_integrity
  ```

  Until lot 0b a « Virement inter-dossiers » between two clients of the SAME
  dossier credited the recipient without debiting the source: the dossier's
  stored per-client balances then disagree with the register. A line
  `dossier …/…: trust_balance_by_client stocké … ≠ recalculé …` (or
  `trust_cleared_by_client`) names that damage — already in the data, not
  caused by the deploy. The script repairs nothing, and the overdraft control
  reads the stored cleared balance, so decide each one by hand before relying
  on it. After the deploy the model refuses, on the web as it will on the
  connector: an entry, a clearing, a reversal or a transfer that falls in a
  period a completed reconciliation covers (the message names its end date);
  a future date; a cash (« comptant ») disbursement other than the art. 72
  refund of a sum of 7 500 $ or more received in cash; a fee payment by any
  method other than cheque or transfer (art. 58); a fee payment on an invoice
  that imputes a provision. The two new queries ride existing indexes
  (`trust_transactions(account_id ASC, sequence ASC)` for the art. 72 receipt
  lookup, `trust_reconciliations(account_id ASC, period_end DESC)` for the
  lock floor) — nothing to deploy.
- **Trust register, checks 5-10 (lot 5a):** the same **read-only** script now
  also re-proves every completed reconciliation at its period end (5), checks
  every clearing date — never before its entry, never in the future, never
  inside a period already closed when the clear was made (6) —, checks that
  dates never go backwards in sequence order (7), lists the entries the lot 0b
  rules would refuse today, every single-leg « virement inter-dossiers »
  and every entry whose objet contradicts its sens — « Dépôt du client »
  paid out, « Remise au client » paid in: the connector refused that pair
  from lot 5b, and since the lawyer's decision D24 (2026-09-29) the model
  refuses it — and the single leg — for every caller, the web form
  included, so these notes describe history —, plus, since the same day, a
  fee payment drawn for ANOTHER client's invoice (D21, ids only) and one
  made to a payee other than the lawyer or his firm as « Paramètres »
  names them (D23, without the name — an unreadable profile is an écart,
  the check not run)
  (8), reads every dossier's stored per-client
  balances, so a balance no entry backs and a client in shortfall both show
  (9), and checks that every fee payment (« paiement d'honoraires ») is backed
  in the administration register by standing recettes adding up to exactly
  its amount — and by none once it was reversed (10: the D-4 linkage; until
  lot 5a step 3 the web wrote that recette after the trust commit and failed
  open, and the reversal cascade reversed the recettes one commit at a time
  — the first only, before step 2 — so both directions drifted in silence;
  since step 3 both are ONE transaction, and check 10 measures the history). A fee payment with no linked recette at all is a note when it was
  written before the D-4 rule (commit e719588, 2026-08-17 11:23 HAE), or when
  an UNLINKED recette of the same amount exists — the manual entry the trust
  page's banner asks for when the automatic one fails, which can never carry
  the link; every other mismatch is an écart. A linked recette dated BEFORE
  the fee payment it carries is a note (D16 on history: the new fee-payment
  model will refuse it). Step 8 of lot 5 (`models/fee_payment`) relies on this measure: its
  reversal treats a fee payment with zero linked recettes as legacy. Run it in
  production, BEFORE the model steps of lot 5 deploy. Its exit
  code says what it found: `0` clean, `1` at least one **écart** (a figure or
  an invariant the register no longer stands on — fix or explain each before
  going further), `2` **notes** only. Its reads are not one snapshot: a write
  committed WHILE it reads (another tab, or Claude through the connector)
  would make a balance read before it disagree with the entries read after
  it — an écart no data carries. So it compares every trust and
  administration account's last-write time around each pass (every register
  write rewrites its account) and re-reads, up to three passes; when every
  pass was crossed, the FIRST écart says so and the others are not
  established — re-run it when nobody is writing. A note is history the register keeps
  (it is append-only): an entry written before a rule existed, or a
  reconciliation completed before the as-of rework (commit 945572a,
  2026-07-29 22:37 HAE), which ran under the old « tickable = en circulation
  now » code and may fail a re-proof it was never held to. Read each note with
  the lawyer; nothing is repaired automatically. One note is known and is
  not a balance error: an inter-dossier transfer written BEFORE lot 5 has a
  FIRST leg whose frozen running balance is high by exactly the amount
  (check 1, reported as a note since the lot-5 completeness review, an écart
  before) — the model stored the pair's net balance on both legs where the
  running balance after the debit leg is lower by the amount. The account,
  dossier and client balances are right, and so is every reconciliation (it
  reads the SECOND leg's figure); what is wrong is the frozen running balance
  of that one leg, which the art. 38 journal PDF prints in its « Solde »
  column (and which trips the PDF's carried-forward cross-check when that leg
  opens a period). The register is append-only, so the historical legs keep
  it; a transfer made since lot 5 stores the exact figure on both legs, and
  check 1 recognizes the old defect only by its whole signature (a
  transfer's debit leg, its recette leg the next sequence, a difference of
  exactly the amount) — any other mismatch stays an écart.
- **The fee payment is ONE transaction (lot 5a, step 3):** « Paiement
  d'honoraires » on the trust entry form now writes the trust withdrawal, its
  recette in the operations account and the invoice's payment in ONE
  Firestore transaction (`models/fee_payment`), and its « Contre-passer »
  reverses the withdrawal, EVERY linked recette and their invoices' payments
  the same way. No index, no dependency, no Tailwind class, no MCP surface.
  **Before deploying it**, the two read-only integrity scripts must be clean
  of écarts — `verify_admin_integrity` check n° 8 above all (an invoice whose
  `amount_paid` is below what a linked recette took makes its fee payment's
  reversal REFUSE: the model never clamps), and `verify_trust_integrity`
  check n° 10 (a fee payment with no linked recette reverses at trust alone —
  legacy — and says so). **Web behaviour changes to tell the lawyer:**
  1. a fee payment the administration side refuses (a closed account, a
     credit card, a date inside a reconciled period of the operations
     account, a payment the invoice refuses) is now refused WHOLE, on the
     form, and nothing is written at trust either — the old
     « la recette n'a pas pu suivre — inscrivez-la manuellement » banner, and
     the withdrawal it followed, are gone;
  2. a new optional field « Date du dépôt au compte d'administration » dates
     the recette (D16) — blank = the withdrawal's date; it may be LATER (a
     cheque of 30 August deposited on 2 September, even once August is
     reconciled on the operations account), never earlier, never future;
  3. the reversal of a fee payment is all-or-nothing too: a recette that
     cannot follow refuses the whole reversal, with its reason (the old
     « administration_contrepassation » banner is gone), and the confirmation
     page says the recette is reversed in the same operation;
  4. « Déjà compensée » on the administration form is ONE create born
     « compensée » (it could half-fail, leaving the entry « en circulation »
     under a banner);
  5. a bulk clear listing the same entry twice is refused (it cleared it
     twice and doubled the client's cleared balance — latent: no web form
     posts a bulk clear today);
  6. the entry's page now SAYS, in an amber banner, what a successful fee
     payment or reversal still needs the lawyer to read (the route used to
     drop it): an invoice addressed to ANOTHER client of the dossier than
     the one whose funds leave trust (check that this client authorised
     it), a fee payment reversed at trust alone (no linked recette — with
     what to do about a recette typed by hand — or recettes already
     reversed), and a client's cleared balance gone negative after a
     reversal (a shortfall the firm must cover). And a deposit date refused
     because the operations account is reconciled THROUGH TODAY now says to
     retry tomorrow, instead of asking for a later date the form refuses;
  7. a save whose OUTCOME IS UNKNOWN — the commit's answer lost (a timeout,
     or the commit retried after it landed) — now says so: « … a échoué
     d'une façon qui ne permet pas de savoir si elle a été inscrite :
     vérifiez le journal … avant de réessayer » (fee payment and its
     reversal, trust entry, inter-dossier transfer, administration entry,
     card payment). The fee payment used to answer « Rien n'a été inscrit.
     Veuillez réessayer » over a withdrawal that HAD landed — the retry
     withdrew the fees a second time — and a retried commit re-ran the body
     over its own landed writes, applying the amount TWICE to the balances
     and the invoice under a success (review of step 3). Rare by
     construction; each occurrence logs an `unexpected` line (see
     OBSERVABILITY.md) and is worth a look at the journal.
  **Pilot on REAL movements only** — the registers cannot be cleaned up, and
  there is no preproduction: the next real fee payment on an issued invoice,
  then check the trust entry, the operations account's encaissement (dated
  as the form said) and the invoice's « Paiements » block and status. Never
  « test » a reversal on the trust register. Re-run both integrity scripts
  afterwards.
- **Invoice void (lot 0b, B1):** voiding now reads, inside ONE transaction,
  the `trust_transactions`, `admin_transactions`, `timeentries` and
  `expenses` rows whose `invoice_id` names the invoice — single-field
  equalities the automatic index serves (no `fieldOverride` exempts
  `invoice_id`), and the invoice page reads the trust rows too. The suite's
  fake store does not model indexes, so watch the FIRST void in production:
  a red « Erreur lors de l'annulation » banner beside an `invoice void
  failed` ERROR carrying `FAILED_PRECONDITION` would mean an index is
  missing (nothing is written — the void fails closed).
- **Tasks, hearings and the step button on the phone (lot 0b, B3/B4):** five
  DAV fixes the suite pins through the real routes, on a store that is not
  DavX5. The plan requires the hearing STATUS round trip to be checked on the
  device, so do all five once after the deploy:
  1. *A task created on the phone keeps its URL.* PUT a throw-away VTODO,
     then GET the same href:

     ```bash
     D=an-active-dossier-id
     DAV_USER=you@yourdomain.example   # the AUTHORIZED_USER_EMAIL of app.yaml
     RID=$(python -c "import uuid; print(uuid.uuid4())")
     printf 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//curl//test//FR\r\nBEGIN:VTODO\r\nUID:curl-%s\r\nDTSTAMP:20260101T000000Z\r\nCREATED:20260101T000000Z\r\nSUMMARY:Test curl\r\nSTATUS:NEEDS-ACTION\r\nEND:VTODO\r\nEND:VCALENDAR\r\n' "$RID" > /tmp/test.ics
     curl -s -o /dev/null -w '%{http_code}\n' -u "${DAV_USER:?}" -X PUT \
       -H "Content-Type: text/calendar; charset=utf-8" -H "If-None-Match: *" \
       --data-binary @/tmp/test.ics "https://yourdomain.example/dav/dossier-$D/$RID.ics"
     curl -s -u "${DAV_USER:?}" "https://yourdomain.example/dav/dossier-$D/$RID.ics" | grep UID
     ```

     Expected: `201`, then `UID:curl-<the same id>`; a 404 on the GET means
     the fix is not live. Delete the test task in the app afterwards. Tasks
     created on the phone BEFORE the deploy keep their server id — their
     phone-side copy is the orphan.
  2. *The step button reaches the phone.* In the app, click « Compléter » on
     a protocol step that has a linked task; sync jtx: the task shows
     completed. Click « Rouvrir »: it comes back open.
  3. *A hearing keeps « Reportée ».* Set a test hearing to « Reportée » in the
     app, sync, move it by an hour in the Android calendar, sync again, and
     open it in the app: still « Reportée ». The served `.ics` carries
     `STATUS:TENTATIVE` and `X-PALLAS-STATUS:reportée`. If DavX5 turns out to
     drop the X-property, the status reads back as before the fix (degraded,
     never corrupted) — note it and report it.
  4. *Notes stop growing.* Edit that hearing's time three times on the
     phone, and a task's title three times in jtx: neither the hearing's
     notes nor the task's description gains a « Dossier: … » block, and the
     hearing's `Visioconférence:` line (if it has a link) is still served.
  5. *A deleted series leaves the phone.* Only if you use series: delete a
     series one of whose occurrences was moved to another dossier; that
     occurrence disappears from the phone too.
- **Protocols and their tasks (lot 1a, L2):** no index, no data migration —
  legacy protocols read `closed_by` as « not recorded » and are therefore
  never reactivated automatically; legacy steps read an empty etag. What
  changes on screen, to announce: a protocol or step form left open while
  something changed elsewhere is refused with the amber banner; the step
  button refuses on a suspended protocol or one completed by hand
  (« réactivez-le… »); a new start date no longer moves completed steps nor
  confirmed CS dates, and moves the linked tasks still on the old date; a
  note saved on a CS step no longer confirms its date (new « Confirmer cette
  date » box) — and a CS date « confirmed » before this deploy only by such
  a note (still equal to the template's suggestion) now shows « À modifier »
  again, with the box: tick it to keep the date through a start-date
  change; a task linked from a step can no longer change dossier (except
  back into its protocol's dossier). Check
  the phone once after the deploy:
  1. *A reopened task reactivates its protocol.* On a test dossier whose
     tribunal is left blank (so the regime gate accepts either C.p.c.
     template), create a « CS — Procédure ordinaire » protocol
     with « Créer les tâches automatiquement » ticked — the web « Ajouter
     une étape » form creates no task, and a « Conventionnel » protocol has
     no step to link. In the app, complete every step but ONE with its
     « Compléter » button; then complete that last step's linked task in
     jtx, sync: the step is completed and the protocol reads « Complété ».
     Reopen the task in jtx, sync: the step reads « À venir » and the
     protocol « Actif » again.
  2. *A linked task stays in its dossier.* Move that task to another
     dossier's list in jtx: the sync reports an error (422) and the task
     stays in its list — delete the test protocol and task afterwards.
  3. *A moved deadline reaches the phone.* Edit that step's deadline in the
     app (a CS step's deadline is editable; a CQ one is locked by law): the
     linked task's due date follows on the next sync (a `REPORT
     sync-collection` of `/dav/dossier-<id>/` returns a new token).
- **Notes and the théorie de la cause (lot 1a, L3):** no index, no data
  migration (the new `notes/{id}/revisions/` documents are only written, on
  the automatic index; deny-all `firestore.rules` already covers them). What
  changes on screen, to announce: « Ajouter une théorie de la cause » now
  refuses with a red banner when the dossier cannot be read (« Erreur de
  lecture ») or already holds TWO théories (it used to go quiet, or show an
  empty sheet); deleting a note that was modified in the meantime comes back
  to the note with a red banner instead of returning silently to the list.
  Deleting the théorie de la cause now leaves a write-once copy of its text in
  Firestore (`notes/<id>/revisions/`, field `content:delete`) — nothing in
  the app lists it yet; recover it from the Firestore console if ever needed.
  Check once after the deploy, with curl (DavX5 fails silently):
  1. *The théorie never changes dossier through DAV.* Take a dossier D1
     whose théorie de la cause exists (its note id `NID` is in the URL of
     « Modifier » on the Analyse tab) and a second active dossier D2. Fetch
     it, then PUT the very same body into D2's collection:

     ```bash
     DAV_USER=you@yourdomain.example   # the AUTHORIZED_USER_EMAIL of app.yaml
     curl -s -u "${DAV_USER:?}" "https://yourdomain.example/dav/dossier-$D1/$NID.ics" > /tmp/theorie.ics
     curl -s -o /dev/null -w '%{http_code}\n' -u "${DAV_USER:?}" -X PUT \
       -H "Content-Type: text/calendar; charset=utf-8" \
       --data-binary @/tmp/theorie.ics "https://yourdomain.example/dav/dossier-$D2/$NID.ics"
     curl -s -o /dev/null -w '%{http_code}\n' -u "${DAV_USER:?}" \
       "https://yourdomain.example/dav/dossier-$D1/$NID.ics"
     ```

     Expected: `422`, then `200` — the théorie is still served from D1 and
     the Analyse tab of D1 still shows it. A `204` on the PUT means the fix
     is not live (and the théorie just moved to D2: move it back by
     repeating the PUT towards D1).
  2. *jtx Board still moves a théorie the way it always did.* jtx moves an
     entry by creating a copy in the target list and deleting the original:
     the copy becomes the target dossier's théorie only if that dossier has
     none (otherwise an ordinary undated note), and the original's deletion
     now leaves its snapshot. Nothing to do unless you move one.
- **Bookings rendez-vous, the hearing form and the task form (lot 1a, L4):**
  no index (the Bookings reads are `source == "bookings"` equalities, served
  by the automatic index), no data migration, no cron change. What changes on
  screen, to announce: the Réception « Rendez-vous » tab shows « Lecture des
  rendez-vous impossible » instead of « Aucun rendez-vous à confirmer » when
  Firestore cannot be read; « Confirmer » and « Refuser » from a page older
  than the rendez-vous (the Bookings sync moves a pending slot silently) are
  refused with a red banner — and a stale « Refuser » no longer touches
  Outlook; a rendez-vous the client cancelled can no longer be confirmed;
  « Lier à … » links the contact whose address matches the requester's
  exactly, whatever the page posted; the hearing edit form refuses a save
  over a change made elsewhere (amber banner); the task form refuses an
  unknown dossier instead of filing the task under « Général »; and
  refusing a pending rendez-vous while Outlook cannot be reached at all
  (Graph unconfigured, or no event id) now shows a RED banner — « la
  réunion Outlook n'a PAS été annulée … annulez-la manuellement » —
  where it used to print a green « Rendez-vous refusé. » with the client
  still booked. The
  connector gains nothing yet (`decide_rendez_vous` is lot 1b). Check once
  after the deploy, with a Bookings reservation booked under an ALIAS
  address (never a real client — a refusal emails the booker):
  1. *A refusal still cancels the meeting, once.* Wait for the reservation
     to appear in Réception (≤ 10 min), click « Refuser »: the green banner
     says the Outlook meeting was cancelled and the client notified, the
     alias receives the cancellation (the comment Graph forwards is the
     fixed « Rendez-vous refusé par le juriste. »), and the card is gone.
     Click « Refuser » again from a second tab left open on the old page:
     « Ce rendez-vous était déjà refusé. », and no second cancellation
     email.
  2. *A stale page never reaches Outlook.* Book a second reservation, open
     Réception, then MOVE that reservation in Bookings (or in Outlook) and
     wait for the next sync (the card's time changes on a reload). From the
     tab still showing the OLD time, click « Refuser »: red banner
     « Ce rendez-vous a changé… Outlook n'a pas été touché », no email.
     Reload and confirm it: it enters the calendar and the phone (a
     `REPORT sync-collection` of `/dav/general/` returns a new token).
- **Lot 1b — the agenda through the connector (ONE consent train, steps
  L5–L8):** eleven new `athena:write` tools — `update_task`, `reopen_task`,
  `update_note`, `edit_analyse` (L5); `create_protocol`, `update_protocol`,
  `add_protocol_step`, `update_protocol_step` (L6); `update_hearing`,
  `create_hearing_series`, `decide_rendez_vous` (L7) — plus new arguments on
  `create_hearing` (modality, link, reminder, status) and two new READ modes
  of `list_hearings` (`serie_id`; `bookings: "pending"`, which shows the
  requester's name and email to ANY token, a read-only one included).
  `decide_rendez_vous` is the connector's FIRST OUTBOUND effect: refusing a
  Bookings request cancels the client's Outlook meeting, which notifies him.
  The scope is frozen at issuance and a new read mode reaches the token in
  force the moment it deploys, so this is a CONSENT TRAIN, never an ordinary
  deploy — and the four steps ship in ONE push:
  1. `python -m scripts.revoke_mcp_tokens` and remove the connector in
     claude.ai BEFORE pushing.
  2. Push the lot 1b commits as one deploy. `MCP_WRITE_ENABLED` stays
     `"true"` — re-consenting while it is `"false"` yields a read-only grant
     without a word.
  3. Re-add the connector and READ the new screen before ticking
     « Autoriser les écritures »: a « Tenir l'agenda, le calendrier, les
     notes et le protocole » block (it says that reopening a task reopens
     its protocol step and a protocol the last step had closed); a
     « Décider des demandes de rendez-vous Bookings » block (it quotes the
     fixed text « Rendez-vous refusé par le juriste. » the client receives);
     the READ paragraph naming the pending Bookings requests; and, in the
     « jamais » list, « défaire une annulation en silence » and « rédiger un
     message destiné à un client ou à un tiers » where « rouvrir une tâche »
     used to be, « supprimer quoi que ce soit » now reading « … dans Athéna
     — annuler une tâche ou un événement les conserve ».
  4. `tools/list` counts **60** (27 read, 33 write); `decide_rendez_vous`
     alone carries `openWorldHint: true`, and it and `create_hearing_series`
     list `idempotency_key` as required. The descriptor byte budget (plan
     train step 4) is the deploy gate's
     `tests/test_mcp_descriptor_budget.py` — about 175 KB of its 280 KB
     total cap at the end of lot 1b, every tool under its 8 KB cap; a red
     build there means a description to TRIM, never a cap to raise.

  No index, no data migration, no cron change, no Tailwind class. Two WEB
  changes ship with it (D17, 2026-09-27), to announce: (a) a protocol that
  is not « actif » can no longer take a new step nor a step edit on ANY
  path — the model refuses them for the web as for the connector (lot 1b
  had the connector alone ask, through a `require_active` flag now gone) —
  and its page hides those controls behind an amber banner, « Ce protocole
  n'est pas actif : réactivez-le (Modifier le protocole → Statut
  « Actif ») avant de modifier ses étapes. », keeping only the « Rouvrir »
  of a completed step when the protocol was closed by its last step (which
  reactivates it); (b) EVERY change of a note's content — the web form,
  the phone, the connector — now keeps the replaced text in
  `notes/{id}/revisions/` (lot 1b kept it for the connector's writes
  only), and the note page says « Versions précédentes : N » once there is
  one. A phone PUT that changes a note's text now commits in a transaction
  (guarded on the version just read, re-read on a lost race —
  last-write-wins is unchanged; one that loses every attempt answers 503
  with `Retry-After`, so the phone keeps its edit and re-sends it — never
  the 422 that would read as a refused body); one that changes only its title or
  category is the same bare write as before. Then, on scratch dossiers —
  DavX5 fails silently, so the `REPORT sync-collection` is the proof and
  the phone the confirmation:

  **A. Tasks, notes, the théorie (L5).**
  1. *A move reaches the phone.* `update_task` with another `dossier_id`: a
     `REPORT sync-collection` of the OLD `/dav/dossier-{id}/` (with its last
     token) lists the task as deleted, the new collection lists it; sync jtx
     on the phone — the task has left the old dossier's list and sits in the
     new one, once. The same with `update_note` on an ordinary note.
  2. *A reopen follows its step, or does nothing.* On a task whose protocol
     step the cascade closed, `reopen_task`: the step reopens and the
     protocol is « actif » again. With a second protocol made actif first,
     the call is refused and the task stays « terminée ».
  3. *Replaced prose is kept.* `update_note` with `content` (and the etag
     from `get_note`) — the note opens with « Révisée par Claude le … »; the
     old text sits in Firestore under `notes/{id}/revisions/`.
  4. *The théorie is edited by bloc, never guessed.* `edit_analyse` without
     operations creates it (`mode: created`), then one `append` on bloc F
     lands at the end of F only. Delete the « ## Bloc F » heading in the app
     and try again: refused, naming F, nothing written.

  **B. The protocol (L6)**, on a dossier with no protocol:
  1. *Creation makes no task unless asked.* `create_protocol`
     (`cq_simplifié`, a start date): every step comes back with its etag,
     `tasks_created: 0`, and a `REPORT sync-collection` of
     `/dav/dossier-{id}/` returns the SAME token as before (nothing to sync).
     A second `create_protocol` on the dossier is refused, naming the first.
  2. *The cascade is reported, re-read.* Suspend that protocol, create a
     `conventionnel` one, `add_protocol_step` twice (the second with
     `create_linked_task: true` — the task appears on the phone after a
     sync), then `update_protocol_step` `status: complété` on the first and
     then the second: `status_change.protocol_closed: true`, the protocol
     reads `closed_by: auto` in `list_protocol_steps`, the task is
     « terminée » on the phone. `status: à_venir` on the second step reopens
     the protocol (`protocol_reopened: true`); while it is actif,
     `update_protocol` `status: actif` on the suspended CQ protocol is
     refused, naming it. Complete that second step again: the conventionnel
     protocol closes by itself once more.
  3. *The law's text stays the law's.* Now reactivate the CQ protocol
     (`update_protocol`, `status: actif` — accepted, the other is closed);
     `status: à_venir` on the conventionnel's second step is then REFUSED
     (reopening it would make two protocols actif) and nothing moves.
     `update_protocol_step` with a `title` on one of the CQ template steps:
     refused, the message opens with « `title` refusé » ; with a
     `deadline_date` on it: « `deadline_date` refusé ». A `notes` edit on
     the same step lands.
  4. *A new start date keeps what is done.* Complete a CQ step, then
     `update_protocol` with another `start_date`: `recompute.preserved`
     names that step (`completed`), every other one is in `recompute.moved`
     with its new etag.
  5. *A CS suggestion is not confirmed by sending it back.* On a Cour
     supérieure dossier, `create_protocol` `cs_ordinaire`, then
     `update_protocol_step` with a step's OWN `deadline_date`: nothing is
     written and a warning says the date stays a suggestion (the connector
     has no confirmation flag; « Confirmer cette date » in the application
     does it). A DIFFERENT date is confirmed (`date_confirmed_now: true`).

  **C. The calendar and Bookings (L7).**
  1. *A reschedule keeps the Montréal hour and the duration — and Outlook
     follows.* `create_hearing` timed on a late-October day (09:00–10:30),
     then `update_hearing` with only a November `date` (after the
     daylight-saving change): `list_hearings` reads 09:00–10:30 `-05:00`, a
     `REPORT sync-collection` of `/dav/dossier-{id}/` lists the event, the
     phone shows 09:00, and the Outlook copy moves to the new day within 10
     minutes (`outlook_mirror: "follows"`). `status: annulée`:
     `outlook_mirror: "removed"`, the Outlook copy is gone within 10 minutes,
     the phone shows it cancelled.
  2. *A status set here reaches the phone exactly.* `update_hearing`
     `status: reportée` on another scratch event (`D` its dossier, `HID` its
     id, `DAV_USER` as in the lot 0b checks), then
     `curl -s -u "${DAV_USER:?}" "https://yourdomain.example/dav/dossier-$D/$HID.ics" | grep STATUS`:
     `STATUS:TENTATIVE` and `X-PALLAS-STATUS:reportée`. Move it by an hour
     in the Android calendar, sync, and `list_hearings` still reads
     `reportée` — if it reads `à_confirmer`, DavX5 dropped the X-property
     (the lot 0b device check): degraded, never corrupted; report it.
  3. *A series is one write, and an occurrence moves only detached.*
     `create_hearing_series` without a key: refused, nothing created. With
     one (`hebdomadaire`, `count: 3`): the phone receives the three events in
     ONE sync, and `list_hearings` with `serie_id` lists them. `update_hearing`
     with another `dossier_id` on one occurrence: refused, naming
     `detach_from_series`; with `detach_from_series: true` in the same call:
     `detached: true`, and a `REPORT sync-collection` of the OLD collection
     lists it as deleted. The same series call with the SAME key again:
     `idempotent_replay: true`, still three events.
  4. *A refusal reaches the client once, with the fixed text.* Book a
     « Bookings with me » slot from an ALIAS mailbox (never a real client —
     a refusal emails the booker). `list_hearings` with
     `bookings: "pending"` lists it with its etag and the alias.
     `decide_rendez_vous` `refuser` with a wrong `expected_etag`: refused,
     and NO cancellation reaches the alias. With the listed etag: the alias
     receives Outlook's cancellation carrying « Rendez-vous refusé par le
     juriste. », and the result reads `graph_cancelled: true`,
     `client_notified: true`. The same call with the SAME key:
     `idempotent_replay: true` and no second mail.
  5. *A confirmed Bookings rendez-vous changes only by its dossier and
     notes (D10).* Book a second slot, `decide_rendez_vous` `confirmer`
     with `lier_partie: false` (or true when a contact carries the alias):
     it appears in the calendar and on the phone. `update_hearing` with
     another `start_time` on it: REFUSED, naming `start_time` and pointing
     to Outlook for the reschedule — `list_hearings` still reads the old
     hour and the Outlook meeting is unchanged; with another
     `hearing_type`, REFUSED too, and the refusal sends that one to the
     application, not to Outlook. `update_hearing` with a `dossier_id` (and a
     `notes_append`): written, `moved: true`, `outlook_mirror:
     "not_mirrored"`, the event leaves « Général » for the dossier's
     collection on the phone — and the Outlook meeting is still
     unchanged. Then move the meeting by an hour IN OUTLOOK: within 10
     minutes Réception (« Rendez-vous ») shows the divergence, and
     « Appliquer » carries the new hour into Athéna.

  **D. The web follows the same rules (D17).**
  1. *A protocol that is not « actif » takes no step change, anywhere.*
     Suspend a scratch protocol (Modifier le protocole → Statut
     « Suspendu »): its page shows the amber banner, no « + Ajouter une
     étape », no « Modifier » / « Ajouter une note » on a step, no
     « Compléter ». `add_protocol_step` on it is refused too. Reactivate it:
     every control is back. On a protocol its last step closed
     (`closed_by: auto` in `list_protocol_steps`), the page still offers
     « Rouvrir » on a completed step, and clicking it makes the protocol
     « actif » again.
  2. *No edit of a note's text is lost, whoever makes it.* On a scratch
     note, change its text in the app, then in jtx Board on the phone and
     sync: the note page reads « Versions précédentes : 2 », and Firestore
     holds two documents under `notes/{id}/revisions/` — the original
     text, replaced `via: web`, and the app's version, replaced `via: dav`
     (`via` names the path that REPLACED it). Change only the
     note's category on the phone: still 2. The `PUT` answers 204 with the
     new `ETag`, and a `REPORT sync-collection` of `/dav/dossier-{id}/`
     lists the note.

  Then update BOTH copies of the claude.ai skill `pallas-athena` the same
  day. What lot 1 made false there: « 49 outils : 27 en lecture, 22 en
  écriture » (now 60: 27 + 33); the théorie de la cause « lecture seule, ne
  s'édite que dans l'application » (it is `edit_analyse`'s, by bloc, with
  the etag); « une note créée ne peut être ni éditée » and « tâches,
  audiences … créés définitivement » (`update_note`, `update_task`,
  `update_hearing`); « rouvrir une tâche … impossible ici » and « créer un
  protocole … impossible, par conception » (`reopen_task`,
  `create_protocol`); « `complete_task` — seul changement de statut »;
  « `create_hearing` … jamais modifiable ni supprimable ici » (modifiable
  now, never deletable) — and, false since lot 0a already, « `en_cours` sur
  une tâche déjà terminée rouvre l'étape liée » (refused; that is
  `reopen_task` — the one rare exception: `en_cours` on an OPEN task whose
  step was left « complété » reopens it, unreported by
  `protocol_step_effect`); « Aucun des 49 outils » and « les 49 outils »
  of the references list (60); the INSCRIPTION row and the « Remplace ce
  qu'on nomme » / « Ajoute » rows (`update_task`, `update_note`,
  `update_hearing` and its `notes_append`, `update_protocol`,
  `update_protocol_step`, `edit_analyse` and its append mode); the
  « confirmer avec l'utilisateur » list (`create_hearing_series`,
  `decide_rendez_vous`); in `references/outils.md`, family C « aucune de
  ces écritures n'est annulable », the tables without AGENDA/BOOKINGS, and
  `create_hearing` « créé `à_confirmer` » (it now takes `status`); and add
  the `expected_etag` workflow (never retry blindly after `stale_etag`),
  the two cascades (a completion can close a protocol, a reopen
  reactivates it), `list_hearings` `bookings: "pending"` and the Bookings
  warnings (a refusal notifies the client with a fixed text; a
  confirmed rendez-vous changes here only by its dossier and notes —
  rescheduling or cancelling it is refused and done in Outlook, any
  other change by the lawyer in the application); and, from D17, that a
  note's replaced text is kept whoever replaced it (the app, the phone,
  Claude — an append included), and that no step of a suspended or
  completed protocol changes anywhere until it is reactivated (the one
  exception: reopening a step of a protocol its last step closed).
- **Active gabarits (lot 2A, step T3, 2026-09-27) — run the designation
  script against production BEFORE deploying this release.** Until T3 the
  invoice note d'honoraires and the note print were filled from « the most
  recently updated template of the kind », so any edit of another template
  of that kind silently switched the letterhead printed on every client's
  note. Since T3 the code reads ONLY the template the lawyer DESIGNATED
  (`active_for`, set by « Désigner comme gabarit actif » on the template's
  page) and has **no recency fallback**: a kind with no designation refuses
  to generate (« Aucun gabarit … n'est désigné comme actif : désignez-en un
  dans Gabarits. »). The script designates, for each kind that has none
  yet, the template production is printing TODAY — the exact old rule —
  and the OLD code ignores the new field, so running it first means there
  is no outage window at all. Deploying first opens one: every note
  d'honoraires and every note print refuses until the script runs.

  Run it from a checkout of THIS release's branch — the script ships with
  the change it prepares, so `main` does not have it until the push that
  deploys, which is exactly one step too late. With Application Default
  Credentials (the script does NOT read `.env`, and `config.py` resolves its
  required variables at import — pass them inline, and never `ENV=production`,
  which would make it resolve the service's secrets; `GOOGLE_CLOUD_PROJECT`
  stops the client from inferring another project from ADC):

  ```bash
  cd athena
  export GOOGLE_CLOUD_PROJECT=$PROJECT FIREBASE_PROJECT_ID=$PROJECT \
    FIREBASE_STORAGE_BUCKET=your-bucket-name \
    AUTHORIZED_USER_EMAIL=you@example.com SECRET_KEY=unused-by-the-script
  # 1. Simulation (the default): per kind, what it WOULD designate.
  python -m scripts.designer_gabarits_actifs
  # 2. Designate. Each designation runs in the model's own transaction,
  #    against the etag just read — a template edited in between is
  #    refused, never designated blind (re-run).
  python -m scripts.designer_gabarits_actifs --apply
  # 3. Re-run the simulation: every kind must read « déjà désigné » with NO
  #    « [!] » line. Exit code 1 flags an anomaly (for instance a designation
  #    that is not the template the old code prints).
  python -m scripts.designer_gabarits_actifs
  ```

  Then deploy promptly: between the script and the deploy, the old code
  still selects by recency, so editing or uploading a note d'honoraires /
  note-print template in that window makes it print a template the new
  code will not use (step 3's « [!] » line catches that — re-run it right
  before pushing). After the deploy, open « Gabarits »: the designated template of
  each kind carries the « Actif » badge. A kind with no template at all
  (« aucun gabarit de ce type ») stays undesignated; upload one and
  designate it on its page.

  The same release keeps template FILE VERSIONS (D11): a replacement writes
  a new object `users/{uid}/templates/{id}/v{N}/{file}` and records a
  write-once `doc_templates/{id}/versions/{N}` entry — the previous object
  is never overwritten nor deleted, and the template's page lists every
  version with « Télécharger » and « Rétablir » (a restore becomes version
  N+1). Templates created before T3 have no entry until their first
  replacement records the file they had. **Manual Word check** (Change
  Impact item 3): replace a scratch template's file, « Rétablir » the
  first version, and open a document generated from the restored template
  — it must open without repair. No index to deploy (single-field queries
  only), no new secret, no new Tailwind class.

  ⚠ Rolling back past T3 is safe for the data but brings the old rules
  back: the older code selects by recency again (it ignores `active_for`),
  and its file replacement deletes the previous object — the `versions/*`
  entries stay in place, inert, and a version whose object that code
  deleted is then no longer restorable once T3 is redeployed (« Rétablir »
  refuses it: « Le fichier de cette version est introuvable »).
- **Upload tickets and the leak scan (lot 2A, step T5, 2026-09-27) — deploy
  the index file FIRST.** This release adds the `mcp_upload_tickets`
  collection (the record of the connector's future `begin_upload` /
  `finalize_upload` exchange) and its TTL policy, declared as a
  `fieldOverrides` entry in `firestore.indexes.json` beside the
  `mcp_idempotency` one — no composite index. Nothing calls the collection
  yet (the tools arrive with the lot's MCP release), so the order is about
  never having to remember it later:

  ```bash
  # From the repo root. Declares the TTL on mcp_upload_tickets.expire_at.
  firebase deploy --only firestore:indexes --project $PROJECT
  # Read-only check: the state must read ACTIVE (it takes a few minutes).
  gcloud firestore fields ttls list --collection-group=mcp_upload_tickets \
    --project=$PROJECT --format="value(ttlConfig.state)"
  ```

  The TTL is garbage collection only — a ticket's one-hour window is
  enforced in code, on every claim — so a missing policy costs storage,
  never security. Then verify the canonical bucket's `staging/` 7-day
  lifecycle rule with the read-only command of §6.6 (or
  `python -m scripts.provision --project=$PROJECT`, row
  `cycle-de-vie-staging`): the upload ticket relies on it to erase a file
  sent after its ticket closed. That row reads « inconnu » in the test
  corpus until its real output is recorded (`tests/fixtures/gcloud/
  production.json`, then remove it from `_CORPUS_EN_ATTENTE` in
  `tests/test_deployment_probe.py`). The same release ships the pure leak
  scan (`utils/docx_leak_scan.py`) and the identifier builder
  (`services/docx_identifiers.py`) the template writers will call, and the
  `run_write` persistence hooks that keep the ticket's upload URL out of
  `mcp_idempotency` — all dormant until the MCP release: no route, form or
  tool changes, no Tailwind class, no new secret.
- **The upload ticket's tools (lot 2A, step T9 — plan D4): `begin_upload` /
  `finalize_upload`.** An MCP-visible change, so it ships inside the lot's
  consent train (revoke the connector BEFORE pushing, re-add it after — §11 —,
  then check `tools/list` and the byte budget — the deploy gate's
  `tests/test_mcp_descriptor_budget.py`).
  Nothing to provision beyond T5 (the `mcp_upload_tickets` TTL and the
  `staging/` lifecycle rule above — verify both are in place first): no
  index, no secret, no Tailwind class. Two prerequisites only the lawyer can
  do:

  1. **claude.ai organisation settings → code execution → network egress:**
     allow `storage.googleapis.com`. Without it `begin_upload` still opens
     tickets, every PUT from the sandbox fails, and the tickets expire
     harmlessly after their hour (their bytes never arrive; nothing is
     filed). Optional hardening, if a firm-wide egress to that host is
     unwanted: a Cloudflare Worker (for instance `depot.poirierlavoie.ca`)
     forwarding ONLY `PUT`s to this bucket's resumable-session URIs — the
     tool contract would not change, only the host `upload_url` names.
  2. **Pilot on a scratch dossier** before relying on it: ask Claude to
     compute the size and MD5 of an attached PDF, `begin_upload`, PUT,
     `finalize_upload` — the document appears in the dossier, its category
     « présumée » if one was given. Then check the refusals: with a deliberately
     WRONG `md5_base64`, NO file may enter — either the service itself
     refuses the PUT (the declared MD5 travels in the session's initiation
     metadata; the repo cannot prove GCS enforces it, so this is what the
     pilot establishes) and `finalize_upload` then answers « Aucun fichier
     n'a encore été reçu », or the PUT lands and `finalize_upload` refuses
     it (« empreinte MD5 ») with the staging object gone
     (`gcloud storage ls gs://<bucket>/staging/<uid>/mcp/` — read-only).
     Both are a pass; a document in the dossier is a failure. Note which
     one happened. A ticket left more than an hour must answer « expiré ».
     For a gabarit, upload a letter FROM a dossier with `dossier_id`: its
     parties' names must be refused by name until accepted
     (`accept_residual`, a new ticket). **Manual Word check** (Change Impact
     item 3): open a document generated from an uploaded template — and from
     one uploaded with `scrub_properties` — without repair.

  The upload URL is the ONE capability a tool output carries (CLAUDE.md,
  Security Rules): it is never stored (`mcp_idempotency` keeps the result
  without it) and never logged; `mcp_upload_opened` / `mcp_upload_finalized`
  carry ids, codes and counts (OBSERVABILITY.md). A burst of
  `mcp_upload_finalized` refusals with `reason: empreinte_differente` means
  bytes other than the declared ones reached a session — look at it.
- **Lot 2A — files, templates and the upload ticket through the connector
  (ONE consent train: the lot's commits — branch `mcp-ecriture-lot2`, with
  any earlier lot of that stack not yet deployed — in ONE push).**
  Ten new tools — the FILES family (`update_document`, `move_documents`,
  `manage_folder`, `fill_gabarit`, `create_document`, `begin_upload`,
  `finalize_upload`), the TEMPLATES family (`create_template`,
  `update_template`) and one read, `list_templates` — plus the model
  hardening they rely on (the T1-T5 items above: documents, folders, active
  templates and versions, the generation service, the ticket store). The
  scope is frozen at issuance and a new READ tool reaches the token in force
  the moment it deploys, so this is a consent train, never an ordinary
  deploy. **In this order** — each step is a precondition of the next:
  1. **The index file first** (the TTL fieldOverride of
     `mcp_upload_tickets.expire_at`, garbage collection only — no composite
     index in this lot):
     `firebase deploy --only firestore:indexes --project $PROJECT`, then the
     read-only
     `gcloud firestore fields ttls list --collection-group=mcp_upload_tickets --project=$PROJECT --format="value(ttlConfig.state)"`
     until it reads `ACTIVE`.
  2. **The designation of the active gabarits, against production, from a
     checkout of THIS branch** (the script ships with the change it
     prepares — `main` only has it after the push, one step too late):
     `python -m scripts.designer_gabarits_actifs` (simulation), then
     `--apply`, then the simulation again — every kind « déjà désigné », no
     « [!] » line (the exact recipe, ADC and inline variables, never
     `ENV=production`, is the T3 item above). The old code ignores the
     field, so there is no outage window; deploying BEFORE it opens one —
     every note d'honoraires and note print refuses until it runs. Re-run
     the simulation right before step 5 (an edit of a note template in
     between moves the old code's pick).
  3. **Verify the canonical bucket's `staging/` 7-day lifecycle rule —
     read-only** (§6.6's command, or `python -m scripts.provision
     --project=$PROJECT`, row `cycle-de-vie-staging`). The ticket relies on
     it to erase a file PUT after its hour closed; a missing rule costs
     storage, never a wrongly filed document.
     **Verified on the live `athena-pallas` bucket on 2026-09-28**
     (`staging/` age 7, plus the versioning rule deleting non-current
     objects after 180 days); the output is in the probe corpus
     (`tests/fixtures/gcloud/production.json`). Re-check only if the
     bucket's lifecycle is ever edited.
  4. **`python -m scripts.revoke_mcp_tokens`, and remove the connector in
     claude.ai** — BEFORE pushing. `MCP_WRITE_ENABLED` stays `"true"`: no
     token exists to abuse, and re-consenting while it is `"false"` yields a
     read-only grant without a word.
  5. **Push the lot as ONE deploy** (Cloud Build runs the suite as the
     gate). Pushing its commits one by one would put new tools under the old
     consent text, and the T3 reader switch ahead of step 2's designation.
  6. **Re-add the connector and READ the new screen before ticking**
     « Autoriser les écritures »: a « Classer vos documents » block (a
     category Claude sets stays « présumée » until you confirm it; never
     « Projets » nor « Reçus du portail » — the seven application folders
     since « Arborescence par défaut » below), « Produire des projets Word » (a
     filled gabarit ALWAYS in « Projets », under « Interne » since the default
     tree; a copy's protection level
     presumed, and its category presumed unless you had set it),
     « Téléverser un fichier » (a write-only link that transits the
     conversation, a file filed only within the hour, and the
     `storage.googleapis.com` prerequisite), a separate « Gérer vos
     gabarits » block (a replaced file kept and restorable; a NEW name given
     to an existing gabarit checked against the dossiers its files came
     from, when recorded — `name_check` says whether it was; the active note
     templates are designated by you alone — though a new file for the
     active one prints at once); and, in the « jamais » list, « modifier le
     fichier d'un document existant », « remplacer le fichier d'un gabarit
     sans en garder la version précédente », « obtenir un lien de lecture
     ou de téléchargement d'un fichier d'Athéna — le seul lien … celui d'un
     dépôt »,
     « confirmer une catégorie ou une analyse présumées » and « désigner le
     gabarit actif ». Then check `tools/list`: **70** tools (28 read, 42
     write), the ten new ones present, the write tools absent on a
     read-only grant, and `MCP_WRITE_ENABLED=false` hiding them. The byte
     budget is the deploy gate's `tests/test_mcp_descriptor_budget.py`
     (about 204 KB of its 280 KB cap; `update_partie` the largest at
     7.8 KB of 8) — a red build there means a description to TRIM, never a
     cap to raise.
  7. **claude.ai organisation settings → code execution → network egress:
     allow `storage.googleapis.com`** (an organisation admin's step). It is
     not per-bucket: any sandbox run could then PUT to ANY Cloud Storage
     bucket. If that is unwanted, the optional hardening is a Cloudflare
     Worker (for instance `depot.poirierlavoie.ca`) forwarding ONLY `PUT`s
     to this bucket's resumable-session URIs — the tool contract does not
     change, only the host `upload_url` names (and the allowlist then names
     only that host). Without either, every PUT fails and the tickets
     expire unused.
  8. **Pilot the upload ticket on a SCRATCH dossier** — the T9 item's
     recipe: a PDF whose size and MD5 the sandbox computes, `begin_upload`,
     PUT, `finalize_upload` (the document appears, « présumée » if a
     category was given); a deliberately WRONG `md5_base64` never files
     anything (either GCS refuses the PUT and `finalize_upload` answers
     « Aucun fichier n'a encore été reçu », or the PUT lands and is refused
     « empreinte MD5 » with the staging object gone — note which); a ticket
     left more than an hour answers « expiré »; a gabarit uploaded from a
     dossier with `dossier_id` is refused on its parties' names until
     accepted.
  9. **Word checks** (Change Impact item 3 — the fill engine's outputs must
     open WITHOUT repair; no test can see Word's repair prompt): open a
     `fill_gabarit` result whose blocs sit in a NUMBERED paragraph (Word's
     numbers, once each, renumbering when a paragraph is inserted), one with
     a `markdown: true` bloc (no double numbering), a `create_document`
     Markdown result (headings, a table), a `create_document` copy, a
     template installed by `update_template` and one « Rétabli » on its
     page, and a document generated from each of those two.

  Then update BOTH copies of the claude.ai skill `pallas-athena` the same
  day. What lot 2A makes false there (on top of the lot 1 list above):
  « 49 outils : 27 en lecture, 22 en écriture » / « Aucun des 49 outils » /
  « Les 49 outils » (now 70: 28 + 42); « la catégorie d'un document et son
  niveau de protection sont dérivés, jamais choisis » (règle 2) and, in
  `references/vocabulaires.md`, « Catégorie de document … en écriture elle
  est dérivée » — a category `update_document`, `create_document` or
  `begin_upload` sets is PRESUMED and only for a document WITHOUT an
  analysis, an analysis replacing it; « Il n'existe aucun paramètre de
  catégorie, à dessein » (le cliquet de protection) — true of
  `record_document_analysis` only; the INSCRIPTION row « pour un document :
  `record_document_analysis` » (a document's filing is `update_document`
  and `move_documents`, a new one `fill_gabarit`, `create_document` or the
  upload ticket, a gabarit `create_template` / `update_template` or the
  ticket); in `references/outils.md`, the family table without FILES and
  TEMPLATES, family B without `list_templates`, family C « aucune de ces
  écritures n'est annulable » and family D without `update_document` /
  `move_documents` / `manage_folder` / `update_template`; and — false since
  2026-08-31, which this lot's text sweep caught — « `query` ne touche que
  … nom affiché, nom de fichier, description, étiquettes » (SKILL.md
  INVENTAIRE row and `references/outils.md` twice): the fields are the
  analysis summary, `notes_internes` and `genere_depuis`, never a
  « description ». Incomplete rather than false, to extend the same day:
  SKILL.md « Les trois sémantiques d'écriture » (« Remplace ce qu'on
  nomme » lacks `update_document`, `update_template` and `manage_folder`'s
  rename and move, and no row says that `fill_gabarit`, `create_document`
  and `finalize_upload` always make a NEW document); `references/outils.md`
  — its pagination table (`list_templates` pages by offset) and its
  provenance bullet (a generated or copied document says « par Claude
  (connecteur) » in `genere_depuis`); `references/vocabulaires.md` (the
  template kinds and categories, `manage_folder`'s actions, `begin_upload`'s
  purposes and template modes). Add: the upload ticket (compute size and MD5 in the
  sandbox, one PUT, never show the link, egress to storage.googleapis.com,
  `finalize_upload` answering a filed ticket again rather than a new
  ticket); `fill_gabarit`'s discipline (read `list_templates` with
  `template_id` and the `dossier_id` first, write only the blocs and manual
  fields, plain paragraphs separated by a blank line with no numbering of
  your own, `markdown: true` only for internal structure, never « {{ » or
  « }} », always into « Projets » (« Interne › Projets » since the default
  tree — « Arborescence par défaut » below), and a refusal saying a party of the
  dossier « n'a pas pu être lue » means retry — never write around it);
  `create_document`'s two sources and its
  refusal when no note-print template is designated; the presumed category
  and its « Confirmer » in the application (a copy's category presumed
  only when the lawyer had not set it; its protection level presumed, to
  verify by qualifying the copy); the leak scan of a template taken from a
  dossier (`accept_residual` on the lawyer's word only) — and, since the
  fixups of lot 2A, that a RENAME is checked against the template's source
  dossiers when they are recorded (`name_check` says whether it was; a
  party's name still never goes into a template's name), that an uploaded
  gabarit must name its source `dossier_id` or declare
  `aucun_dossier_source: true` — only for a file from no dossier, never as a
  way round the check; and that the active note templates are the lawyer's
  to designate AND to undesignate. D18: a category the lawyer chose or
  confirmed (`category_set_by_lawyer: true` on `list_documents`) is never
  replaced — report the disagreement to him instead; a document filed
  before that marker counts as his unless its category is « autre » (fixups
  of lot 3).
- **Lot 2B — turning a finished letter into a gabarit through the connector
  (ONE consent train: branch `mcp-ecriture-lot2b` — with lot 2A and any
  earlier lot of that stack not yet deployed — in ONE push, then the PILOT
  on ONE real letter).** One new READ tool, `preview_templatize`, and one new
  argument, `create_template`'s `substitutions` — no new family, no index, no
  dependency, no Tailwind class, no `cron.yaml` change, nothing DAV-exposed.
  A new read reaches the token in force the moment it deploys, and the
  consent text changes (« transformé en gabarit »), so this is a consent
  train, never an ordinary deploy. **In this order:**
  1. **`python -m scripts.revoke_mcp_tokens`, and remove the connector in
     claude.ai** — BEFORE pushing. `MCP_WRITE_ENABLED` stays `"true"` (no
     token exists to abuse; re-consenting while it is `"false"` yields a
     read-only grant without a word).
  2. **Push the lot as ONE deploy** (Cloud Build runs the suite as the gate).
  3. **Re-add the connector and READ the « Gérer vos gabarits » block before
     ticking** « Autoriser les écritures »: a document « tel quel, ou
     transformé en gabarit » (a COPY, its document properties always emptied,
     all or nothing, each replacement counted in advance), checked « tel qu'il sera enregistré, les textes que Claude
     a demandé de remplacer compris », and « relisez un gabarit transformé
     avant de vous en servir » — the check reads names, numbers and
     addresses, never the rest of the text. Then `tools/list`: **71** tools
     (29 read, 42 write), `preview_templatize` present on a read-only grant
     too (it writes nothing), `create_template` listing `substitutions`. The
     byte budget is the deploy gate's `tests/test_mcp_descriptor_budget.py`
     (about 209 KB of its 280 KB cap).
  4. **The pilot — ONE real letter, in a real dossier** (Change Impact item
     3: no test can see Word's repair prompt, and only a real letter has a
     real letterhead). Pick a finished `.docx` the practice wrote — no
     tracked changes, no comments (accept, reject and delete them in Word
     first, then file it again) — with the client's name in the body AND in
     a header or footer if possible.
     a. **Preview.** Ask Claude to templatize it: it calls
        `preview_templatize` with the pairs it proposes (the client's name,
        the file number, the court number, the addresses…). Read the rows:
        each `substituted` count must match what the letter really holds,
        `in_non_target_parts` names a footnote (the title, subject, author,
        last editor and description are ALWAYS emptied on this path, and
        `scrubbed_properties` says which were), and `leak_scan.residues`
        lists what would stay — an ALL-CAPS heading the pairs missed shows
        up here as the literal itself (`origin: substitution`).
     b. **Adjust**, preview again, until `ready_to_create: true`.
     c. **Create** (`create_template`, the same pairs, each
        `expected_occurrences` = its `substituted`). Try one deliberately
        WRONG count first: it must be refused « rien n'a été écrit » and
        « Gabarits » must show nothing new.
     d. **Open the template in Word** (Gabarits → the new template →
        « Télécharger le gabarit »): it opens WITHOUT a repair prompt, the
        letterhead, fonts and headers are intact, and each replaced text now
        reads `{{…}}` — in the body, the header and the footer.
     e. **Fill it for ANOTHER dossier** (« Générer depuis un gabarit » on
        that dossier, or `fill_gabarit`), open the result in Word WITHOUT repair, and read
        it whole: the other client's name, file number and address are where
        the first ones were — and **nothing of the source client remains**:
        not in the body, not in a header or footer, not in a footnote, not
        in Fichier › Informations (the document properties).
     f. The source letter is unchanged in its dossier (same size, same
        date). If anything is wrong, delete the template in the application
        (the connector cannot delete) and note what the preview missed.

  Then update BOTH copies of the claude.ai skill `pallas-athena` the same
  day. What lot 2B makes false there: every « 70 outils » (now 71: 29 + 42),
  and any sentence saying a document becomes a gabarit only « tel quel » or
  that turning a letter into a gabarit must be done in Word. Add: the
  workflow — `preview_templatize` FIRST, adjust, then `create_template` with
  each `expected_occurrences` = `substituted`; matching is case-SENSITIVE
  (an ALL-CAPS variant is its own pair, with an ALL-CAPS field name); the
  field names are those `list_templates` shows for an existing gabarit (a
  misspelt one classifies « passthrough »); what stays in footnotes and
  field results is never replaced, and the title, subject, author and
  description are always emptied; a residue is accepted
  only on the lawyer's word; an OUTSIDE letter is filed first as a document
  of its dossier (`begin_upload` purpose document) and templatized there —
  the upload ticket never templatizes; the preview's rows count from 0 while
  its French messages number from 1; and the lawyer reads the new gabarit
  before it serves — the check never reads the rest of the letter's text.
- **Lot 3 — billing through the connector (3a: an ordinary release; 3b:
  ONE consent train — branch `mcp-ecriture-lot3`, with lot 2B and any
  earlier lot of that stack not yet deployed, in ONE push).** 3a is the
  model, service and web half and changes no MCP surface: one plan for
  issuing an invoice (`plan_invoice`) with its issuance rules, the
  brouillon correction (`/factures/<id>/brouillon`), the move of an
  un-invoiced time entry or disbursement to another dossier, the note
  d'honoraires as ONE generation (`services/note_honoraires.py`), and the
  budget's transactional version counter. 3b adds five tools — two reads,
  `preview_invoice` and `get_budget`, and the BILL family, `create_invoice`,
  `update_invoice`, `create_budget_version` —, `dossier_id` (the move) on
  `update_time_entry` / `update_expense`, and `source: invoice_note` on
  `create_document`. No index to deploy (single-field equalities and keyed
  reads only), no dependency, no Tailwind class, no `cron.yaml` change, no
  `firestore.rules` change (`counters/budget-{dossier_id}` falls under the
  deny-all), nothing DAV-exposed. The scope is frozen at issuance and the
  two new READ tools reach the token in force the moment they deploy, so 3b
  is a consent train, never an ordinary deploy. **In this order:**
  1. **The firm's GST and QST registration numbers — BEFORE deploying 3a.**
     Open « Paramètres → Profil du cabinet » and check both are filled.
     From 3a on the MODEL refuses a NEW invoice that charges GST or QST
     under an EMPTY number — on the web « Nouvelle facture » form as through
     the connector (« Le numéro d'inscription TPS du cabinet est vide… »).
     Invoices already issued are untouched: their numbers were snapshotted
     at issuance and nothing rewrites them.
  2. **`python -m scripts.verify_admin_integrity` is clean** (read-only —
     the environment of the T3 designation recipe above: ADC, inline
     variables, never `ENV=production`). Check n° 8 above all: every
     invoice's recorded `amount_paid` is backed by the register. The void
     (web « Annuler » and, from 3b, the connector's `update_invoice` with
     status « annulée ») refuses an invoice on which a payment stands, read
     from `amount_paid` AND the standing admin and trust entries; a drift
     the report names is an invoice whose void would be judged on figures
     the lawyer does not see — decide it by hand first.
  3. **3a may go alone as an ordinary release** once every earlier lot of
     the stack is deployed; otherwise its commits ride in step 5's push —
     they change no MCP surface, so the order below is unchanged. Web
     checks after it, none of which burns a number: on any brouillon,
     « Modifier le brouillon » then « Enregistrer » WITHOUT changing a field
     returns to the sheet and writes nothing (the no-op); « Note
     d'honoraires (Word) » on an existing, non-annulée invoice files its note
     in « Projets » (in « Mandat › Factures » since « Arborescence par
     défaut » below), and it must open in Word WITHOUT repair (Change Impact
     item 3 — the fill now runs in `services/note_honoraires.py`). Never
     test « Créer » on « Nouvelle facture »: a success takes a real number.
  4. **`python -m scripts.revoke_mcp_tokens`, and remove the connector in
     claude.ai** — BEFORE pushing 3b. `MCP_WRITE_ENABLED` stays `"true"`: no
     token exists to abuse, and re-consenting while it is `"false"` yields a
     read-only grant without a word.
  5. **Push the lot as ONE deploy** (Cloud Build runs the suite as the
     gate). Pushing 3b's commits one by one would put the new tools under
     the old consent text.
  6. **Re-add the connector and READ the new screen before ticking**
     « Autoriser les écritures »: the read paragraph now names the budgets;
     a « Facturer » paragraph (a NEW invoice, always a brouillon, only
     billable un-invoiced entries of one dossier, after a preview whose
     total must match exactly — and **it consumes the year's next number
     for ever, even if voided**; its entries are frozen, their phase aside,
     until voided), « Tenir une facture » (**only a brouillon is
     corrected**; marking « envoyée » **sends nothing** and is undone only
     by a void; a void needs a reason and is refused while a payment is
     recorded), « Budgets » (a NEW version, refused when a newer one was
     saved; every version kept, the proof of what the client was told);
     the CORRECT paragraph (an entry may be moved to another dossier), the
     IMPORT paragraph (« annulez la facture — ici ou dans l'application »)
     and the FILES paragraph (the Word note d'honoraires). In the « jamais »
     list: « envoyer une facture à qui que ce soit », « marquer une facture
     payée », « écarter en silence une entrée … nommée », and still
     « supprimer » and « inscrire ou encaisser un paiement » — while
     « changer le statut d'une facture ou l'annuler » and « émettre un
     nouveau numéro de facture » are GONE. Then check `tools/list`: **76**
     tools (31 read, 45 write), `preview_invoice` and `get_budget` present
     on a read-only grant too, and `MCP_WRITE_ENABLED=false` hiding the 45
     writes — the three BILL tools among them — while keeping those two
     reads. The byte budget is the deploy gate's
     `tests/test_mcp_descriptor_budget.py` (about 221 KB of its 280 KB cap;
     `update_partie` still the largest, 7.8 KB of 8).
  7. **The smoke test burns NO number** (Change Impact item 1 — and the
     lawyer's books: every successful `create_invoice` takes the year's
     next `YYYY-F###` for ever, and a void leaves a visible hole in the
     « Journal des honoraires »). First read the year's counter —
     read-only, ADC, the year being TODAY's in Montréal; « absent » means
     no invoice was numbered this year yet:

     ```bash
     cd athena
     export GOOGLE_CLOUD_PROJECT=$PROJECT
     python -c "import sys; from google.cloud import firestore; d = firestore.Client(project='$PROJECT').document('counters/invoices-' + sys.argv[1]).get(); print(d.to_dict() if d.exists else 'absent')" 2026
     ```

     Then, through Claude:
     a. `preview_invoice` on a dossier with unbilled work: `ready: true`
        and a total, or `refusals` saying exactly why (a blank tax number
        here means step 1 was skipped). Nothing is written.
     b. **Never call `create_invoice` here — not even with a total
        deliberately wrong.** That refusal is judged in `plan_invoice`,
        BEFORE the counter is read at all, so it proves nothing about the
        allocation's place inside the transaction; and the same call with
        the preview's TRUE total — one slip, one « helpful » correction —
        issues a real invoice under a permanent number. That the allocation
        commits with the invoice or not at all is the deploy gate's to
        prove (`tests/test_mcp_billing_writes.py`
        `test_a_source_changed_during_the_call_burns_no_number`,
        `test_a_create_that_would_burn_a_number_never_does`, and
        `tests/test_invoice_numbering.py`).
     c. `get_invoice` on an existing `envoyée` invoice: an `etag` and its
        `connector_transitions`; then `update_invoice` with `status:
        "envoyée"` — its CURRENT status — and that etag: `outcome:
        "unchanged"`, nothing written.
     d. `get_budget` on a dossier that has a budget, then
        `create_budget_version` with the `base_version` returned, `mode:
        "merge"` and one line equal to a stored one: `outcome: "unchanged"`,
        nothing written (a new version is permanent — the budget is
        append-only).
     e. `create_document` with `source: "invoice_note"` on an existing,
        non-annulée invoice: ONE note filed in « Projets » (« Mandat ›
        Factures » since « Arborescence par défaut » below) — its object in
        the bucket under `users/<the practice's uid>/…`, never
        `users/unknown/` (the Cloud Storage console shows it; no tool output
        carries a path) — which opens in Word WITHOUT repair; the same call
        again returns it (`reused: true`, `created: false`) instead of
        filing a second. (This
        one does write a document — delete it in the application if it is
        not wanted.)
     f. Re-read the counter: it must be exactly what it was.

     The promotion `brouillon → envoyée` (`update_invoice`) is undone only
     by a void: run it ONLY on an invoice the lawyer has chosen to promote
     anyway — an imported brouillon of the IMP-07 backlog, typically — and
     name it before the call. And when the lawyer really issues an invoice
     through `create_invoice`, re-read the counter after it: `seq` must have
     advanced by **exactly one** (a refusal never moves it; a success always
     does, once).

  Then update BOTH copies of the claude.ai skill `pallas-athena` the same
  day. What lot 3 makes false there (on top of the lot 1, 2A and 2B lists
  above): every tool count — « 49 outils » in the copies as they stand
  today, « 71 » once the lot 2B list is applied — now 76 (31 + 45); SKILL.md « Aucun des … outils
  n'expose de `dry_run`, de `preview` ni d'équivalent » — still no `dry_run`,
  but `preview_invoice` (like `preview_templatize`) is a READ that computes
  exactly what its write would do; « Les écritures qu'aucun outil ne fait :
  … changer le statut d'une facture, enregistrer un paiement ou annuler une
  facture … Tout cela passe par l'application » — the status and the void
  are `update_invoice` now, the payment alone stays in the application;
  `references/comptabilite.md` §1 « le seul retour est l'annulation de la
  facture dans l'application » and §7 rule 5 « La facture atterrit en
  brouillon et y reste. Le connecteur ne change aucun statut de facture »;
  `references/outils.md` — `complete_task` « seul changement de statut »
  (false since lot 1 already), family C « aucune de ces écritures n'est
  annulable par le connecteur » (an invoice it issues, it voids), family G
  listing reads only, `import_invoice` « atterrit en brouillon et y
  reste », and the row « Changer le statut d'une facture, enregistrer un
  paiement, annuler une facture — Impossible ici »; README « ne change
  aucun statut de facture »; and the four `set_*_phase` tools « sont les
  seuls à atteindre une ligne déjà portée à une facture » — SKILL.md « Le
  mur de la facturation », `references/comptabilite.md` §1 (its heading
  « la seule porte qui le traverse » too) and `references/outils.md`
  family E: the void (`update_invoice`, status « annulée », refused while
  a payment stands) reaches every such line, to RELEASE it — what stays
  true is that they alone CHANGE a line while it stays billed (the
  connector's own INSTRUCTIONS said the same until the review of this
  step). Incomplete rather than false, to extend the same day: « Remplace
  ce qu'on nomme » lacks `update_invoice` (a brouillon only) and the
  moves; `references/vocabulaires.md`'s
  invoice statuses should say which ones `update_invoice` sets (envoyée,
  en_retard, annulée — never payée, never back to brouillon) and that
  `create_budget_version` refuses ADM and HOR. Add: the two-step
  `preview_invoice` → `create_invoice` (the SAME selection, its total as
  `expected_total_cents`, an `idempotency_key` always, the lawyer's
  confirmation first — the number is consumed for ever); `update_invoice`'s
  discipline (one change a call, the etag from `get_invoice`, `en_retard`
  only past the due date, a `void_reason` on every void, a payment reversed
  first — in « Fidéicommis » or « Administration », as the refusal says);
  the note d'honoraires by `create_document` source `invoice_note`
  (`regenerate` only on the lawyer's word); `get_budget` →
  `create_budget_version` (`base_version`, `merge` keeping what a line does
  not name, `replace` making the lines the whole budget); and the move of
  an un-invoiced entry by `update_time_entry` / `update_expense` with
  `dossier_id` (its amount and phase kept; its share of the budget actuals
  follows it — a non-billable time entry counts in no budget). Since the
  fixups of lot 3, also: a `create_invoice` answer « Issue INCERTAINE »
  (reason `invoice_outcome_uncertain`) means the invoice MAY exist — re-read
  `list_invoices`, and retry only with the SAME key, never a new one before
  that re-read; the counter never reissues a number, while an IMPORTED
  invoice's number can be imported again once the voided invoice is deleted
  in the application; and IMP-07 names imported brouillons only.
- **Lot 4 — dossiers and contacts through the connector (4a: an ordinary
  release; 4b: ONE consent train — branch `mcp-ecriture-lot4`, with lot 3
  and any earlier lot of that stack not yet deployed, in ONE push).** 4a is
  the model, service and web half: the DavX5 drain of a dossier leaves the
  route for `services/dossier_dav.py` (its members read STRICTLY before the
  status is written — unreadable, the change is refused and nothing is
  saved —, re-runnable, with the amber banner and « Resynchroniser le
  téléphone »), `opened_date` / `closed_date` stamped as the Montréal day,
  the party-link rules in the model (a client who has EVER had trust funds
  on the dossier, a party a signification names and the last client never
  leave; a contact is never client AND adverse — grow-only on a legacy
  duplicate; every detached link journaled in `audit_events`), one
  representation at a time for mandataires (their notes sanitized and
  capped), and the D7 provenance of a compliance check (a presumed
  inscription AMBER on the fiche, « Confirmer » the only way to the
  lawyer's attestation, the coverage report keeping it open). Its
  MCP-visible part is read-only text: `list_deletions` accepts two new
  `entity_type` values and says what their rows name, and
  `get_coverage_report` counts a presumed check as open (none exists before
  4b). 4b adds four writes in two new families — DOSSIERS
  (`set_dossier_status`, `update_dossier_party`) and CONTACTS
  (`update_partie_mandataire`, `record_kyc_status`) —, `get_partie`'s
  provenance fields, `_no_replay` in the write protocol, and the texts. No
  index to deploy (the strict readers reuse the same single-field
  equalities, the trust check the existing `(dossier_id, client_id,
  sequence)` index), no dependency, no Tailwind class, no icon, no
  `cron.yaml` or `firestore.rules` change, and no DavX5 account re-add (no
  collection path, name or component set changes). **In this order:**
  1. **Measure, read-only, BEFORE deploying 4a** — the environment of the
     T3 designation recipe above (ADC, inline variables, never
     `ENV=production`). Two answers decide what to repair by hand:

     ```bash
     cd athena
     export GOOGLE_CLOUD_PROJECT=$PROJECT
     python - <<'PY'
     from google.cloud import firestore
     db = firestore.Client()
     # (a) A contact on BOTH sides of a dossier. From 4a the model lets such
     # a legacy duplicate stand (the rule is grow-only) — removing one side
     # in the dossier's form is its repair.
     for s in db.collection("dossiers").stream():
         d = s.to_dict() or {}
         both = set(d.get("client_ids") or []) & set(d.get("opposing_party_ids") or [])
         if both:
             print("des deux côtés :", d.get("file_number"), len(both))
     # (b) A representation the forward rule already refuses (an unknown
     # contact, itself, not an individual, another role). Such a contact
     # refuses EVERY save today, the web form's included — repair its
     # mandataires in the form before relying on it.
     parties = {s.id: (s.to_dict() or {}) for s in db.collection("parties").stream()}
     for pid, p in parties.items():
         for e in p.get("mandataires") or []:
             mid = e.get("id") if isinstance(e, dict) else None
             m = parties.get(mid)
             why = ("introuvable" if m is None else "soi-même" if mid == pid
                    else "pas une personne physique" if m.get("type") != "individual"
                    else "autre rôle" if m.get("contact_role") != p.get("contact_role")
                    else "")
             if why:
                 print("mandataire :", pid, "→", mid, why)
     PY
     ```

     No output is the expected answer. The identifiers it prints are for
     the lawyer's console only — paste them nowhere else. (A legacy
     single-mandataire contact — `mandataire_id`, migrated on read — is
     not scanned; it is repaired by its next save.)
  2. **4a may go alone as an ordinary release** once every earlier lot of
     the stack is deployed; otherwise its commits ride in step 4's push.
     Web checks after it: open a contact's fiche — its Conformité reads as
     before for the lawyer's own checks (« le … par Me … »), and the
     section now also shows for a CLIENT of a dossier whatever the contact's
     role. On a TEST dossier with one task, one note and one confirmed
     event — its ONLY client a TEST contact of role « client », whose
     identity step 7 inscribes —, close it in the form: no amber banner (the drain completed);
     then reopen it. Worth telling the lawyer, though it dates from lot 0a
     (the form's version check): a trust entry recorded while a dossier's
     edit form is open in another tab refuses that tab's save with the
     conflict banner — a trust write changes the dossier record: reload,
     then redo.
  3. **`python -m scripts.revoke_mcp_tokens`, and remove the connector in
     claude.ai** — BEFORE pushing 4b. `MCP_WRITE_ENABLED` stays `"true"`.
  4. **Push the lot as ONE deploy** (Cloud Build runs the suite as the
     gate).
  5. **Re-add the connector and READ the new screen before ticking**
     « Autoriser les écritures »: a « Dossiers » paragraph (a status changed
     as the application does — closing or archiving takes the dossier's
     tasks, notes and events off your phone and out of the prescription
     alerts, reopening puts them back and erases the closing date; a drain
     that could not finish is REPORTED and repaired by asking the same
     status again or by « Resynchroniser le téléphone »; a party's roles
     or lawyer corrected, a party DETACHED — a link, never the contact,
     refused for the last client, a served party and a client with trust
     history —, names refreshed) and a « Contacts » paragraph (mandataires
     added, corrected or detached; an identity or conflict check INSCRIBED
     as presumed, « inscrit par Claude le … — à confirmer », counting as NOT
     done until you confirm it, never written over a check you decided). In
     the « jamais » list: « confirmer une vérification d'identité ou de
     conflits d'intérêts, ni modifier une vérification que vous avez décidée
     ou confirmée », and « supprimer » now says that detaching a party or a
     mandataire removes a link — while « changer le statut d'un dossier » and
     « … à la vérification d'identité ou à la vérification des conflits
     d'intérêts » (beside the trust promise — « toucher au fidéicommis »,
     which reads « écrire au fidéicommis ou au registre d'administration »
     when lot 5 rides in the same push) are GONE. Then check
     `tools/list`: **80** tools (31 read, 49 write), and
     `MCP_WRITE_ENABLED=false` hiding the 49 writes — the four new ones
     among them. The byte budget is the deploy gate's
     `tests/test_mcp_descriptor_budget.py` (about 232 KB of its 280 KB cap;
     `update_partie` still the largest, 7.9 KB of 8).
  6. **Check the phone on the wire, THEN on the device** (Change Impact
     item 2 — DavX5 fails silently). On the TEST dossier of step 2 (one
     task, one note, one confirmed event), with curl prompting for the DAV
     password:

     ```bash
     D=the-test-dossier-id
     DAV_USER=you@yourdomain.example   # the AUTHORIZED_USER_EMAIL of app.yaml
     BODY='<?xml version="1.0" encoding="utf-8"?><d:sync-collection xmlns:d="DAV:"><d:sync-token/><d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop></d:sync-collection>'
     report() {
       curl -s -u "${DAV_USER:?}" -X REPORT \
         -H "Content-Type: application/xml; charset=utf-8" --data "$BODY" \
         "https://yourdomain.example/dav/dossier-$D/" > /tmp/report.xml
       echo "live: $(grep -o '<D:getetag>' /tmp/report.xml | wc -l)  removed: $(grep -o '404 Not Found' /tmp/report.xml | wc -l)"
     }
     report   # before: live 3
     ```

     First the DISCOVERY itself (fixes of lot 4 — the root Depth:1 PROPFIND
     reads its dossiers STRICTLY and answers 503 + `Retry-After` on a read
     failure instead of a 207 advertising zero dossier collections; a single
     dossier document it cannot read is skipped and logged, the others
     listed):

     ```bash
     root() {
       curl -s -u "${DAV_USER:?}" -X PROPFIND -H "Depth: 1" \
         -o /tmp/root.xml -w '%{http_code}\n' "https://yourdomain.example/dav/"
       echo "dossier collections: $(grep -o '/dav/dossier-' /tmp/root.xml | wc -l)"
     }
     root   # 207, and as many collections as actif + en_attente dossiers
     ```

     The count must equal the number of `actif` and `en_attente` dossiers in
     the application's list. A 503 (with `Retry-After: 30`) means a read
     failed — the `unexpected` line « dav root propfind read failed » says
     which (`check`); retry, and never read it as « no dossier ». A count
     BELOW the application's is a skipped document: the logs then carry
     `list_dossiers_by_status_strict: document skipped` or `dav root
     propfind: dossier skipped`, each naming the `dossier_id` that left
     discovery. Repair that stored document by hand (a legacy client entry
     without an `id`, a stored `id` that is not the document's, a file
     number or title that is not text); its own collection stays out of
     DavX5 until then.

     Then the probe DavX5 itself makes. When discovery fails — its 503
     included — DavX5 does NOT keep its list on the strength of that answer:
     it re-probes every collection at its own URL (Depth:0) and deletes from
     the phone each one that answers 403, 404 or 410; any other error aborts
     the refresh, retried later (davx5-ose, read 2026-09-29). So the
     collection's own answer is the load-bearing one (review of the fixes of
     lot 4 — it read its dossier fail-open and answered 404 on an outage):

     ```bash
     curl -s -u "${DAV_USER:?}" -X PROPFIND -H "Depth: 0" -o /dev/null \
       -w '%{http_code}\n' "https://yourdomain.example/dav/dossier-$D/"   # 207
     ```

     207 in service. During a Firestore incident it must read **503** (with
     `Retry-After: 30` and an `unexpected` « dav dossier scope read failed »
     naming the id) — never 404, which is a deletion on every phone; a 404
     outside an incident is a dossier that does not exist or a document
     logged « get_dossier_for_dav: document unusable ».

     Then, through Claude: `set_dossier_status` « fermé » on it — the
     result reads `dav.complete: true`, `dav.direction: "drain"`,
     `dav.resources: 3`, with warnings naming what closing does —; `report`
     again: live 0, removed 3; a PROPFIND Depth:1 on `/dav/` no longer lists
     `dossier-$D`. The same call again (same status): `outcome:
     "unchanged"`, nothing written, the phone's view re-applied.
     `set_dossier_status` « actif »: `dav.direction: "restore"`; `report`:
     live 3, removed 0. On the device: refresh DavX5's collection list —
     the dossier's items left after the close and came back after the
     reopening (re-tick the collection if DavX5 dropped it).
  7. **The compliance check, on a TEST client** (never a real one — the
     inscription is a real record on a regulatory field): the test
     dossier's ONLY client, the test contact of step 2 — another client
     with an unverified identity would keep `IDENTITE_NON_VERIFIEE` listed
     after the confirmation, and the check below would read as a failure.
     `record_kyc_status` `check: "identity"`, `status: "vérifié"`: its fiche
     shows the AMBER « Vérifié (présumé) » and « inscrit par Claude le … — à
     confirmer », with NO « par Me … »; `get_coverage_report` (the test
     dossier is actif) still lists `IDENTITE_NON_VERIFIEE`, « Dont 1
     inscrite(s) par Claude ». Click « Confirmer » on the fiche: it reads
     « confirmé le … par Me … (inscrit par Claude le …) », and the finding
     is gone. A second `record_kyc_status` on that check is REFUSED
     (`kyc_lawyer_attestation`).
  8. **The links, on the same test data:** a second test contact added as a
     client (`update_dossier` `add_clients`), its roles set
     (`update_dossier_party` `update`), then detached (`remove`):
     `list_deletions` shows a `dossier_party` row. `update_partie_mandataire`
     `add` then `remove` of a third test contact (an individual of the same
     role) on the first one: a `mandataire` row, whose `title` names the
     contact it REPRESENTED. Then delete the test data in the application —
     the dossier's task, note and event first (a dossier with children
     cannot be deleted), then the dossier, then the contacts (a contact
     still on a dossier cannot be deleted); the journal rows stay, by
     design.

  Then update BOTH copies of the claude.ai skill `pallas-athena` the same
  day. What lot 4 makes false there (on top of the lot 1, 2A, 2B and 3
  lists above) — and one NAME to write as shipped: `update_dossier_party`
  names a party's lawyer `avocat_partie_id`, the key of the party entries
  of `create_dossier` / `update_dossier` — never `avocat_id`, the stored
  field's name, which its schema refuses (the RESULT keeps that name:
  `avocat_id_before` / `avocat_id_after`): every tool count (now 80: 31 + 49 — the synced copies still
  say 49: 27 + 22); SKILL.md « Le `status` choisi à la création d'un dossier
  ne peut plus jamais être changé ici » (`set_dossier_status`); « Les
  écritures qu'aucun outil ne fait : Fermer un dossier; … vérifier une
  identité; vérifier les conflits; gérer les mandataires; … La vérification
  d'identité et la vérification de conflits ne sont inscriptibles par aucun
  outil et ne le seront jamais » — closing a dossier is
  `set_dossier_status`, the mandataires `update_partie_mandataire`, and
  `record_kyc_status` INSCRIBES a check as presumed; what stays true is that
  only the lawyer CONFIRMS one (« une machine ne doit pas attester »
  survives in that form); README « Il ne ferme pas un dossier » and
  « n'atteste aucune vérification d'identité ou de conflits » (true only as
  « it inscribes a presumed one, which only the lawyer confirms »);
  `references/outils.md` — the `create_dossier` row « `status` jamais
  modifiable ensuite », the `create_partie` row « vérif. identité/conflits
  **jamais** inscriptibles », the `update_dossier` warning « `status` est
  délibérément absent — fermer un dossier doit vider sa collection DavX5,
  ce que seule l'application fait », « Vérification d'identité, vérification
  de conflits et mandataires ne sont inscriptibles par aucun outil, et ne
  le seront jamais », the `get_coverage_report` warning « le connecteur ne
  peut créer un protocole, vérifier une identité ni déposer une
  signification » (all three false now), the `list_deletions` row « 15
  `entity_type` » (17 — `dossier_party`, `mandataire`), and the table rows
  « Fermer un dossier — Impossible ici » and « Créer un protocole, vérifier
  une identité, vérifier les conflits, gérer les mandataires — Impossible
  ici, par conception »; `references/vocabulaires.md`
  « `list_deletions.entity_type` (15) »; and, in SKILL.md's « Ajoute »
  row, « Les tableaux de parties sont en ajout seul » (a party link now
  leaves its array through `update_dossier_party` remove — what stays true
  is that `update_dossier` only ADDS). Incomplete rather than false, to
  extend the same day: « Ajoute » names `update_dossier.add_clients` alone —
  say that one party link is corrected or DETACHED through
  `update_dossier_party` (refused for the last client, a served party, a
  client with trust history) and names refreshed with its `refresh_names`;
  family D and the families table lack DOSSIERS and CONTACTS; the
  `get_partie` row should name `*_source` / `*_presumed` /
  `*_confirmed_at`; « Les suppressions » and the row « Supprimer quoi que
  ce soit — Aucun outil ne supprime » should say that a detached party or
  mandataire is a LINK, the contact staying, and that `list_deletions`
  lists those detaches (a `mandataire` row's `title` names the REPRESENTED
  contact); `references/vocabulaires.md` should list the five kinds of
  representation (`mandataire`, `tuteur`, `curateur`, `représentant_légal`,
  `autre`) and the two compliance vocabularies (identity: `non_vérifié`,
  `vérifié`, `exempté`; conflict: `non_vérifié`, `vérifié`,
  `conflit_détecté`). Add: `set_dossier_status`'s discipline (the lawyer's
  confirmation first — closing takes the dossier off his phone and out of
  the prescription alerts; read every warning; `dav.complete: false` → the
  SAME status again, same key — and if that retry is refused « encore en
  cours », wait, then the same key again; if « interrompu », re-read the
  dossier, then the status read under a NEW key — unless the warnings say
  the status moved during the call: then re-read first, and ask for the
  status read under a NEW key; a `refresh_names` that refused a dossier
  is retried the same way — never « the same key is fine » without these
  two exceptions); `record_kyc_status`'s (only on the
  lawyer's instruction, only for a client, never over his own decision —
  `*_presumed` false on a decided status means « tell him »; a detected
  conflict reported to him AT ONCE; notes are appended, never replaced);
  and `update_partie_mandataire`'s (an individual of the same role;
  `update` for a representation already listed); and what « Le dossier
  n'a pas pu être lu — réessayez. » means (fixes of lot 4 — every write
  that names a dossier): the store did not answer and NOTHING was
  written — send the same call again in a moment; never read it as
  « introuvable », and never create a dossier on the strength of it.
- **Lot 5 — accounting through the connector (5a: the model, service and
  web half, an ordinary release; 5b: six tools behind their OWN switch —
  branch `mcp-ecriture-lot5`, with lot 4 and any earlier lot of that stack
  not yet deployed, in ONE push).** **Superseded in part on 2026-10-05**:
  the lawyer removed the switch, the scope and the box that day (« No MCP
  switch (2026-10-05) », below). The switch was never armed — steps 3 to 5
  never ran, and stay as the record of what was planned —; a write token
  reaches the six tools with every other write, **88** tools (32 read, 56
  write), and a read-only one the **32** reads, `get_admin_ledger` among
  them. Steps 0 and 2 (the integrity scripts — run clean before the
  2026-10-05 deploy), 6 and 7 (a supervised pilot on a TEST account, then
  the scripts again — the lawyer's call) and 9 (the skill's disciplines,
  due now — in the claude.ai plugin « athena » 1.1.0, which replaced the
  skill `pallas-athena`: « No MCP switch (2026-10-05) », step 5) still
  serve; step 8 names the break-glass that replaced the
  switches. 5b adds the READ `get_admin_ledger` and the ACCOUNTING family —
  `record_trust_entry` (a trust entry; its
  `virement_honoraires` is the fee payment, trust withdrawal + operations
  recette + the invoice's payment in ONE transaction), `record_admin_entry`
  (a dépense split TPS/TVQ, an other recette, an encaissement that pays the
  invoice in the same transaction, a card payment's two legs),
  `update_admin_entry` (an editable entry, against its etag),
  `clear_register_entries` (≤ 50 entries of one account, at the STATEMENT
  date) and `reverse_register_entry` (the only correction of a trust
  entry, and of an administration entry no longer editable) — all under the
  scope `athena:comptabilite` and its switch `MCP_COMPTABILITE_ENABLED`,
  every rule the MODELS' (the lock floor, the Montréal clock, arts. 57, 58
  and 59, the provision refusal). No index to deploy (the reads reuse the
  existing registers' indexes), no dependency, no Tailwind class, no icon,
  no `cron.yaml` or `firestore.rules` change, and no DavX5 account re-add
  (neither register nor an invoice is DAV-exposed). **The lot SHIPS with
  `MCP_COMPTABILITE_ENABLED: "false"`**, and in that state nothing is
  reachable: the six tools are hidden from every `tools/list` and refused at
  `tools/call`, the box is not offered — and no token in force holds the
  scope (it was never offered before 5b), so the push needs no revocation.
  **In this order** (the lot's own train; if an earlier lot of the stack
  rides in the same push, ITS train's revocation comes first — §15
  « Lot 4 » — since those lots widen `athena:write`):
  0. **Baseline, before the push** (read-only; the environment of the T3
     recipe above): `python -m scripts.verify_trust_integrity` and
     `python -m scripts.verify_admin_integrity`. Exit `0` — or, for the
     trust script only, `2` with every note read with the lawyer (the
     administration script has no notes: every finding it prints is an
     écart, exit `1`) —, admin check nº 8 (`amount_paid` ==
     Σ receipts, over EVERY invoice carrying a payment) clean above all: an
     encaissement the old regime never projected onto its invoice reads
     there as an écart, and 5a's atomic reversal REFUSES when an invoice's
     recorded payment is below the amount it reduces, so such an
     inconsistency must be repaired by hand first.
  1. **Push the lot as ONE deploy with `MCP_COMPTABILITE_ENABLED: "false"`.**
     5a's web half goes live (the fee payment in ONE transaction, its
     « Date du dépôt au compte d'administration », the stale-page refusals
     of the administration edit, « Contre-passer » and « Compenser » — tell
     the lawyer, Phase History « Lot 5 » in `git show abc63b4:CLAUDE.md`), and nothing of 5b is
     reachable: the connector in force still lists **80** tools (31 read,
     49 write), and its `initialize` text says only that accounting tools
     « appear only under the SEPARATE `athena:comptabilite` grant ». The
     byte budget is the deploy gate's `tests/test_mcp_descriptor_budget.py`
     (251 KB of its 280 KB cap, the six counted).
  2. **Run both integrity scripts again, on the deployed version**, before
     anything is armed — `verify_trust_integrity` and
     `verify_admin_integrity`, read-only: exit `0` (the trust script `2`
     with every note read with the lawyer). The connector will write into
     these registers;
     an écart there first is one nobody could later tell from the
     connector's.
  3. **Arm the switch**: `MCP_COMPTABILITE_ENABLED: "true"` in `app.yaml`,
     and deploy. Nothing changes for the token in force: it lacks the scope,
     so it sees no accounting tool and reads the same `initialize` text (the
     box is merely OFFERED from now on).
  4. **Revoke and re-consent, READING the accounting box**:
     `python -m scripts.revoke_mcp_tokens`, remove the connector in
     claude.ai, re-add it, and read the accounting block before ticking
     « Autoriser la comptabilité » (with « Autoriser les écritures »). It
     says what it grants (reading the administration ledger; trust entries
     — their objet agreeing with their sens, a disbursement drawing only on
     CLEARED funds, a fee payment by cheque or transfer only, to the lawyer
     or his firm as « Paramètres » names them, on an Athéna invoice the
     lawyer SENT, addressed to the client whose funds leave, that imputes no
     provision, writing the withdrawal, the recette and
     the invoice's payment in one operation; administration entries, an
     encaissement that pays its invoice; corrections of an EDITABLE
     administration entry; clearing at the statement date; reversal — the
     only correction of a trust entry), the lock/future rule, and its
     « jamais » list — delete an entry, start / complete / abandon a
     reconciliation, create or modify an account or attach a receipt,
     transfer between dossiers or reverse a leg of such a transfer,
     withdraw cash, back a fee payment with a paper, unsent or
     provision-imputing invoice or with another client's invoice (or one
     naming no client, in a dossier of several), make a fee
     payment to anyone but the lawyer or his firm, show a bank number (the
     last two joined on the lawyer's decisions of 2026-09-29 — D21, D23).
     The write block's list
     now says a payment exists only as a register entry, and « écrire au
     fidéicommis » only with that box. Re-consenting while the switch is
     still `"false"` silently yields a grant WITHOUT accounting — step 3
     comes first.
  5. **Verify `tools/list` per scope.** The new token (all three scopes):
     **86** tools (32 read, 54 write), and its `initialize` text carries an
     « ACCOUNTING: » index line and the seven accounting « never » sentences
     (six until D23 added « fee_payee », 2026-09-29).
     The other token shapes were the deploy gate's literal pins then
     (`tests/test_mcp_jsonrpc.py::test_tools_list_counts_per_token_are_the_train_s_checklist`):
     **31** read-only, **80** read + write (or any token while the switch is
     off), **37** read + comptabilité, **32** with `MCP_WRITE_ENABLED` off
     (the reads and `get_admin_ledger`) — since 2026-10-05 it pins two
     shapes, **32** read-only and **88** read + write. To see one on the
     wire, a second authorization without the accounting box must list 80.
     Then call `get_admin_ledger` and `get_trust_snapshot`: no transit, no
     account number, no last 4 digits anywhere in either payload, and the
     balances match « Comptabilité ».
  6. **A supervised pilot on a TEST administration account — never on the
     trust register, never on the real operations account.** Every entry is
     PERMANENT (an administration entry is deletable in the application
     only while unlocked, and a reversal pair never), so the pilot needs an
     account of its own: in the application, « Comptabilité » → new
     administration account, type opérations, named « Essai — connecteur »
     (no transit, no digits). First the refusals, which write nothing: a
     trust disbursement « comptant » (art. 57), a fee payment on a
     brouillon invoice (its refusal names the lawyer as the one who attests
     the sending, and says no way around it — D20), a fee payment naming
     another payee than the lawyer or his firm (D23), an entry dated
     tomorrow. Then, the lawyer watching,
     through the connector and each with its own `idempotency_key` — and
     telling Claude it is a pilot on the test account, since its
     instructions record only movements that happened at the bank:
     (a) `record_admin_entry` — a `dépense` of 1,00 $ on the test account,
     today, any `category` of its list, `ventilation` « sans_taxe » (no tax
     figure: nothing reaches a TPS/TVQ total), a `method` and a
     `counterparty` (both required) — the account's balance reads
     **−1,00 $**, the entry « en circulation »; (b) `clear_register_entries`
     (`register: admin`) on it at today's date, with the `etag` its
     `get_admin_ledger` row shows in `expected_etags` (required at admin
     since the finitions: an entry edited after that read is refused, never
     cleared at its new amount) — still **−1,00 $**, the
     entry « compensée » (clearing moves no balance: the ledger balance
     counts every status); (c) `reverse_register_entry` on it, with a
     `reason` — a cleared entry's reversal enters « en circulation », a
     real movement to come, and the balance reads **0,00 $** AT ONCE (the
     reversal counts from its creation); (d) `clear_register_entries` on
     the reversal — still **0,00 $**, now with nothing outstanding. A
     balance other than these after any step is an écart: stop there.
     After each step, open the entry in the application (its status, the
     test account's balance) and read it back with `get_admin_ledger` —
     its `created_via` / `cleared_via` read `"mcp"`, and the entry's page
     in the application shows « inscrite par Claude » / « compensée par
     Claude » beside its status (and « · par Claude » on a revision the
     connector made). Then close the test account
     (« Fermé ») in the application; its TWO entries (the dépense and its
     reversal) stay in the ledger for good, netting to zero.
  7. **Re-run both integrity scripts** (read-only): exit `0` (the trust
     script `2` with the notes read) — the test account's two entries
     among what the administration script checks (Σ = 0, the pair
     symmetric). Only then may the first REAL bank movement be recorded
     through the connector — the lawyer's, at its statement date.
  8. **Incident**: since 2026-10-05 nothing stops the six tools alone — the
     switches are gone. `python -m scripts.revoke_mcp_tokens` stops every
     write at once, the accounting ones included, and every read within
     the bearer cache's five minutes; the connector stays off until it is
     authorized again. (Until then, `MCP_COMPTABILITE_ENABLED: "false"` and
     a deploy stopped the six tools only, `MCP_WRITE_ENABLED: "false"`
     every write.)
  9. **Then update BOTH copies of the claude.ai skill `pallas-athena` the
     same day.** What lot 5 makes false there (the synced copy of
     2026-09-17, `references/comptabilite.md` and `SKILL.md`):
     « Le fidéicommis est en lecture seule intégrale. Aucun outil n'y
     écrit. » (comptabilite.md §6); « Le connecteur ne change aucun statut
     de facture et n'enregistre aucun paiement » (§7, rule 5 — its status
     half false since lot 3 already); « enregistrer un paiement » among
     « Les écritures qu'aucun outil ne fait » (SKILL.md); the family « F.
     Comptabilité / fidéicommis » listing three READ tools only
     (`references/outils.md`); and every tool count (« 49 outils »,
     « 27 en lecture, 22 en écriture » — 86 = 32 + 54 under the accounting
     grant and 80 without it when lot 5 shipped; **88** = 32 + 56 for every
     write token since 2026-10-05). Each was true WITHOUT the accounting
     grant only — and that grant left with its scope on 2026-10-05: every
     write token reaches the accounting tools —, and the connector's own
     texts refuse those phrasings
     (`tests/test_mcp_disclosure.py` `KNOWN_FALSE_CLAIMS`). Add the
     discipline: record only a movement that HAPPENED at the bank, at the
     date it happened — the tools' own rule; a cheque just written is one,
     recorded en circulation before any statement shows it; one
     `idempotency_key` per movement, kept on a retry; an uncertain outcome
     (`accounting_outcome_uncertain`) is re-read (`list_trust_transactions`,
     `get_admin_ledger`) before anything else and retried only with the
     SAME key — and so is a reversal, a clearing or a correction answered
     « Erreur lors de … » (or refused as already reversed, already
     cleared, or changed since the read, right after such an answer): its
     commit may have landed with the answer lost, so it is never reported
     to the lawyer as failed before the register says so (a retry cannot
     apply it twice — OBSERVABILITY.md, `accounting_refused`); clear only
     at the statement date; a trust mistake is
     REVERSED, never re-entered on top, and an administration mistake is
     corrected with `update_admin_entry` while the entry stays editable;
     never a paper, unsent or provision-imputing invoice for a fee payment
     — nor another client's invoice, nor one naming no client in a
     dossier of several (D21), nor a payee other than the
     lawyer or his firm as « Paramètres » names them (D23, art. 58 — either
     name for either method; omitted, the lawyer on a cheque — art. 58
     draws a fee cheque « à l'ordre de l'avocat » —, the firm on a
     transfer); an invoice is marked « envoyée »
     only on the lawyer's word
     (D20 — the fee payment trusts that status); and an objet always agrees
     with its sens (D24 — the model refuses otherwise).
- **Finitions — the adversarial review of lots 0a-5 (branch
  `mcp-ecriture-finitions`, on top of `mcp-ecriture-lot5`, in the SAME push
  as the stack it sits on).** No train of its own: no scope, no switch, no
  tool (still **86**), no index, no dependency, no Tailwind class, no icon,
  no `cron.yaml` or `firestore.rules` change, no DavX5 account re-add. Its
  connector changes ride the ONE revocation of §15 « Déploiement unique
  (D22) » (step 5; the accounting ones were to reach a token only at its
  step 17 — since 2026-10-05 they reach the write token itself):
  INSTRUCTIONS open on a SAFETY CORE that a client cutting at
  2 048 characters reads whole (the « never » list, the one outbound
  effect, confirm-before-writing, idempotency and etag, re-read before a
  retry) and, since the context-cost lot (2026-09-30), the CONVENTIONS
  within the same 2 048 characters (money, dates, ids, the provenance the
  server stamps), then ONE index line per family naming its tools — the
  family prose moved into the tool descriptions, and the text weighs at
  most 8 000 bytes for every token where it weighed 22-26 KB; every tool
  description fits the same cut, a tool result's text block is COMPACT
  JSON (its `structuredContent` unchanged), and the property texts every
  write repeats (`idempotency_key`, `expected_etag`, `phase` /
  `sous_phase`) are concise — no tool, scope, input contract or
  outputSchema moved: those texts need no revocation, the next
  `initialize` and `tools/list` carry them;
  `clear_register_entries` takes
  `expected_etags` (REQUIRED at the administration register);
  `record_document_analysis` takes an optional `expected_etag` and returns
  `entity` {id, etag}; `list_trust_transactions` rows gain ten optional
  keys (account, dossier, client, reference, description, invoice, reversal
  links, provenance); four write outputs require LESS than before (a
  same-key replay stored by the previous release still conforms); a write
  whose record could not be read answers `read_unavailable`, never
  « introuvable »; a create whose commit raised and whose read-back failed
  too answers `write_outcome_uncertain`, the key kept; and — the lawyer's
  decisions of 2026-09-29 — `record_trust_entry` refuses a fee payment's
  `counterparty` other than the lawyer or his firm (D23; omitted, the
  lawyer on a cheque, the firm on a transfer)
  and another client's invoice — or, in a dossier of several clients, one
  naming none (D21) —, its refusal of an unsent invoice
  names the lawyer as the one who attests the sending (D20), and the
  accounting INSTRUCTIONS carried a seventh « never » (`fee_payee`) — all
  under the accounting grant, which no production token ever held (its
  switch was never armed): since 2026-10-05 they are the write grant's,
  and the one INSTRUCTIONS text every token reads carries that « never ».
  And D25 (the same day, under the ordinary write grant):
  `record_document_analysis` KEEPS a category the lawyer chose or
  confirmed — a confirmed analysis included —, the new analysis itself
  always PRESUMED (his confirmation of the previous one covered that one
  only, and is recorded, never carried over), and
  records the category the sub-nature derives beside a
  `divergence_categorie` flag and a warning; `list_documents` rows gain
  `divergence_categorie`; `update_document` judges the lawyer's rule
  before the analysis rule (« dites-le au juriste »). When BOTH copies of
  the claude.ai skill `pallas-athena` are updated on the day of the push
  (§15 « Déploiement unique (D22) », step 15 — D25 rides the ordinary
  write grant, so it does NOT wait for the accounting lists of « Lot 5 »
  step 9, deferred to step 17), add: an analysis no longer replaces the lawyer's category — a
  phrasing « l'analyse remplace la catégorie » (or « an analysis
  replacing it », the lot 2A wording above) is true of a PRESUMED
  category only; a gap is reported to him, never settled by a new
  analysis. The claude.ai skill « Analyse documentaire » is GENERATED, not
  edited: re-export it at that same step 15 (`python -m
  scripts.exporter_competence_analyse`) and re-paste it — its « Il
  **remplace** la catégorie stockée » bullet was unconditional until the
  review of D25. **Device-visible — tell the lawyer, then check after the
  deploy:**
  1. A DAV read that FAILS answers **503 + `Retry-After`** — a collection
     listing, a sync report, a contact, an event, a task, a note, a
     DELETE —, where it answered an empty 207, a 404, a 412, a 422 or a
     500. DavX5 retries; it deletes nothing. No outage can be staged in
     production, so check the ordinary path: `curl` a PROPFIND Depth:1 on
     one active dossier collection and on `/dav/addressbook/` — 207, every
     member listed —, then a phone sync that pulls one edit made in the
     application.
  2. A CONFIRMED Bookings rendez-vous: « ignorer » / « conserver » a
     divergence in Réception, then edit that event on the phone — it
     uploads (before, the phone's next edit answered 412 and was lost).
  3. On the phone, type a line AFTER the « Dossier: / Type: / Modalité: »
     block of an event's description, and after a task's « Dossier: »
     line; sync, then open both in the application: the notes and the
     description show the typed line and NO metadata block. Move an event
     from one dossier calendar to another on the phone: the new event's
     notes carry no block either.
  4. The web, on a read that fails: a note edit re-renders the form with
     the typed text and « n'a pas pu être lu — réessayez » (it used to
     return to the list, the edit lost); a task or hearing edit refuses
     the same way; a deletion deletes nothing and never tombstones
     « Général » (a task's or hearing's refused deletion still returns to
     the list without a banner, as before — the item is simply there).
  5. The trust entry form (« Fidéicommis » → « Nouvelle écriture »),
     the lawyer's decisions of 2026-09-29: the « Objet » list no longer
     offers « Virement inter-dossiers » (the « Virement inter-dossiers »
     screen stays the way); choosing « Dépôt du client » sets the sens to
     « Recette » (and « Remise au client » to « Déboursé »); on « Paiement
     d'honoraires » the « Bénéficiaire » becomes a choice between the two
     names « Paramètres » holds, the LAWYER preselected (the form opens on
     « chèque », and art. 58 draws a fee cheque « à l'ordre de l'avocat »;
     the firm stays selectable on either method — the rule the lawyer
     settled on 2026-09-29 (D23, clarified the same day): EITHER name is
     accepted for EITHER method, cheque or transfer, the lawyer's name
     being only the DEFAULT on a cheque) — check the names there FIRST: a
     profile naming neither refuses every fee payment. No real entry is
     needed to check it: an incoherent pair, another client's invoice or —
     in a dossier of several clients — an invoice naming none (D21) is
     refused and writes nothing. Then `python -m scripts.verify_trust_integrity`
     (read-only): check 8's new NOTES (a single transfer leg, an incoherent
     objet/sens, a fee payment for another client's invoice, a payee
     outside the profile) describe what the register already holds — read
     them with the lawyer; nothing is repaired.
  6. A document whose category the lawyer chose or confirmed, analysed
     again through the connector (D25): its category stays. When the
     analysis derives another category, the fiche's « Analyse » card
     shows, in amber, « L'analyse suggère la catégorie X ; la vôtre est
     conservée ». The analysis itself reads « Présumée », its alerts shown
     and « Confirmer » offered, even when he had confirmed the previous
     one — the card's footer says « Analyse précédente confirmée le … »:
     tell him that his confirmation covered the previous analysis, not
     this one, and that he re-reads it, then confirms it. And in the edit form, correcting an analysis
     field other than the sous-nature no longer replaces the category
     (changing the sous-nature still re-derives it).
  Watch, the first week: the finitions' `unexpected` messages
  (OBSERVABILITY.md, « Messages of the finitions ») — a burst is an outage,
  a steady line on one id a stored document to repair.
- **Bulk creators — `create_time_entries_bulk` / `create_expenses_bulk`
  (2026-09-30, approved by the lawyer — ONE consent train of its own,
  after the D22 release).** Two new write tools under `athena:write`, in the
  CREATE family: the batch forms of `create_time_entry` and
  `create_expense`, up to 50 rows a call (`mcp.tools.ENTRY_BULK_MAX`),
  **ALL OR NOTHING** — one refused row refuses the whole call and names
  every bad row `entries[i]` (0-based; a field the schema refuses reads
  `entries[i].hours`); the rows are written in ONE
  Firestore batch — with `idempotency_key` REQUIRED (money: a retry
  without it would be a second batch the next invoice sweeps). One import
  had made 61 separate `create_time_entry` calls. What ships: **82** tools
  under the write grant (31 read, 51 write), **88** with the accounting
  grant (32 read, 56 write); no scope, no switch, no index, no TTL, no
  dependency, no Tailwind class, no icon, no `cron.yaml` or
  `firestore.rules` change, no DavX5 account re-add (time entries and
  disbursements are not DAV-exposed). **A new write tool reaches the token
  in force the moment the deploy lands** — there is no code gate (D19,
  §11) — so this release runs the D19 train, in this order:
  1. *Before revoking*: run the suite on the commit to push
     (`python -m pytest tests/ -q -p no:cacheprovider`, from `athena/`) —
     a red build after the revocation leaves the connector down.
  2. *Revoke and disconnect — BEFORE the push*:
     `python -m scripts.revoke_mcp_tokens`, and remove the connector in
     claude.ai. Pause any scheduled Claude job for the deploy window (§11,
     last bullet). `MCP_WRITE_ENABLED` stays `"true"`.
  3. *The push*; Cloud Build runs the suite as the gate.
  4. *Re-add the connector and READ the CREATE paragraph before ticking
     « Autoriser les écritures »*: time entries and disbursements are now
     created « un à un ou par lots d'au plus 50 — un lot s'écrit en entier
     ou pas du tout ». Re-consenting while `MCP_WRITE_ENABLED` is `"false"`
     would yield a read-only grant without a word. A token that also held
     `athena:comptabilite` is re-granted by ticking « Autoriser la
     comptabilité » too, after reading its block.
  5. *Verify `tools/list`*: **82** tools (31 read, 51 write) for a write
     token — **88** (32 read, 56 write) for one that also holds
     `athena:comptabilite` while `MCP_COMPTABILITE_ENABLED` is `"true"`;
     both new tools with `destructiveHint: false`, `idempotentHint: false`
     and `idempotency_key` in their schema's `required`; the `initialize`
     CREATE line names them.
  6. *One smoke test, on a TEST dossier only* — the connector can never
     delete what it creates: `create_time_entries_bulk` with two rows, the
     second invalid (hours `0.333`) — refused, naming `entries[1]`, and
     `list_time_entries` on the dossier shows NOTHING new (hours `30`
     instead, a schema fault, is refused naming `entries[1].hours`); the
     two rows corrected, under a NEW key — two entries, in order; the
     SAME call again — `idempotent_replay: true`, still two. Then delete
     both test entries in the application.
  7. Update BOTH copies of the claude.ai skill `pallas-athena` the same
     day: the counts read 82 (31 + 51), 88 with the accounting grant, and
     an import of time or disbursements goes through the bulk creators (the
     key required, 50 rows a call, a refused batch resent WHOLE).
- **Arborescence par défaut (2026-09-30) — the default folder tree of every
  dossier (branch `arborescence-par-defaut-main`, on top of the D22 release
  and of the bulk creators above; ONE consent train — the SAME one as the
  bulk creators' when both ride one push).** Every dossier receives eighteen filing folders, seven of
  them the application's own (« Mandat » with « Factures » and « Déboursés »,
  « Interne » with « Projets », « Autres » with « Reçus du portail » — never
  renamed or moved); the note d'honoraires is filed in « Mandat ›
  Factures », the copy of a dépense's pièce justificative in « Mandat ›
  Déboursés »; an EMPTY dossier deletes with its empty folders. No tool is
  added (still 82 under the write grant, 88 with accounting — the bulk
  creators' counts), no index, no
  dependency, no Tailwind class or icon, no `cron.yaml` or `firestore.rules`
  change, no DavX5 account re-add (folders and documents are not
  DAV-exposed). But the consent text changes under a frozen scope — the
  CREATE block (« chaque nouveau dossier reçoit l'arborescence de
  classement par défaut ») and its checkbox summary, the FILES block (the
  seven application folders, « Projets » under « Interne », the note in
  « Factures » under « Mandat ») — hence the train (§11, D19). Existing
  dossiers get the tree from `scripts/arborescence_par_defaut.py`, which
  runs the engine of the Fichiers tab's « Créer l'arborescence par
  défaut » button (`models/folder.ensure_default_tree(relocate=True)`). In
  order:
  1. **Before the push, from a checkout of THIS branch, the DRY-RUN only,
     against production** — the script ships with the change it prepares,
     as `designer_gabarits_actifs` does (the recipe of « Active gabarits »
     above: Application Default Credentials, the variables inline, never
     `ENV=production`, which would make `config.py` resolve the service's
     secrets; `GOOGLE_CLOUD_PROJECT` stops the client from inferring another
     project). Unlike that script this one READS a `.env` (`find_dotenv`,
     from the working directory upward) — it never overrides a variable
     given inline, but it fills the ones left unset: make sure no `.env` on
     the way up points at another project, and read the two header lines
     it prints (« Projet Firestore : … », « Fichier .env lu : … »):

     ```bash
     cd athena
     export GOOGLE_CLOUD_PROJECT=$PROJECT FIREBASE_PROJECT_ID=$PROJECT \
       FIREBASE_STORAGE_BUCKET=your-bucket-name \
       AUTHORIZED_USER_EMAIL=you@example.com SECRET_KEY=unused-by-the-script
     # (the original deployment: PROJECT=athena-pallas)
     python -m scripts.arborescence_par_defaut          # simulation — writes nothing
     ```

     Expect « SIMULATION — ce que --apply ferait ; rien n'est écrit », one
     line per dossier, a closing « Dossiers traités : N · en échec : 0 · … »
     where N is the production dossier count, and exit `0`. Exit `1` means
     a read failed, a dossier could not be treated, a folder of the tree
     could not be placed (its reason is printed under the dossier — « [!]
     bloqué : … ») or no dossier was read at all (the wrong project): stop,
     and look. The output is the ONLY record of a run — the engine's events
     do not reach Cloud Logging from a script —: keep it.
  2. **Read that output WITH the lawyer, before the push** — after the push
     the first generation, versement or receipt copy that needs a system
     folder adopts it with no review at all (`models/folder.py`: its module
     docstring and `ensure_system_folder`). Three kinds of line: « « X »
     existant sera le dossier de l'application (ne se renommera plus, ne se
     déplacera plus) » — a folder he made, e.g. a ROOT « Mandat » or
     « Autres », becomes a locked application folder; « « Projets » sera
     rangé sous « Interne » » / « « Reçus du portail » sera rangé sous
     « Autres » » — the old root system folders move, their documents with
     them; « à vérifier : « X » (…) porte le nom d'un dossier de
     l'arborescence sans en être un » — a homonym out of place (a root
     « Déboursés » of his own…), listed by folder name only, never merged:
     he looks. « repris : N » counts his own ordinary folders already
     bearing a default name, kept as they are, nothing written. Anything he
     wants otherwise is renamed or moved in the application BEFORE the push
     (step 4), and the simulation re-run — not merely before step 6: from
     the push on, the first generation, note d'honoraires, versement or
     receipt copy that needs « Mandat », « Interne » or « Autres » stamps
     his root folder of that name, and it can no longer be renamed or
     moved.
  3. **Suspend the scheduled Claude tasks, then `python -m
     scripts.revoke_mcp_tokens` and remove the connector in claude.ai —
     BEFORE the push** (§11: that manual sequence is the only control, D19;
     the consent text changed in the CREATE and FILES blocks).
     `MCP_WRITE_ENABLED` stays `"true"`.
  4. **Push ONE deploy.** The commits of `main` not yet in production ride
     along — `d8de4e5` (the procedural holiday calendar, art. 82 C.p.c.),
     `935eb0a` (the connector's context savings) and `30f1ead` (the bulk
     creators, whose own train this one then IS: follow its checks too —
     `tools/list` → 82 / 88); the « aucun re-consentement » of `d8de4e5` is
     true of it alone and covers neither. Cloud Build runs the suite as the
     gate.
  5. **Confirm the backup before writing to every dossier**: Point-in-Time
     Recovery or a recent scheduled backup of the `(default)` database
     (read-only, e.g. `gcloud firestore databases describe
     --database='(default)' --project=$PROJECT` —
     `pointInTimeRecoveryEnablement`; `gcloud firestore backups schedules
     list --database='(default)' --project=$PROJECT`). The backfill has no
     undo of its own.
  6. **`--apply` ONCE, after the push — never before it**: the OLD code
     counts folders among the children that block `delete_dossier`, so a
     tree written under it would make every dossier undeletable until the
     deploy. Same environment as step 1:

     ```bash
     python -m scripts.arborescence_par_defaut --apply   # « ÉCRITURE »
     ```

     Each dossier's line says what was created, adopted and moved; exit
     `0`. Keep this output too.
  7. **Re-run the dry-run**: every dossier line ends « (à jour) » and the
     exit is `0` (a « [?] » line under a dossier means its counts may be
     incomplete — a lost answer, or another writer in between —: the
     re-run's « (à jour) » is what settles it). The « à vérifier » lines
     stay: nothing merges homonyms. Then open THREE dossiers in the
     application, picked from the step-6 output: one that had a root
     « Projets » (it now sits under « Interne », its documents inside, none
     left at the root); one with a root « Déboursés » of the lawyer's (still
     at the root, untouched, beside « Mandat › Déboursés », and listed « à
     vérifier »); and one « fermé » dossier (the tree is there — the
     backfill treats every status).
  8. **Re-add the connector and READ the rewritten CREATE and FILES blocks
     before ticking « Autoriser les écritures »** (and the accounting box,
     if its switch has been armed since — §15 « Lot 5 »). `tools/list`:
     **82** tools for a grant without accounting, **88** with it — the bulk
     creators' counts (production served 80 before this push); this lot
     adds none. Say it to the lawyer at this step: from this release, the
     copy of a dépense's pièce justificative filed in « Mandat ›
     Déboursés » is an ordinary document of the dossier — a firm-level
     receipt no tool could reach before. Under the READ grant alone the
     connector lists it and reads its text (`list_documents`,
     `get_document_text`), so a supplier's invoice or a bailiff's note now
     transits claude.ai when Claude is asked about the dossier's files.
  9. **Pilot on a TEST dossier** (web and connector; delete the test data
     at the end, in the order the guards allow):
     a. Create it in the web form: the Fichiers tab shows the five root
        folders — « Autres », « Correspondance », « Interne », « Mandat »,
        « Procédures » (alphabetical) — and, opened, the 18; « Renommer »
        is absent from the seven application folders' menu, present on
        the others; no « Créer l'arborescence par défaut » button (nothing
        left to do).
        a'. A SECOND test dossier through the connector (`create_dossier`,
        an obviously test file number, the client of step a — a contact on
        two dossiers is fine): its result carries no warning naming « Créer
        l'arborescence par défaut », and its Fichiers tab shows the same 18
        folders (the connector writes the tree AFTER the dossier's commit,
        under the same idempotency key — the one path step a does not
        exercise). It stays empty: delete it in the application at the end,
        with the first.
     b. `fill_gabarit` on it: the result's folder reads `system_role:
        "projets"` and `path: "Interne / Projets"`; the document opens in
        Word WITHOUT repair.
     c. « Note d'honoraires (Word) » on an EXISTING, non-annulée invoice
        (never « Créer » a test invoice: a number is consumed for ever): the
        success fragment says « sous « Mandat › Factures » », and the note
        is there in that invoice's dossier; no invoice is created and
        `counters/invoices-{year}` does not move. Open it in Word, then
        delete that one draft in the application.
     d. A test « dépense » in « Administration », linked to the TEST
        dossier, on a period no reconciliation locks, with a pièce
        justificative: after the upload the entry page reads « Copie au
        dossier …, sous Mandat › Déboursés », and the copy is in the test
        dossier's « Mandat › Déboursés » (the receipt itself stays on the
        entry).
     e. Try « Supprimer » on the test dossier while it still holds those
        documents: refused ON the dossier page (red banner), nothing
        deleted. Then delete the documents of b and d in the application,
        and the test entry in « Administration » (an unlocked entry
        deletes, journaled — an administration entry never blocks a
        dossier's deletion). Once the dossier is EMPTY: « Supprimer »
        deletes it WITH its folders and the list opens; `list_deletions`
        (connector, read) shows ONE `dossier` row for it and no `folder`
        row.
  10. **Later repairs**: one dossier with `--dossier ID` (`--apply`), or its
      Fichiers tab's « Créer l'arborescence par défaut » (offered only
      while something is missing or misplaced). A FULL `--apply` re-run
      recreates, at their deterministic ids, every default folder the lawyer
      deleted since, the ordinary ones included (« Transcriptions » he had
      no use for) — prefer `--dossier`.
  11. **Tell the lawyer** what changed on the web — the Phase History of
      `git show abc63b4:CLAUDE.md`, « Arborescence par défaut des dossiers
      (2026-09-30) », « Changements web à annoncer ». Watch the first week:
      `default_folder_tree_incomplete`, `generation_failed` with
      `factures_unavailable`, `admin_receipt_filed` refused (OBSERVABILITY.md).
  12. **Update BOTH copies of the claude.ai skill `pallas-athena`** where
      they describe `create_dossier` (a new dossier now comes with its
      filing tree — a warning, never a failure, when it could not be
      created: never re-create the dossier) or say the note d'honoraires
      goes to « Projets » (it goes to « Factures » under « Mandat »;
      « Projets » is under « Interne »), and the seven folders
      `manage_folder` never renames or moves.
- **No MCP switch (2026-10-05) — the three switches and the separate
  accounting scope removed, by the lawyer's decision.** `MCP_ENABLED`,
  `MCP_WRITE_ENABLED` and `MCP_COMPTABILITE_ENABLED` are gone from the code
  and from `app.yaml`, and with them the `athena:comptabilite` scope and its
  box « Autoriser la comptabilité ». The ACCOUNTING family
  (`record_trust_entry`, `record_admin_entry`, `update_admin_entry`,
  `clear_register_entries`, `reverse_register_entry`) is a write family like
  the others, under `athena:write`, and `get_admin_ledger` an ordinary read
  under `athena:read`. The consent screen offers ONE write box, always —
  its block now carries the accounting paragraph, and its « jamais » list
  the register promises (no entry deleted, no reconciliation or account, no
  transfer between dossiers, no cash withdrawal, the fee-payment invoice
  and payee rules, no bank number) —, and its read paragraph names the
  administration ledger. INSTRUCTIONS are ONE text for every token. A value
  of the three left in a yaml or a `.env` is read by nothing: an
  `MCP_ENABLED=false` in a local `.env` no longer turns the connector off.
  No tool added (the registry's 88), no index, no dependency, no Tailwind
  class or icon, no `cron.yaml` or `firestore.rules` change, no DavX5
  account re-add (neither register is DAV-exposed). In order:
  1. *Before the push*: both integrity scripts, read-only
     (`python -m scripts.verify_trust_integrity`,
     `python -m scripts.verify_admin_integrity` — « Lot 5 », step 0): the
     connector is about to write into these registers, and an écart found
     first could not later be told from its own. They ran clean against
     production on 2026-10-05, before this release (trust: no écart, 13
     notes on past entries for the lawyer; administration: clean); run
     them again if the registers have moved since.
  2. *The push — without the §11 train, by the lawyer's explicit choice.*
     The token in force, `athena:read athena:write`, was granted under a
     consent screen that PROMISED the opposite of this release: its
     « jamais » list said the connector would never write to the trust
     register or the administration ledger, nor record a payment (the
     box's summary ended « jamais de paiement »), and its INSTRUCTIONS
     said the accounting tools appear only under the separate
     `athena:comptabilite` grant. It reaches the five accounting writes
     and `get_admin_ledger` the moment the deploy lands — there is no code
     gate (D19, §11) —, so deploying without the §11 train lifts those
     promises for that token. On 2026-10-05 the lawyer chose exactly that
     (« skip the runbook »): no revoke, no re-consent for this release. To
     put the grant back in step with the screen, run the §11 train at any
     time: revoke, remove the connector, re-add it, tick the box under the
     new text.
  3. *Verify `tools/list`*: **88** tools (32 read, 56 write) for the token
     in force; a read-only grant lists the **32** reads, `get_admin_ledger`
     among them. The `initialize` text — the same for every token — reads
     « TOOLS: 32 tools read; 56 write, in 13 families: », an
     « ACCOUNTING: » index line among them, and states the register
     « never » sentences after the index.
  4. *The first accounting write through the connector* is the lawyer's to
     supervise: « Lot 5 », steps 6 and 7, are the recipe (a TEST
     administration account, never the trust register; then both integrity
     scripts again). Only a movement that HAPPENED at the bank is recorded.
  5. *The claude.ai plugin « athena »* (`plugin/athena/`) moved to
     **1.1.0** with this release — its accounting recipe
     (`skills/athena/recettes/comptabilite.md`) no longer says the tools
     exist only under a separate « Comptabilité » authorization: rebuild it
     and unpack it into the mirror repository `athena-plugin` (from
     `athena/`, the old copy removed first: `python -m
     scripts.exporter_plugin_athena --deplie
     ../../athena-plugin/plugins/athena`), bump the version in the mirror's
     `.claude-plugin/marketplace.json`, commit and push it, the same day.
     That push IS the delivery: claude.ai syncs the plugin from that GitHub
     repository (`jpoirierlavoie/athena-plugin` — an organization setting),
     and Claude Code from claude.ai; nothing is uploaded by hand, and a
     GitHub release carrying the archive is optional (a download point).
     The plugin's own README still said to upload the archive: 1.1.1, the
     same day, says this instead. The skill `pallas-athena` it replaced is
     removed, never updated.
  6. *The break-glass, from now on*: `python -m scripts.revoke_mcp_tokens`
     (« Emergency switches », in « Déploiement unique (D22) ») — nothing
     stops the writes, or the accounting tools, alone any more. A rollback
     past this release brings the switches back with the older version's
     own `app.yaml`, the accounting tools hidden again.
- **Register findings: keys and reviews (2026-10-06).** Both integrity
  scripts now print every finding with a key in brackets, and take
  `--revue FICHIER`, a JSON list of `{"cle", "revu_le", "motif"}` naming the
  findings the lawyer has reviewed. Keep that file with the signed memo in
  the trust records, never in this repository (it names entries and his
  reasons). A reviewed finding still prints, under « Constats déjà revus »,
  and no longer counts toward the exit code; the key binds the entry's
  stored document, so an entry changed since brings its finding back (the
  stale review is listed). A balance that does not add up can never be
  marked reviewed, and an unreadable file is an écart. New checks, read-only
  like the rest: trust **11** (a date-only `date` or `cleared_date` carrying
  a time of day — a NOTE, the trace of an edit outside the application),
  trust **12** (every number from 1 to the account's counter is on an entry
  — an ÉCART: the application never deletes a trust entry; a gap may be
  marked reviewed once explained, a counter behind the highest number
  never), administration **10** (the same time-of-day NOTE). The art. 38
  sheet orders its rows by calendar day, then sequence
  (`models/trust.register_order`): sorted on the full timestamp, an entry a
  console edit left at 7 PM printed after the rest of its day, its frozen
  balance out of place. On 2026-10-06 production read: trust exit `1` (the
  gap 96–97) with 14 notes, administration exit `2` with 4 notes.
- **Rectifying a register entry outside the models (2026-10-06).** The trust
  register is append-only and a reconciled administration entry is locked,
  so the application corrects neither a payee typed wrong nor a date's hour.
  **Never do it in the Firestore console**: the console edits in local time,
  so a correct date-only value (midnight UTC) shows there as 8 PM the
  evening before (7 PM in winter) — it LOOKS a day early — and « fixing » it
  stores an evening hour; the console also lets a creation instant be
  overwritten, and a document be deleted, with no trail in the application
  (the Data Access audit log keeps the call for 30 days, never the value it
  replaced). Point-in-time recovery showed it on administration entries 214
  and 215: their dates were right before the console edits of 2026-09-30.
  Instead, from
  `athena/`, with the environment of « Active gabarits » above (ADC, the
  variables inline, never `ENV=production`):

  ```bash
  python -m scripts.rectify_registers PLAN.json               # prints, writes nothing
  python -m scripts.rectify_registers PLAN.json --appliquer   # writes, all or nothing
  ```

  The plan (a JSON file kept outside this repository, like the review file)
  lists changes `{registre, ecriture, champ, avant, apres, motif}`; each
  states the stored value it expects, so a plan refuses itself when the
  entry changed since. It reaches only: a trust `counterparty` (on a fee
  payment, only a name of the firm profile — D23 binds a repair too), a fee
  payment's `invoice_external_ref`, a `date` or `cleared_date` brought to
  midnight of the SAME day (never another day), and an administration
  entry's `created_at` (no later than its `updated_at`). Every document gets
  the stamps and a `revisions` trail entry, and all of them go in one batch
  guarded by the time each was read. Run both integrity scripts afterwards.
- **Administration natures outside the results (2026-10-07).** The
  administration ledger gains four kinds: the lawyer's `prélèvement` and
  `apport` (a sole practitioner's own money) and `virement_interne_sortant`
  / `virement_interne_entrant` (the firm's money to or from an account
  outside the ledger — the trust account, a savings account). They move,
  clear and reconcile like any entry, carry no category, no invoice and no
  tax split, and no revenue, expense or TPS/TVQ total counts them
  (`admin_ledger.results_statement`, the one authority; `owner_equity` for
  the « Avoir de l'avocat » block of the account page — neither the
  journal header nor the PDF journal shows it, by the lawyer's decision).
  The web form shows ONE « Virement interne » with a « Sens »; the four
  read as plain text, like every kind; the CSV export gains a « Nature »
  column. The connector's
  `record_admin_entry`, `update_admin_entry` and `get_admin_ledger` know the
  four kinds (an edit moves only within one sign, plus the older
  dépense ↔ autre recette switch) — still 88 tools, no new output key. No
  index, no dependency, no Tailwind recompile (the new blocks reuse
  compiled classes), no DAV account re-add. `verify_admin_integrity` gains check 11
  (an unknown kind, or one of the four carrying a category, a tax, an
  invoice, a trust link, or a dossier on a prélèvement or an apport) and
  check 4 compares the two card legs' UTC days. In order:
  1. *Before the push*: both integrity scripts, read-only, from this
     release's checkout — a guard for the deploy. The reclassification's
     own baseline is the run made just before its `--appliquer`.
  2. *The push — without the §11 train, by the lawyer's explicit choice
     (2026-10-07)*: the token in force reaches the four kinds and the new
     passages the moment the deploy lands. Running the §11 train at any
     time puts the grant back in step with the consent text, which now
     names the four kinds.
  3. *The claude.ai plugin « athena »* moves to **1.2.0** (its accounting
     recipe names the kinds, and the registry fingerprint changed): rebuild
     it, unpack it into the mirror repository `athena-plugin` (the old copy
     removed first), bump the mirror's `.claude-plugin/marketplace.json`,
     commit and push it the same day.
  4. *Then, and only once this release serves*, the reclassification below.
     A rollback after it restores the data FIRST (`--restaurer`), then the
     code: the older code refuses to edit the four kinds (`type_invalide`)
     and shows their raw codes. Under the older code, never REVERSE an entry
     of the four kinds recorded since this release: its reversal would copy
     the split and stamp no `reverses_kind`. Delete it while it is unlocked,
     or wait for the roll-forward; check 11 names such a reversal after it.
- **Reclassifying the administration journal (2026-10-07).** The one-shot
  `scripts/reclassify_admin_ledger.py` writes, OUTSIDE the models like
  `rectify_registers`, what the lawyer's audit of the operations account
  decided, line by line from a CSV kept outside this repository with its
  backup and its report: the kind of an entry (to one of the four kinds
  above, never across signs), a category and a dossier, a counterparty,
  and the date of a card payment's two legs or of an entry within the same
  month. From `athena/`, with the environment of « Active gabarits » above
  (ADC, the variables inline, never `ENV=production`) — even the dry run
  reads production:

  ```bash
  python -m scripts.reclassify_admin_ledger CSV --rapport RAPPORT.md       # dry run: plan, controls, fingerprint
  python -m scripts.reclassify_admin_ledger CSV --rapport RAPPORT.md --sauvegarde DOSSIER --appliquer EMPREINTE
  python -m scripts.reclassify_admin_ledger --restaurer SAUVEGARDE.json --rapport RAPPORT.md   # dry run of the undo
  ```

  A line whose stored values no longer match the CSV is refused and
  reported, never forced; so is an entry in a reversal pair, linked to an
  invoice or to trust, carrying a tax on a kind change, or whose trail is
  nearly full. `--appliquer` takes the fingerprint the APPROVED dry run
  printed and refuses everything when anything moved since; the typed JSON
  backup is written first; every document gets the stamps and a
  `revisions` entry (`via: script`, the motif naming its CSV line), all in
  one batch guarded by the time each was read. An absent trust counterpart
  is reported, an unreadable trust register blocks the write. A report or
  backup path inside this repository is refused. Run both integrity scripts
  before and after, and compare their findings by TEXT: a reviewed finding
  that names a migrated entry returns under a new key (its document
  changed) and needs a new review.
- **Cold starts:** `min_instances: 0` (in `app.yaml`) trades a cold start for
  zero standing cost; set `1` to eliminate it (one always-on F2).
- **Dependencies:** edit `athena/requirements.in`, then re-lock —
  `uv pip compile requirements.in --python-version 3.13 --universal --generate-hashes -o requirements.txt`
  (never hand-edit `requirements.txt`). Dependabot proposes weekly bumps; after
  any of them, re-run the matching check of [CLAUDE.md](CLAUDE.md)'s Change
  Impact Assessment (its « Dependency bumps are a silent trigger » paragraph
  names which bump needs which check). A Dependabot PR on `/athena`
  regenerates the lock as a Linux-only resolution under the `--universal`
  header: close it and re-lock the same bump by hand with the command above.
- **Frontend assets:** if you change Tailwind classes, recompile
  `static/src/app.input.css` → `static/vendor/app.<hash>.css`, then fan the new
  hash out to **five** sites — `templates/base.html`,
  `templates/auth/login.html`, **`client/templates/base.html`**,
  `static/sw.js`'s PRECACHE (bumping `STATIC_CACHE` with it), and the Early
  Hints lists in `security.py` — and delete the old hashed file. Full recipe in
  [CLAUDE.md](CLAUDE.md) → « Regenerating the stylesheet ».
  ⚠ **This list named four until 2026-09-13, and the one it omitted was
  `client/templates/base.html` — the PORTAL's own base.** Following it left the
  public, client-facing service pointing at a deleted filename served
  `Cache-Control: immutable` for a year: unstyled, for everyone, with no error
  anywhere. A test now pins this list against the files that actually carry the
  hash, so it cannot fall behind again.
  The App Check bootstrap, `static/vendor/appcheck-boot.<hash>.js`, follows
  the same rule with its own carriers — `templates/base.html`, `static/sw.js`
  and `security.py` — and `tests/test_security_headers.py` pins its literal.
- **Effacement Loi 25 — le clavardage interne est parti (2026-09-02).** Ce
  runbook visait le registre des conversations d'assistant, append-only par
  convention précisément pour que l'effacement reste TECHNIQUEMENT possible
  quand la loi l'exige. La fonctionnalité a été retirée, et ses données
  effacées puis vérifiées à zéro (15 conversations, 173 enfants de
  sous-collection, 11 objets Storage) par `scripts/effacer_clavardage.py`,
  l'acte étant consigné par les journaux d'audit Data Access activés à la
  §11b. Le principe reste bon pour toute collection append-only future :
  **une sous-collection ne cascade pas** (les enfants avant la tête), les
  compteurs agrégés sont de la comptabilité et non des renseignements
  personnels (on les laisse), et on ne script JAMAIS un effacement dans
  l'application.

---
- **Contact titles, occupation and the vCard (2026-10-30).** A natural
  person's title is one of `utils/civilite.PREFIX_LABELS` (M., Mme, Me, Dr,
  Dre, Pr, Pre, L'hon.), plus two new parts of vCard `N`: « Autres
  prénoms » (`middle_name`) and the honorific « Suffixe » (`suffix`: Ad. E.,
  j.c.s.). « Occupation » (`occupation`, `utils/occupations.py` — art. 14 of
  the lawyers' accounting regulation) is new and never serialized;
  « Titre professionnel » became « Poste ou profession » (`job_title`,
  TITLE) and « Fonction » (`job_role`, ROLE) left the schema: every save
  folds a stored one into `job_title` (`partie.fold_job_role`), never drops
  it. A title the phone sends is normalized (« Dr. » → « Dr ») and an
  unknown one kept as received — the web and the connector store keys only.
  The vCard: FN without the title (the phone no longer files most contacts
  under « M »), N with every component, no ROLE, no ADR for an empty
  address (the phantom « Québec, Canada »), PRONOUNS beside X-PRONOUN; an
  update PUT never replaces the UID (Rule 11). The connector:
  `create_partie` / `update_partie` gain `middle_name`, `suffix`,
  `occupation` and lose `job_role` (a call that still sends it is refused);
  `get_partie` reads the three new keys — still 88 tools. No index, no
  dependency, no Tailwind recompile (compiled classes only), no DAV account
  re-add, no §11 train (the consent text's read paragraph already covers
  contacts). In order:
  1. *Before the push*: `python -m scripts.migrate_contact_titles` à blanc
     from this release's checkout (ADC, inline variables, §14). Read the
     « Fonction » pairs and the unknown titles with the lawyer.
  2. *The push.* Pause scheduled Claude tasks for the deploy window: a
     same-key retry carrying `job_role` would be refused by the new schema.
  3. *Once this release serves*: `python -m scripts.migrate_contact_titles
     --appliquer --resynchroniser`, then à blanc again — it must read
     « Rien à faire. ». The resync restamps EVERY contact: DavX5 downloads
     a card again only when its ETag (the record's `etag`) changes, and the
     serialization changed for nearly all of them.
  4. *curl* (DavX5 fails silently): `getctag` of `/dav/addressbook/` moved;
     a GET of a titled contact shows `FN:` without the title, `N:…;Me;`, no
     `ROLE:`, no `ADR;` when it has no address, `PRONOUNS:` and
     `X-PRONOUN:` when it has pronouns. The throw-away contact recipe, with
     `If-Match`, a different `UID:` and `N:Curl;Test;Anne;L'hon.;j.c.s.`,
     answers 204 and the GET that follows keeps the original UID.
  5. *On the device*: contacts sort by first name; the title, the suffix and
     the other given names show in the name editor; a suffix typed on the
     phone reaches the fiche.
  6. *Word*: a letter using `{{destinataire.nom_complet_avec_civilite}}` for
     a contact titled « L'hon. » opens without repair (Change Impact item 3).
  7. *The claude.ai plugin « athena »*: the registry fingerprint changed —
     `python -m scripts.exporter_plugin_athena --check`, then rebuild and
     deliver it through the `athena-plugin` mirror the same day
     (`plugin/athena/README.md`).
  A rollback: under the older code, never save in the web form a contact
  titled « L'hon. » (its hand-quoted `x-data` breaks on the apostrophe and
  the form posts an empty mandataires list) or Dr / Dre / Pr / Pre (the
  older `<select>` has no such option and erases the title); the
  `job_role` folded by the script stays folded.
- **Personal coordinates first (2026-10-30).** Once « Occupation » made a
  client's work block hold an EMPLOYER's coordinates, the lawyer decided
  that a natural person is written to at home: `template_fields`
  (`uses_work_block`, `selected_phone`, beside `selected_address` and
  `selected_email`) is the one authority, and a natural person's personal
  block comes first unless the contact acts in a professional role
  (`avocat_adverse`, `expert`, `huissier`, `notaire`); an organization keeps
  the work block first. It now also decides the invoice's billing snapshot
  (`invoice.billing_address_from`, web and connector), the phone of every
  gabarit (`{{<slot>.telephone}}` — cell → home → work at home), the fiche's
  « Appeler » / « Courriel », the contact list's line and the portal's
  accusé (no employer line under a client's name). Issued invoices keep
  their frozen address; a brouillon moves only on « rafraîchir l'adresse ».
  No index, no dependency, no recompile, no DAV or connector change. After
  the push: a note d'honoraires drafted for a client holding both blocks
  carries the home address, and a letter's `{{client.telephone}}` the cell
  number (Word, by hand — Change Impact item 3).
- **Occupation at the portal's intake (2026-11-05).** The intake form asks a
  natural person for « Occupation » (required, the keys of
  `utils/occupations.py`), « Poste ou profession » and the employer's name —
  never the employer's coordinates. The portal REFUSES (422, nothing
  written) an occupation outside the vocabulary, and a position or employer
  name over its cap or holding a tag (`client/routes._BALISE_RE`, pinned to
  `security.TAG_RE`); it blanks the three for a legal person and the
  employer for an occupation in `WITHOUT_EMPLOYER`. Réception files them
  as `occupation`, `job_title` and `organization` (`_CORRESPONDANCE`), shows
  the occupation's label, and never offers or files an occupation the
  vocabulary does not know. A linked contact's invitation prefill carries
  the three. A client whose form was loaded before the push is told, on
  « Transmettre », to reload: the draft survives. No index, rule, secret,
  queue, consent-version or dependency change. After the push, at 375 px:
  an intake invitation to a test address, « Salarié(e) » names the field
  « Employeur », « Retraité(e) » hides it, and Réception shows the label and
  files the three fields.
- **Compliance statuses « Identifié », « Vérifié », « À faire » (2026-11-06).**
  The identity check reads « Identifié » (`identifié`, new: the client is
  identified — the particulars of art. 14 on file — without a document
  check), « Vérifié » or « À faire »; the conflict check « À faire »,
  « Vérifié » or « Conflit détecté ». « À faire » labels the kept key
  `non_vérifié` of both checks (`à_faire` is a task status). « Identifié »
  is DECIDED like « Vérifié » (`utils/kyc.DECIDED`): it closes
  `IDENTITE_NON_VERIFIEE` (label « Identité à faire », code unchanged),
  removes the list's dot, earns « le … par Me … », and Claude may only
  inscribe it presumed. « Exempté » is retired: from this release on, a
  stored one reads « Identifié » everywhere, with its date, source and
  confirmation (`kyc.stored_status`, `partie._migrate_kyc`, purged by the
  next full-document save), and `scripts/migrate_compliance_statuses.py`
  rewrites the rest; the map stays for restored backups. The same script
  repairs the fiches created through the form with a decided status before
  1d33b1a (the 2026-09-29 push), which store no `*_date` and therefore show
  no attribution: it sets `*_date = created_at` — the instant the fixed
  creation stamps — on a decided check holding no date, no source and no
  `created_via`, and only lists every other anomaly. The connector:
  `record_kyc_status`'s status enum loses `exempté` and gains `identifié`,
  and its descriptions derive from `utils/kyc`; no tool added or removed,
  no consent change (the consent text names no status, and Claude still only
  inscribes a presumed one). No index, no dependency, no recompile (compiled
  classes only), no DAV account re-add, no §11 train (Change Impact items 1,
  2 and 6). In order:
  1. *Before the push*: `python -m scripts.migrate_compliance_statuses` à
     blanc from this release's checkout (ADC, inline variables, §14). Read
     « Exempté » → « Identifié » by class, the undated decisions with their
     creation day, and « À examiner » with the lawyer.
  2. *The push.* Pause scheduled Claude tasks for the deploy window: a
     same-key retry of a pre-deploy `record_kyc_status` call carrying
     `exempté` would be refused by the new schema instead of replayed. Wait
     until this release serves 100 % (`gcloud app versions list
     --service=default`).
  3. *At 375 px*: a former « Exempté » client reads « Identifié » in blue
     with « le … par Me … »; « Nouveau contact » opens on « À faire » in both
     selects; the identity select lists Identifié, Vérifié, À faire; an
     « Identifié » client has no dot in the contact list.
  4. *Only now*: `python -m scripts.migrate_compliance_statuses --appliquer`,
     then à blanc again — it must read « Rien à faire. ». Open a fiche whose
     date was filled in (« le <jour de création> par Me … ») and a converted
     one. An « À examiner » line is settled by hand on its fiche: set « À
     faire », save, then the true status.
  5. *curl* (DavX5 fails silently): `getctag` of `/dav/addressbook/` moved —
     the script restamps the rows it writes and bumps the CTag in each batch;
     on the device, edit a migrated contact: it syncs, no 412.
  6. Resume the scheduled tasks.
  7. *The claude.ai plugin « athena »*: the registry fingerprint changed —
     `python -m scripts.exporter_plugin_athena --check`, then the follow-up
     commit « Greffon 1.3.0 » (its recipe also stops Claude from saying
     « identifié » of a presumed inscription), rebuilt and delivered through
     the `athena-plugin` mirror the same day (`plugin/athena/README.md`).
  A rollback: once this release has served, never go below it. It stores
  « identifié » from its first save — any save of a former « Exempté »
  client (web, phone, Réception, connector), any « Identifié » chosen or
  inscribed —, long before `--appliquer`, and the older code reads
  « identifié » as undecided (a false manquement, the dot, and Claude may
  overwrite it) while its form erases it on any save. If a rollback is ever
  unavoidable: stop every connector write first — pausing the scheduled
  tasks is not enough, interactive sessions write too
  (`python -m scripts.revoke_mcp_tokens`) — and save no client fiche on the
  older version until the roll forward.
- **Proofs of identity on the contact fiche (2026-11-06).** Under
  « Conformité », a client's fiche — role « client » or client of a dossier,
  and any fiche that holds a proof, so a role change never strands one —
  lists its proofs of identity, each with « Consulter » (a redirect to a
  15-minute signed URL, opened inline in a new tab; never embedded in the
  page). « Gérer » uploads PDF, JPG or PNG files of 20 Mo at most, 20 per
  contact, straight from the browser to GCS through
  `staging/{uid}/identite/{id}/upload{ext}` (five segments: the documents,
  receipt and connector finalizers refuse it, and it refuses theirs; §6.6's
  `staging/` rule sweeps an abandoned one), renames the display name, and
  deletes after a confirmation step. Records live in
  `parties/{id}/pieces_identite/{id}` — a subcollection the contact owns, so
  no write touches the contact's document, its etag or its CTag — and
  objects in `users/{uid}/parties/{id}/pieces_identite/{id}{ext}`, with no
  file name in the object name; no lifecycle rule may ever reach that
  prefix. « Supprimer » erases the object BY GENERATION (no 180-day
  non-current copy, only the bucket's 7-day soft delete; SECURITY.md) and
  appends ONE `audit_events` row of type `piece_identite` titled « Preuve
  d'identité ». The connector reaches none of it: no tool reads the
  subcollection, and `list_deletions` filters those rows out
  (`audit_event.CONNECTOR_HIDDEN_ENTITY_TYPES`) while its enum is unchanged.
  A contact holding a proof refuses deletion (web banner, CardDAV 409).
  Change Impact items 1, 2, 4, 5, 6 and 7 are touched, but no index, rule,
  secret, queue, consent-version, plugin or dependency changes. After the
  push:
  1. *At 375 px, on the phone, in the browser and in the installed app*:
     « Gérer » → « Ajouter des fichiers »: the picker offers the camera; a
     photo and a PDF show their progress, then the list. Rename one.
     « Consulter » opens each inline in a new tab, saved under its display
     name. Delete one through its two-step confirmation.
  2. *curl* (DavX5, §15-16): a DELETE of a test contact holding a proof
     answers 409 with the French reason and the contact stays; delete its
     proofs, then the contact: 204 and a tombstone.
  3. *Logs*: `jsonPayload.event` in (`id_proof_filed`, `id_proof_renamed`,
     `id_proof_deleted`) carries ids, type and size only; one
     `audit_events` row of type `piece_identite` per deletion.
  A rollback: once a proof has been filed, never go below this release —
  the older `delete_partie` would delete a contact holding proofs, and
  Firestore does not cascade: its records and objects would stay with no
  path to them in the application.
- **Filing outcome « incertain » (2026-11-06).** A document filing whose
  commit answer was lost, and whose record could not then be read back, is
  « incertain » wherever the shared ingestion runs — never « Veuillez
  réessayer », which every caller answered with a second document. The
  upload page answers 503 `incertain` and KEEPS the staging object for its
  one automatic re-post, about 2 s later: when the store answers, the
  re-post settles it; when it still does not, the re-post files nothing —
  the strict dossier read refuses it first (503, the staging object kept
  for the `staging/` 7-day rule) or, the dossier readable, the reserved
  pre-read does (which consumes the staging object) — and the page keeps
  « incertain » (the canonical copy stays). A file still « incertain » is never re-sent: check
  the dossier's documents and, only if it is absent, remove the row and add
  the file again. A failed read of the dossier or of the staging object
  answers 503 « Lecture … impossible. Réessayez. », never « sélectionnez un
  dossier » nor « introuvable ». Réception's « Verser » shows a banner — the
  file is perhaps already in the dossier, under « Autres › Reçus du
  portail » — and leaves it « reçu » in quarantine: look there before
  versing it again, since a second « Verser » mints a new document, and
  refuse it to close the lot if it is there. The receipt copy gets its own
  banner (« Verser une copie au dossier » still never files twice), and the
  note d'honoraires keeps one note per click, its sentence sending the
  lawyer to « Mandat › Factures » first. Through the connector,
  `finalize_upload`, `create_document` (`copy`, `markdown`,
  `invoice_note`) and `fill_gabarit` answer « Issue INCERTAINE » (reason
  `write_outcome_uncertain`) with their idempotency claim KEPT: the
  document MAY exist. The generations keep the creators' generic refusal —
  re-read `list_documents`, then the SAME key, whose « interrompu » refusal
  then allows a new key only if nothing was written; only `finalize_upload`
  states the full protocol, on the SAME ticket: the SAME key until
  « interrompu », then re-read `list_documents`, and only if the document
  is absent a NEW key on the SAME `ticket_id`, never a new ticket (its
  claim is kept too, reclaimed even past the hour, and every claim now
  keeps the ticket at least 24 h past itself before the TTL may collect
  it). An empty list proves nothing during the same outage: the documents
  views and the connector's `list_documents` still read fail-open (a strict
  reader is a later lot). A copy left behind by an outcome that stayed
  unknown is named by the ERROR « document ingest: filing uncertain »
  (`document_id`, `dossier_id`) for an erase by hand — never while a call
  can still finalize its id (OBSERVABILITY.md). This reverses two decisions
  the documents lot shipped the same day (a failed staging reload worded
  « introuvable », the staging consumed on every outcome). No index, rule,
  secret, consent, plugin or dependency change; **run no MCP write during
  the deploy window** (§11) — an old instance still answers « réessayez »
  and releases the claim.
- **Refusals said where they happen (2026-11-06).** A refused delete,
  move, pin or unlink no longer lands where a success lands: a task (its
  deletion, and its page's « Marquer comme terminée »), a court date (one
  occurrence or the rest of a series), a protocol or step, a note's pin,
  an administration entry, a document or folder move — refused by a
  failed read, a lock set since the page was shown, a mandatory step —
  comes back to the record's OWN page with its reason in a banner (the
  task and court-date pages now print one), and so does a contact's
  « Confirmer » whose read failed. An invoice sheet that cannot show its
  invoice forwards the refusal to the invoice list, which now prints it.
  A DELETION of a record already gone (deleted on the phone or in another
  tab) still goes to the list, quietly; a move or an unlink of one says
  « introuvable ». Under htmx no branch answers a 4xx any more, and every
  landing this change moved answers a `200` carrying `HX-Redirect` and no
  `Location` (a refused time or expense deletion re-renders its form at
  `200`) — except the `302` the task list's checkbox keeps for its XHR to
  swap the list in place (as would the document browser's folder forms and
  bulk move if htmx posted them: rendered inside Alpine `x-if` templates,
  the folder forms post without htmx today and reload the page). Plain
  forms keep their `302`. What DavX5 receives does not change: a web
  deletion still tombstones, then bumps the CTag, then journals. **Phone
  smoke test after the deploy:** delete a task and a court date on the
  web, then check that both leave DavX5 at its next sync. No index, rule,
  secret, consent, plugin or dependency change.
- **Invoice dates: UTC, never in the future, due one month later
  (2026-11-07).** The lawyer's decisions of that day; Change Impact items
  1 and 4 are touched, item 3 is not (the docx engine is unchanged, only
  the due date it prints). `plan_invoice` refuses an invoice date after
  the current UTC calendar day (reason `date_future`), which the web form
  and — for the first time — `import_invoice` reach (the import raises it
  as `argument_refused`). The UTC day itself, already tomorrow on a
  Montréal evening, is accepted, and the web form still pre-fills it — now
  through `utils.deadlines.today_utc`, the one UTC « today » Architecture
  Rule 5 names. So an invoice made on the evening of a month's or a year's
  last day defaults to the NEXT period's date (at year end its number
  still follows the Montréal year, and the « datée de … » warning says
  so): change the date in the form when the invoice belongs to the day
  ending in Montréal. The connector's `create_invoice` and
  `preview_invoice` keep their own stricter Montréal-day check and
  default, declared in `mcp/handlers._invoice_date`: aligning them would
  widen a shipped tool, a decision of its own. When no due date is given,
  it is the invoice date plus one calendar month (31 January → the last
  day of February) instead of 30 days — on the web and through both
  connector creators, an in-place change of a default on two shipped write
  tools by the lawyer's decision (item 1) — and `preview_invoice` now
  returns the `date` and `due_date` the write would store (two new keys of
  a read). The default payment terms read « Payable dans un délai d'un
  mois suivant la date de facturation. »; the due date stays correctable
  on a brouillon; existing invoices keep their stored due date and terms.
  **The note d'honoraires prints the due date (`{{facture.date_echeance}}`)
  but no placeholder carries the payment terms:** any delay written in the
  active « Note d'honoraires » gabarit (« 30 jours ») is the gabarit's own
  text — check it before the deploy and, if it states a delay, upload a
  version that says « un mois » or none. On the next real invoice dated
  the 29th to the 31st, read the clamped due date on its note. The
  connector's descriptions changed, so the registry fingerprint moved: per
  the approved plan, the plugin is rebuilt and delivered with lot A's
  plugin release (1.4.0), and `exporter_plugin_athena --check` reports
  that expected drift until then. No index, rule, secret, consent or
  dependency change.
- **TPS/TVQ remittances and the « Infonuagique et IA » category
  (2026-11-07).** The lawyer's decisions of that day (lot A of the
  financial statements). Change Impact: item 1 is touched; item 6 is
  touched and verified (the form's help line uses compiled classes only, no
  recompile); item 4 only in the documented refusal reasons (no new event);
  items 2, 3, 5 and 7 are not. The administration ledger gains
  `remise_tps_tvq` and `remboursement_tps_tvq`: the firm's TPS/TVQ paid to,
  or refunded by, the tax authorities, one combined amount. They join
  `admin_ledger.NON_RESULT_KINDS` — they move, clear and reconcile like any
  entry, carry no category, no invoice and no tax split, and no revenue,
  expense or tax-credit total counts them (`results_statement`): they
  settle the firm's TPS/TVQ balance (owed by the firm, or owed to it).
  Interest or a penalty paid stays a dépense of its own, interest received
  an autre recette. Like the lawyer's
  prélèvement and apport they never carry a dossier
  (`admin_ledger.NO_DOSSIER_KINDS`, which the model, the web form, the
  connector, `verify_admin_integrity` and `scripts/reclassify_admin_ledger.py`
  read — the last one read the owner kinds, so a reclassification into a
  refund would have kept its dossier; `services/comptabilite.ADMIN_OWNER_KINDS`
  became `ADMIN_NO_DOSSIER_KINDS`). An edit moves an entry among the kinds of
  its sign (`KIND_MOVES`), so a TPS/TVQ payment booked as a dépense becomes a
  remise while the entry is still editable: on the web, the form drops its
  category, split and dossier; through `update_admin_entry`, the category
  and split go and a dossier must first be sent empty (`dossier_id` "").
  The dépense category `infonuagique_ia` (« Infonuagique et IA (à
  l'usage) ») follows `abonnements`: cloud and AI billed by use; a flat
  plan stays an abonnement. The PDF journal's closing line now reads
  « TPS/TVQ payées sur les dépenses de la période (CTI/RTI) », which a
  remittance is not part of. `verify_admin_integrity` check 11 also names a
  remittance carrying a category, a tax, an invoice, a trust link or a
  dossier; its texts now read « ventilée … ne réclame ni TPS ni TVQ » and
  « rattachée », agreeing with « écriture » (check 11's findings are écarts
  no review can cover, so no exit code moves). The connector's
  `record_admin_entry`,
  `update_admin_entry` and `get_admin_ledger` know both natures and the
  category (enums and descriptions — no new tool, no output key;
  `mcp/output_schemas.py` declares no kind or category enum); the consent
  partial names the remittances in a sentence of their own (they settle
  the TPS/TVQ balance, so « ces trois dernières natures n'entrent dans
  aucun total … de TPS/TVQ » cannot cover them), and the write box's
  summary adds them to its list. No index, no dependency, no DAV account
  re-add.
  In order:
  1. *Before the push*: both integrity scripts, read-only, from this
     release's checkout.
  2. *The push — without the §11 train, by the lawyer's decision
     (2026-11-07)*: the token in force reaches the two natures and the
     category the moment the deploy lands. No remittance or refund had been
     made when this shipped.
  3. *The claude.ai plugin « athena »* moves to **1.4.0** (its accounting
     recipe routes a remittance, a refund and a usage bill, and the registry
     fingerprint moved — lot D's descriptions included): rebuild it, unpack
     it into the mirror repository `athena-plugin` (the old copy removed
     first), bump the mirror's `.claude-plugin/marketplace.json`, commit and
     push it the same day.
  4. *Then, and only once this release serves*, lot C: the one-shot
     reclassification of past cloud and AI charges into `infonuagique_ia`,
     on a list the lawyer approves.
  A rollback restores lot C's data FIRST (its `--restaurer`), then moves
  every dépense filed as `infonuagique_ia` since this release to another
  category — an editable one in the app, a locked one (cleared,
  reconciled) through `scripts/reclassify_admin_ledger.py`; lot C's backup
  covers only the entries lot C moved —, then rolls the code back. Under
  the older code such a dépense shows no category in the journal list and
  the CSV export, prints as « Dépense » in the PDF journal, keeps its raw
  code only on its own page, and any edit of it is refused unless it picks
  a category the older code knows: on the web « Une catégorie est requise
  pour une dépense. » (its select, lacking the code, posts it empty),
  through the connector « La catégorie de dépense est invalide ». The
  older code also refuses to edit
  an entry of the two natures, shows their raw codes, and its check 11 names
  each as a kind unknown to the register; a reversal under it stays sound
  (it copies a split from a dépense only and stamps the reversed kind).
  Redeliver plugin 1.3.0 with the code, or the 1.4.0 recipe steers Claude to
  kinds and a category the older schema refuses (`schema_invalid`, nothing
  written).
- **Financial statements (2026-11-08).** Lot B of the lawyer's plan of
  2026-11-07: the page `/comptabilite/etats` (a card on the Comptabilité
  hub — since 2026-11-10 `/finances/`, its own menu entry, below) and its PDF give, for a month, a quarter or a calendar year from
  2025-01-01, the income statement (billed basis), the cash flow of the
  operations accounts, the balance sheet at the period's start and end, the
  equity rollforward, the TPS/TVQ table, the prior-period adjustments and
  two memos (trust funds, unbilled work), computed by
  `models/financial_statements` over ONE read-only Firestore transaction
  (`services/financial_statements`) — the application's first read-only
  transaction. Each model it reads gained a strict whole-collection reader
  capped at `models/full_reads.FULL_READ_CAP` (a truncated read raises; the
  sections that need it say « indisponible »). Change Impact: item 1 in name
  only (new pure functions and readers in models a tool reaches —
  `admin_ledger`, `invoice`, `fee_payment`, `trust`, `time_entry`,
  `expense` — and no tool, schema or text change); item 4 (`admin_export` widened with `document`,
  `period_kind` and `counts`; three new `log_unexpected` messages); item 5
  (a GET-only blueprint under `@login_required`, never CSRF-exempt, under no
  special-cased prefix); item 6 (a new template on compiled classes only —
  no recompile); item 7 not touched (whole-collection reads with no filter
  or order: no index). The trust integrity script's manual-recette
  predicate moved into `models/fee_payment`, unchanged. Nothing is
  written; no rule, secret, dependency or DAV change. After the deploy:
  1. Open the page on the phone (375 px) and on the desktop, light and
     dark, and download the PDF of the last complete month.
  2. For a closed month, compare the cash with the operations journal's
     balance that day, the card with the account page, the TPS/TVQ perçue
     with the « Journal des honoraires » less its brouillon and annulée
     rows, and today's receivables with the dashboard's « Factures
     impayées », net of the listed points.
  3. Read the « Points à vérifier »: the trust receipts attributed by the
     number they cite (« rattachement présumé »), those whose invoice is
     absent from Athéna, the « payée » invoices presumed settled — each
     moves a figure until it is fixed in its own register.
  « Écart non expliqué » reads 0,00 $; any other value is a bug of the
  computation, not of the data. A rollback is the code alone.
- **Past cloud and AI charges into `infonuagique_ia` (2026-11-08).** Lot C
  of the lawyer's plan of 2026-11-07, to run only once lot A serves. The
  one-shot `scripts/reclassify_admin_category.py` writes, OUTSIDE the
  models like `scripts/rectify_expense_ventilation.py` (its docstring says
  why), the category of the dépenses the lawyer lists, line by line from a
  CSV kept outside this repository: the category, the stamps (`updated_via`
  `script`) and one `revisions` entry naming the plan line — never an
  amount, a split, a date, a link or a counterparty, no account and no
  reconciliation. A locked entry (cleared, or dated in a reconciled period)
  is accepted: a category moves no balance, no reconciliation and no tax
  figure, only the line `results_statement` reports the cost under. From
  `athena/`, with the environment of « Active gabarits » above (ADC, the
  variables inline, never `ENV=production`) — even the listing and the dry
  run read production:

  ```bash
  python -m scripts.reclassify_admin_category --lister abonnements [--beneficiaire TEXTE]   # read-only: candidates, plan lines
  python -m scripts.reclassify_admin_category PLAN.csv --rapport RAPPORT.md                  # dry run: verdicts, controls, fingerprint
  python -m scripts.reclassify_admin_category PLAN.csv --rapport RAPPORT.md --sauvegarde DOSSIER --appliquer EMPREINTE
  python -m scripts.reclassify_admin_category --restaurer SAUVEGARDE.json --rapport RAPPORT.md [--appliquer EMPREINTE]
  ```

  The listing writes nothing and creates no file: for each dépense of the
  category, its lock and whether the script would refuse it, then the plan
  lines (`ligne,entry_id,date,montant_cents,categorie_actuelle,categorie_cible,motif`)
  of the entries it would accept — keep the ones to move, fill the target
  and the motif. A line is refused and reported, never forced, when the
  base no longer holds what it states, or when its entry is not a dépense,
  belongs to a reversal pair (seen from either end), is annulée, is linked
  to an invoice or to trust, or has a nearly full trail; the other lines
  still apply. `--appliquer` takes the fingerprint the APPROVED dry run
  printed — it covers the plan file, the project, every write and every
  refusal — and refuses everything when anything moved since, or when a
  control cannot be computed or moved; the backup is written, re-read and
  verified first, then one batch guarded by the time each entry was read.
  `--restaurer` puts the saved categories back, all or nothing; an entry
  written since the migration (cleared, edited: a new etag) refuses the
  whole restoration — move such an entry back with a new plan of this
  script instead. Run both integrity scripts before and after, and compare
  their findings by TEXT. Change Impact: none of items 1 to 7 (a script and
  its tests; the registers are not DAV-exposed). A rollback of lot A
  restores this lot's data first (above).
- **A client's advance, a supplier's refund, two categories (2026-11-08).**
  Lot E, the lawyer's decisions of 2026-11-08. The administration ledger
  gains `avance_honoraires` (« Avance d'honoraires », the trust register's
  label): a client's advance received in the operations account instead of
  in trust — client money, among the natures outside the results
  (`NON_RESULT_KINDS`), a dossier allowed — and a virement interne sortant
  may carry `transfer_purpose` « avances » (`TRANSFER_PURPOSES`), the
  transfer that moves those advances to trust. It gains
  `remboursement_depense` (« Remboursement de dépense »): a supplier's
  refund, a recette that carries a category and a split like a dépense
  (`CATEGORY_KINDS`) and counts against them in `results_statement` —
  gross, net and the CTI/RTI it gives back —, never as a revenue. Two dépense
  categories: `documentation` (« Documentation juridique », a fixed cost) and
  `recherche` (« Frais de recherche », variable); `abonnements` is labelled
  « Logiciels et bureautique », its code kept. The statements show the
  advances not yet moved to trust as a liability (« Avances de clients à
  verser au fidéicommis ») — neither an advance nor its marked transfer
  moves the net assets —, lower each category by its refunds, and list the
  advances still owed, the marked transfers beyond the advances recorded,
  and an « autre recette » sent on within 14 days by an unmarked transfer of
  the same amount. The PDF journal's tax line is net of refunds. Change
  Impact: item 1 touched (record_admin_entry, update_admin_entry,
  get_admin_ledger: the kinds, the categories, `transfer_purpose` in the
  inputs and the rows — optional in the write outputs, which a same-key
  retry may replay from before —, the descriptions kept under the
  2 048-character cut; the consent partial and the write box name the two
  natures; plugin 1.5.0), without the §11 train by the lawyer's decision of
  2026-11-08 — the token in force reaches the new kinds at deploy; item 4:
  two refusal reasons (`objet_virement_interdit`, `objet_virement_invalide`);
  item 5: two form fields under the existing CSRF; item 6: the form's new
  lines on compiled classes only; items 2, 3 and 7 not touched. In order:
  1. *Before the push*: both integrity scripts, read-only.
  2. *The push.*
  3. *Plugin 1.5.0*: rebuild it, unpack it into the mirror `athena-plugin`
     (the old copy removed first), bump the mirror's marketplace.json,
     commit and push it the same day.
  4. *Then the data*, on the lawyer's lists, each a dry run first: the
     documentation and research entries through
     `scripts/reclassify_admin_category.py`; the advances of 2026 kept as
     « autre recette », their transfers' purpose and the Anthropic credit
     through the one-shot `scripts/reclassify_admin_nature.py`
     (`reclassify_admin_ledger.py` writes no money field and no purpose). It
     writes, OUTSIDE the models like the category script (its docstring
     says why), the nature, category, split and purpose of each entry its
     plan names — the target the model's own rules make of the line
     (`kind_move_allowed`, `_nature_refusal`, `_purpose_refusal`,
     `_validate_business`) —, with the same controls, fingerprint, backup
     and restoration. From `athena/`, with the environment of « Active
     gabarits » above — even the dry run reads production:

     ```bash
     python -m scripts.reclassify_admin_nature PLAN.csv --rapport RAPPORT.md       # dry run
     python -m scripts.reclassify_admin_nature PLAN.csv --rapport RAPPORT.md --sauvegarde DOSSIER --appliquer EMPREINTE
     python -m scripts.reclassify_admin_nature --restaurer SAUVEGARDE.json --rapport RAPPORT.md [--appliquer EMPREINTE]
     ```

     The plan's header: `ligne,entry_id,date,montant_cents,nature_actuelle,categorie_actuelle,objet_actuel,nature_cible,categorie_cible,net_cible,tps_cible,tvq_cible,objet_cible,motif`.
     Run both integrity scripts before and after, and compare their
     findings by TEXT.
  A rollback restores those data first, then the code: under the older code
  an entry of either kind makes the statements « indisponibles » (a kind
  the register does not know), its check 11 names it and its form refuses
  to edit it, while a `transfer_purpose` is ignored. Redeliver plugin 1.4.0
  with the code.
- **Five categories and the « Honoraires professionnels » heading
  (2026-11-08).** Lot F, the lawyer's decisions of 2026-11-08. Five dépense
  categories: `location_salle` (« Location de salle »: a room rented by the
  occasion — a VARIABLE cost without a dossier; an office's rent stays
  `loyer`), `notaire`, `expert` and `avocat` (the fees of another
  professional; `honoraires_professionnels` is labelled « Autres honoraires
  professionnels », its code kept) and `timbres_judiciaires` (« Timbres
  judiciaires et frais de greffe »: court stamps, greffe fees and copies;
  `taxes_permis` keeps the permits and other duties). The income statement
  prints « Autres produits » as one total — an « autre recette » that may be
  a fee payment's recette is now a point of its own
  (`autres_produits_lies`) —, gathers the notary, the expert, the lawyer,
  the other professionals, the bailiff and the stenographer under one
  heading, « Honoraires professionnels » (`admin_ledger
  .EXPENSE_CATEGORY_GROUPS`: a presentation only, no filter matches it),
  prints no category line that sums to zero in the period (a heading prints
  whenever one of its members does, even at zero), and lists court stamps
  filed without a dossier with the bailiff's. Change Impact: item 1 touched
  (the category enum of record_admin_entry, update_admin_entry and
  get_admin_ledger gains five values, every existing value unchanged; the
  category hint names them, and get_admin_ledger's filter says it matches
  ONE code, never the statements' heading, whose codes it names; plugin
  1.6.0), without the §11 train, as for lots A and E — no new kind of data is
  read and no match is widened; item 6: the new lines reuse the statements'
  row levels, no template or class changed; items 2, 3, 4, 5 and 7 not
  touched.
  In order:
  1. *Before the push*: both integrity scripts, read-only.
  2. *The push.*
  3. *Plugin 1.6.0*: rebuild it, unpack it into the mirror `athena-plugin`
     (the old copy removed first), bump the mirror's marketplace.json,
     commit and push it the same day.
  4. *Then the data*, on the lawyer's list, a dry run first, through
     `scripts/reclassify_admin_category.py` (above): the entries of `loyer`,
     `honoraires_professionnels`, `taxes_permis` or `autre` that belong to a
     new category. A reversed pair stays where it is (the script refuses
     either end): it nets to zero, and prints no line, in a period that
     holds both its ends — a period that holds only one (the correction is
     dated on its own day) prints that end under the old category, so name
     such pairs to the lawyer, with their two dates, beside the plan. A
     supplier's refund (`remboursement_depense`) in one of these categories
     is neither listed nor accepted by this script: move it with its
     dépense, through its edit form while it is editable, otherwise through
     `scripts/reclassify_admin_nature.py` (same nature, the new category, its
     split restated) — left behind, it prints as a negative line under the
     old category. Run both integrity scripts before and after, and compare
     their findings by TEXT.
  A rollback restores those data first (`--restaurer`), then the code:
  under the older code an entry in a new category prints its raw code in the
  statements (a `location_salle` without a dossier counted as a fixed cost),
  no category in the journal list and the CSV export, its nature
  (« Dépense », « Remboursement de dépense ») in the PDF journal's
  « Catégorie » column, an edit form with no category selected (the model
  refuses to save it, on the web as through update_admin_entry, until an
  older category is picked), and the integrity script names a refund in
  one. Redeliver plugin 1.5.0 with the code.
- **« Finances » and the statements through the connector (2026-11-10).**
  The statements page left the Comptabilité hub for its own menu entry,
  « Finances » (icon `monitoring`), in the sidebar and in the phone's
  « Plus »: `/finances/` and `/finances/pdf`; the first address,
  `/comptabilite/etats/` (and `/pdf`), answers a 302 carrying `periode`
  alone. A new read, `get_financial_statements`, serves the page's own rows
  — every row of `models/financial_statements.present` now carries a stable
  `key`, the same line keyed alike in every period — for up to 24 months,
  quarters or years in one call, all computed from ONE read-only snapshot
  (`services/financial_statements.build_series`); a section that cannot be
  computed carries its « indisponible » reason, a snapshot that read
  nothing is refused. The consent screen's read paragraph names the
  statements. Change Impact: item 1 touched (a new read, its output schema,
  plugin 1.7.0, the consent sentence) — without the §11 train: the
  statements add up records the read grant already covers (invoices, both
  registers, hours and disbursements), so no new kind of data is read; a
  token in force sees the tool at deploy. Item 5: a GET-only blueprint
  under `@login_required` at `/finances`, never CSRF-exempt, under no
  special-cased prefix, and the redirect blueprint of the old address.
  Item 6: the icon subset regenerated from `sorted(MATERIAL_ICONS)`
  (gstatic v383, 46 glyphs, `forum` gone), the stylesheet recompiled — the
  font's `url()` its only change — and rehashed, `STATIC_CACHE` bumped.
  Items 2, 3, 4 (no new event: `mcp_tool_call` covers the read) and 7 (no
  query: the whole-collection reads of lot B) not touched.
  In order:
  1. *The push.*
  2. *On the phone* (375 px, light and dark) and on the desktop:
     « Finances » under « Comptabilité » in both menus, its icon drawn (not
     the word « monitoring »), the page as before; no card left on the
     Comptabilité page; an old `/comptabilite/etats/?periode=…` bookmark
     lands on `/finances/` for the same period.
  3. *In claude.ai*, after reloading the connector's tools: ask for a
     comparative of two quarters, and compare the last complete month's
     figures with the `/finances/` page.
  4. *Plugin 1.7.0*: rebuild it, unpack it into the mirror `athena-plugin`
     (the old copy removed first), bump the mirror's marketplace.json,
     commit and push it the same day.
  A rollback is the code alone: redeliver plugin 1.6.0 with it.

- **Pièces justificatives through the connector, and the short consent
  screen (2026-11-10).** Three lots, the lawyer's request of the day. Lot A
  (`af8ab41`, pushable alone) repairs the entry page's upload: its widget
  read the CSRF token with `this.$el` from a NESTED `@change`, found none,
  and every upload since 2026-08-13 answered 400 « The CSRF token is
  missing. » (shown as « refusé »); the token is now an `x-ref`, and .eml and
  .msg join PDF, JPG, PNG and TIFF. Lot C (`32ef35a`) cuts the consent
  screen from about 4 600 words to about 430 — four read lines, one line
  per write family, one short clause per « never » —, the 13 family
  partials deleted. Lot B gives the connector the receipts:
  `begin_upload` with purpose `piece_justificative` (`tx_id`, and
  `replaces_receipt_md5` to replace — the target state, never a toggle),
  then `finalize_upload`, which attaches through the model's door
  (`models/admin_ledger.attach_uploaded_receipt`, reached through
  `services/comptabilite`): a create-only object under
  `…/administration/{tx}/pieces/{ticket}{ext}`, the precondition re-checked
  in the transaction, a revision naming both paths, and NOTHING deleted —
  a replaced receipt's object stays where it was. A dépense of a dossier
  gets its copy in « Mandat › Déboursés » under the ticket's reserved id,
  as the web files one. The web's own « Remplacer » still deletes, and its
  uploads now land under their staging uuid, so they can never overwrite a
  kept file. A new read, `get_admin_receipt`, gives a PDF's text layer, an
  email's text (.eml by the stdlib, .msg by `olefile`, a new pure-Python
  dependency) or a JPG/PNG photo as the IMAGE itself — reduced by Pillow
  (long side 1 568 px, JPEG, at most 700 KiB) and sent as an MCP image item
  after the JSON, never in `structuredContent`, never a link; a TIFF, a
  photo too large even reduced or an unreadable file answer `content_kind`
  « none » with a French reason. `get_admin_ledger`'s rows carry `receipt`
  (name, type, size, MD5 — never a path). The « never » that said the
  connector attaches no receipt is lifted, by the lawyer's decision.
  **90** tools (34 read, 56 write). Change Impact: item 1 — a purpose, a
  read, the image channel, a lifted promise, the screen rewritten, plugin
  1.8.0: hence the §11 train below; item 4 — no new event: new fields on
  `mcp_upload_opened`, `mcp_upload_finalized` (and its refusal reason
  `piece_modifiee`) and `admin_receipt_attached`, and the new messages,
  all in OBSERVABILITY.md; item 5 — lot A a client-side token fix, no
  exemption changed; lot B's staging objects under the existing `staging/`
  rule, the shared sniff, Pillow's JPEG and PNG decoders reachable from
  `utils/receipt_image.py` alone behind a header check (pixel and side
  caps), `olefile` on stored, sniffed .msg of at most 10 MB; item 6 —
  templates with compiled classes only. Items 2 (no DAV-exposed record),
  3 and 7 (single-field equalities only) not touched. In order:
  1. *Lot A, whenever it ships*: on an entry's page, attach a PDF, a JPG,
     an .eml and an .msg — each lands, no « refusé »; the logs show
     `admin_receipt_attached` (and `admin_receipt_filed` for a dépense of a
     dossier), and no `csrf_failure`.
  2. *Before the push of C and B — the §11 train*: in claude.ai, remove
     the « Athena » connector; then, from `athena/`, `python -m
     scripts.revoke_mcp_tokens`. The token in force was granted under a
     screen that said Claude never attaches a receipt.
  3. *The push.*
  4. *Re-add the connector* (§11), read the new screen — four read lines,
     the box, its lines and its « jamais » — and tick « Autoriser aussi
     les écritures ».
  5. *Verify `tools/list`*: **90** tools for the write token; a read-only
     grant lists the **34** reads, `get_admin_ledger` and
     `get_admin_receipt` among them. The `initialize` text reads « TOOLS:
     34 tools read; 56 write, in 13 families: ».
  6. *Reads on real receipts*, in claude.ai: `get_admin_ledger` shows
     `receipt` on an entry carrying one; `get_admin_receipt` on a PDF (its
     text), an .eml and an .msg (headers, body, a PDF attachment's text), a
     2–4 MB phone photo (seen as the image: if Claude says it did not see
     it, the image item did not reach the model — report it, never let it
     describe one) and a TIFF (its metadata and the French « none »). The
     logs: one `mcp_tool_call` each, no `pallas.unexpected` line.
  7. *The first attach*, on an entry the lawyer chooses: the entry page
     shows the receipt, the dossier's « Mandat › Déboursés » its copy; the
     entry carries `updated_via` « mcp » and the revision « Pièce
     justificative jointe »; the logs show `admin_receipt_attached` and
     `mcp_upload_finalized` (`transaction_id`, `dossier_copy` « versee »).
     A replacement, if tried: the previous object still listed under the
     entry's folder of the bucket, the revision « … remplacée — la
     précédente est conservée ».
  8. *Plugin 1.8.0*: rebuild it, unpack it into the mirror `athena-plugin`
     (the old copy removed first), bump the mirror's marketplace.json,
     commit and push it the same day.
  A rollback past lot B: first let every open receipt ticket expire (one
  hour after the last `begin_upload` with purpose `piece_justificative`) —
  an older version knows two purposes and would read a third as a
  gabarit's. Then the code, with plugin 1.7.0; the receipts attached
  meanwhile stay, served by the web like any other.

- **The bank statement of a reconciliation (2026-11-10).** Art. 32, 3°, of
  the Barreau's accounting regulation (RLRQ c. B-1, r. 5): the bank
  statements behind the registers are kept 7 years. Each reconciliation, of
  either register, now carries ONE copy of its statement, through
  `models/releve_conciliation` (the one door; `routes/_releve_conciliation`
  and the partial `components/_releve_conciliation.html` on both
  worksheets). The lawyer's decisions: a « brouillon » attaches, replaces or
  removes it; « Compléter » REFUSES without it (both models'
  `complete_reconciliation`, judged inside the transaction, whose completed
  record is now built from the copy it read — the pre-pass copy, set over a
  statement attached meanwhile, would have dropped it); a « complétée »
  without one takes it once, then it is FROZEN — never replaced, never
  removed. Every reconciliation completed before this release lacks one:
  the catch-up is « Relevés manquants » (a banner, a `?releve=manquant`
  filter and a badge on each list, a link on the Comptabilité page), with
  no deadline. The bytes go browser → GCS (create-only resumable session on
  `staging/{uid}/releve/{id}/upload{ext}`, sniffed, copied create-only to
  `users/{uid}/{fideicommis|administration}/conciliations/{rec}/releve-{id}{ext}`
  — ids only, no file name stored); « Consulter » is a 15-minute signed URL.
  The six `statement_*` fields and `statement_retired_ids` joined
  `_RECONCILIATION_KEYS` (the repair scripts' whitelist). A completion checks the object it freezes (its
  path's shape and its presence), and refuses a statement replaced or
  rewritten since that check (the reconciliation's etag);
  a finalization's own type patch is pinned to the metageneration its copy
  produced, and its rollback to the last one the call saw, so a
  finalization's rollback never erases an object a parallel finalization
  has patched to adopt it; the erasures after a record stopped pointing at
  its object (removal, replacement, abandon) are checked afterwards. A
  removal or a replacement retires its statement for good
  (`statement_retired_ids`: no finalization attaches it again, not even a
  re-post of its own upload), so « erased object referenced » is a
  defect: restore the generation from the bucket's 7-day soft delete,
  whatever the status it names (OBSERVABILITY.md; the reviews of
  2026-11-10 and 2026-11-11). Change Impact: item 1 in name only (the « register_setup »
  never-rule forbids the four new calls and names the statement — « (relevé
  bancaire compris) », three words on the short screen, since the connector
  now attaches an entry's receipt —, and the
  « link » never-rule forbids `consult_url`, the model signer of a
  statement's and a proof's read link — plugin 1.8.1, whose « Seule
  l'application » the first feeds; the consent screen's « Jamais » clause
  follows; no tool, schema or payload changes, no revocation: it
  narrows); item 4 (four events —
  `trust_`/`admin_reconciliation_statement_attached` / `_removed` — and the
  module's messages, ids only); item 5 (four twin routes under the existing
  prefixes, never CSRF-exempt, JSON with the `X-CSRFToken` header — not
  App-Checked, like the proofs and the receipts); item 6 in name only (a
  partial on compiled classes, the third verbatim twin of the chunked
  uploader — no recompile); items 2, 3 and 7 not touched (no new query: the
  lists filter what they already read).
  In order:
  1. *The push.*
  2. *The reconciliation in progress, if any*: attach its statement on the
     worksheet, then complete it (« Compléter » refuses without it).
  3. *The catch-up*: Comptabilité → « Relevés manquants », one register then
     the other: open each completed reconciliation listed, « Joindre le
     relevé ». Once attached, it is frozen — check the file before.
  4. *On the phone* (375 px): attach a photo or a PDF, « Consulter », and
     check that « Retirer » is gone on a completed reconciliation.
  5. *Plugin 1.8.1*: rebuild it, unpack it into the mirror `athena-plugin`
     (the old copy removed first), bump the mirror's marketplace.json,
     commit and push it the same day. It carries 1.8.0's receipts too: a
     1.8.0 not yet delivered is skipped, never delivered after it.
  A rollback is the code and plugin 1.8.0: the older code completes without
  a statement, offers no upload and shows the fields nowhere — but its
  repair scripts (`rectify_admin_entries`, `rectify_pre_registration_taxes`,
  whose `_RECONCILIATION_KEYS` lacks the six fields and
  `statement_retired_ids`) then refuse, as « conciliation_instantané »,
  every entry cited by a reconciliation completed with a statement or
  carrying retired ids — a draft whose statement was removed keeps those
  ids, and the older code completes it without a statement — run them
  from this release's code; and its abandon
  deletes a draft without erasing the statement attached to it, leaving
  the object under `users/{uid}/{fideicommis|administration}/conciliations/`
  with no record and no log line — before rolling back, list the drafts
  that carry a statement (and settle them), or afterwards erase the
  objects no record points at.

- **Journals newest first, paginated (2026-11-10).** The administration
  journal (each account), the trust journal when a filter is set (without
  one it already paged newest first) and the carte-client show their newest
  entries first, 15 per page, under the shared pager
  (`components/pagination.html`, in its « page » mode: `pagination.paginate`).
  Each view reads its period as before and pages it in memory, so every
  « Solde » stays exact: the administration running balance is computed over
  the whole period before the page is cut (« Solde reporté » now closes the
  last page, under the oldest entry), and the trust balances are stored on
  each row. The filtered trust journal reads the complete register of its
  period (`trust.list_register`, the art. 38 reader) instead of the oldest
  600 entries it used to keep 200 of, shown in the unfiltered journal's
  order (newest sequence first), and the carte-client a complete read of
  its own (`trust.list_card_register`); a truncated read says that the
  newest entries are missing. The exports are unchanged: the CSV and the PDF
  read the whole period, oldest first, and ignore the page. Change Impact:
  item 1 — touched only by a refactor that leaves `trust.list_transactions`
  (which the connector's `list_trust_transactions` reaches) equivalent: its
  day filter moved into `_day_bounds` / `_within_days`, with the same
  query, limit, order and day bounds, checked against the previous release
  output for output (the connector's tools, every export, the dossier's
  trust tab); items 2, 3, 4 and 7 not touched (no new query shape, so no
  index to deploy); item 5 — a `page` parameter, parsed as an integer and
  clamped, and the carte-client's period carried in two hidden inputs
  re-formatted from the parsed dates, never the raw query string; item 6 —
  templates with compiled classes only. In order:
  1. *The push* — no index, no connector train.
  2. *On the desktop and the phone (375 px)*: `/administration/` for each
     account — newest first, « Page 1 / N », each row's « Solde » the
     balance after it, and with a period, « Solde reporté » under the
     oldest entry of the last page; `/fideicommis/` with a filter (paged)
     and without (as before); a carte-client, from a dossier's
     « Fidéicommis » tab.
  3. *The exports*, opened from page 2 of each view: the same CSV and PDF
     as before — oldest first, the whole period.
  A rollback is the code alone.

- **The Bibliothèque: Gabarits, Modèles and Documents (2026-11-11).**
  « Bibliothèque » (icon `local_library`) replaces « Documents » and
  « Gabarits » in the sidebar and in the phone's « Plus », where
  « Documents » stood; it opens `/gabarits/`. One tab bar,
  `templates/bibliotheque/_onglets.html`, links the three lists in the
  lawyer's order: Gabarits, Modèles (`/modeles/`, new) and Documents. A
  contract model is a template of the new kind `modele` (« Modèle (sans
  champs) »), chosen at upload or set later in « Modifier » — that is how a
  contract filed as a gabarit moves over, no migration: listed under
  Modèles, never under Gabarits, downloaded and adapted in Word, never
  filled — the generation popup never offers one and refuses it
  (`generation_failed`, `kind_refused`), as `fill_gabarit` does. By the
  lawyer's choice the connector sees modèles like any template:
  `list_templates`' default list includes them, `kind="modele"` filters
  them, and `create_template`, `update_template` and `begin_upload` accept
  the kind. Change Impact: item 1 touched (the kind in the copied enum and
  its descriptions, the refusal's sentence, plugin 1.9.0 — the registry
  changed) — without the §11 train: a kind of template under writes
  already consented, no new kind of data, no consent text; item 3 lightly
  (the web refusal, one row of `GABARITS_PLACEHOLDERS.md`; the engines do
  not change); item 4 (`kind_refused` on the web, registered); item 5
  lightly (a GET-only browser blueprint at `/modeles`, under
  `@login_required`, never CSRF-exempt, under no special-cased prefix);
  item 6 (the icon subset regenerated from `sorted(MATERIAL_ICONS)` —
  gstatic v383, 47 glyphs, `local_library` added —, the stylesheet
  recompiled — the font's `url()` its only change — and rehashed,
  `STATIC_CACHE` v18); items 2 and 7 not touched.
  In order:
  1. *The push.*
  2. *On the phone* (375 px, light and dark) and on the desktop:
     « Bibliothèque » in both menus between « Tâches » and « Réception »,
     its icon drawn (not the word « local_library »), no « Documents » or
     « Gabarits » entry left; the three tabs, the current one marked;
     « Nouveau modèle » on Modèles (the round button on the phone).
  3. *A real modèle*: upload a contract the lawyer KEEPS (a deleted test
     modèle would leave a permanent journal row), download it and open it
     in Word; if he wishes, switch a contract filed as a gabarit to
     « Modèle » in « Modifier ».
  4. *Plugin 1.9.0*: rebuild it, unpack it into the mirror `athena-plugin`
     (the old copy removed first), bump the mirror's marketplace.json,
     commit and push it the same day. It follows 1.8.1, delivered with
     the reconciliations' bank statements.
  5. *In claude.ai*, after reloading the connector's tools, the lawyer
     asks for his contract models: `list_templates` with `kind="modele"`
     lists the modèle; asking Claude to fill it is refused.
  A rollback is the code alone, with plugin 1.8.1: on the old code a
  modèle shows among the gabarits (its kind unknown there, `fill_gabarit`
  still refuses it, but the web popup offers and generates it like a
  gabarit); saving its form there turns it into a gabarit, and its other
  edits (« Rétablir », Claude's rename or new file) are refused « Type de
  gabarit invalide ».

- **Office-hours crons, one Secret Manager client, a fuller warmup
  (2026-11-12).** The lawyer's decision: the portal reconciliation (every
  15 minutes), the Bookings sync and the Outlook mirror (every 10 minutes)
  run from 9 h to 17 h, Montréal time, only (`cron.yaml`, `timezone:
  America/Toronto`) — no longer around the clock, which kept one F2
  instance up day and night. Their runs keep the instance warm during
  office hours; outside them only real use wakes it. App Engine's cron
  syntax cannot limit an interval to weekdays, so weekends keep the same
  hours. Measured over 5–12 November and simulated with the new window:
  about 14 instance-hours a day instead of 24, and about 13 cold starts a
  day of about 8 s — about 4 of them the lawyer's own first page, before
  9 h or after 17 h, about 5 DAVx5 syncs and 3 connector calls; inside
  office hours, only a deploy's. The cadences live in
  `utils/cron_schedule.py`, which every text promising a delay quotes —
  Réception's footnote and refusal, three connector messages,
  `update_hearing`'s description — and `tests/test_cron_schedule.py` pins
  `cron.yaml` to it. Outside office hours, what waits until 9 h: a
  « Bookings with me » reservation's arrival in Réception; a hearing
  created, moved or cancelled in Athéna reaching the Outlook calendar — so
  a client can book over a court date entered after 17 h until the next
  morning's first run; a client's cancellation reaching Athéna; a portal
  batch whose Cloud Task failed. Beside it, `config.py` and
  `client/config.py` read every secret through ONE Secret Manager client
  per process (five clients per worker before), and `/_ah/warmup` — sent
  only when App Engine starts an instance ahead of traffic, never on a
  wake from zero — also opens the « portail » Firestore channel without
  reading an invitation, imports the two Firebase modules loaded on first
  use and compiles `base.html` and the dashboard. Change Impact: item 1
  lightly (texts only — the registry's fingerprint covers names and input
  schemas, unchanged, so plugin 1.9.0 stays current); item 2 not touched
  in what syncs (DAVx5 pulls, and what the crons write is invisible to DAV
  until confirmed), but a sync that wakes the instance waits for its
  start; item 4 not touched (no event; the cadence's mentions in
  `athena/OBSERVABILITY.md` corrected); item 5 lightly (how the same five
  secrets are fetched, with the same failure policy); items 3, 6 and 7 not
  touched.
  In order:
  1. *The push* — Cloud Build deploys `cron.yaml`, which replaces the whole
     table. App Engine's cron jobs are not Cloud Scheduler API jobs
     (`gcloud scheduler jobs list` shows none): the console's App Engine →
     Cron jobs page lists the three, with their window.
  2. *The first day*: the request log shows `/taches/` only from 9 h to
     17 h — and whether 17:00 itself has a run, which the documentation
     does not say:
     `gcloud logging read 'resource.type="gae_app" AND protoPayload.resource:"/taches/"' --project=$PROJECT --freshness=1d --format='value(protoPayload.startTime,protoPayload.resource,protoPayload.status)'`.
  3. *The sleep*: `protoPayload.wasLoadingRequest=true` on ordinary
     requests before 9 h or after 17 h — the instance stopped and started
     again.
  4. *On the phone*, before 9 h or late in the evening: pull to refresh in
     DAVx5 — the sync completes, its first request about 10 s slower than
     during the day.
  5. *A week later*: Billing → Reports, App Engine instance hours.
  A rollback is `cron.yaml`'s old schedules (`every 15 minutes`,
  `every 10 minutes`, `every 10 minutes`, no timezone) with the code.

  **The deploy images** (same day, by `gcloud`, not by the push): App
  Engine keeps every build's image in Artifact Registry (`gae-standard`,
  `northamerica-northeast1`) — 436 for `app/default`, 202 for
  `app/portail`, 40 for `app/chat` (the service deleted 2026-09-02). A
  cleanup policy keeps the ten newest images of each package — the four
  versions per service that `cloudbuild.yaml`'s cleanup leaves, rollback
  included, plus margin — and deletes the rest once a day:
  `gcloud artifacts repositories set-cleanup-policies gae-standard --location=northamerica-northeast1 --project=$PROJECT --policy=<file> --dry-run`
  with the two rules `{"name": "garder-les-10-plus-recentes", "action":
  {"type": "Keep"}, "mostRecentVersions": {"keepCount": 10}}` and
  `{"name": "supprimer-anciennes-images", "action": {"type": "Delete"},
  "condition": {"tagState": "any", "olderThan": "86400s"}}` — a Keep rule
  always wins over a Delete rule. It was set in dry-run on 2026-11-12,
  then ENFORCED on the lawyer's go the same day — the same command with
  `--no-dry-run` —, and
  `gcloud artifacts packages delete app/chat --repository=gae-standard --location=northamerica-northeast1 --project=$PROJECT`
  removed the deleted service's 40 images. Artifact Registry applies the
  policy in a background pass about once a day. A deleted image cannot be
  restored; a version whose image is gone
  is redeployed from its commit.

  **The instances** (same release): both services run ONE gunicorn worker
  × 8 threads (`app.yaml`, `portail.yaml`; 2 × 4 before) — the same eight
  concurrent requests, one copy of the app to start instead of two booting
  together on one CPU share, about half the memory, and every per-process
  cache shared: the in-memory rate limits now count once per instance,
  where two workers each kept their own count — they become exact. If that
  process dies, the requests in flight fail and gunicorn starts it again;
  no worker died in the 30 days before. The default service's
  `target_cpu_utilization` rises from 0.65 to 0.9: a second instance
  starts later — a saving, never a speed-up. To check after the push: the
  request log's `/_ah/warmup` latency (7.5–9.4 s over the five deploys
  before) and, over the next days, the requests' `pendingTime` and the
  instance count. A rollback is the two yaml files.

  **The bytecode** (same release): App Engine's disk is read-only and the
  upload excluded `__pycache__/` and `*.pyc`, so every worker recompiled
  the app's own source (about 5.8 MB) at every start — 0.66 s per
  process on the development machine, more on F2. Cloud Build's test
  step now compiles it once the tests pass (`python -m compileall -q -f
  --invalidation-mode checked-hash -x '/tests/' .`, Python 3.13 as the
  runtime), and `.gcloudignore` lets it ship. checked-hash: each file is
  checked against its source's hash before use, so a stale one never
  runs, whatever the upload does to file times. The one exception to
  « no deploy-time build » (CLAUDE.md), by the lawyer's decision. A hand
  deploy from a checkout ships the timestamp-based files Python wrote
  there, which Python uses only while their source's recorded time and
  size still match; a clean checkout ships none — as before. Its effect shows in the same
  `/_ah/warmup` latency.

- **The Documents landing reads the page it shows (2026-11-12).** The
  Bibliothèque's Documents tab with no dossier chosen streamed every
  document of the firm — 2,267 — and sorted them in Python to show
  fifteen: p50 4.2 s on 2026-11-11. It now reads one page, newest first,
  in Firestore's own order (`models/document.list_documents_page`, STRICT,
  cursor-paged like the dossiers list), with its COUNT
  (`count_documents_page`, fail-open) and a completeness count that says
  how many documents the date order cannot list — none on 2026-11-12, the
  ordered and unordered counts both 2,267. A failed read — an outage, an
  index missing or still building — answers at 200 « La liste des
  documents n'a pas pu être lue », never « Aucun document ». The search box
  and the « Nom » and « Taille » sorts keep the whole read. The dossier
  picker reads four fields per dossier
  (`models/dossier.list_dossiers_for_picker`) and says « indisponible » if
  its read fails; the search box gains `hx-sync`. Change Impact: item 7 —
  the composite `(category ASC, created_at DESC)` on `documents`, for the
  category filter, created in production BEFORE the code by `gcloud
  firestore indexes composite create` (READY in about four minutes); the
  unfiltered order rides the automatic `created_at` index, proved by its
  COUNT run in production; item 4 — two `log_unexpected` lines,
  registered; `list_documents` is unchanged, so the connector (item 1),
  the zip builder and the dossier tab read as before; items 2, 3, 5 and 6
  not touched (classes already compiled, the `error` glyph already in the
  subset).
  In order:
  1. *The index*, before the push — done: `gcloud firestore indexes
     composite list --project=$PROJECT` shows 38 READY.
  2. *The push.*
  3. *On the desktop and the phone (375 px)*: Bibliothèque → Documents
     opens fast, newest first, « Page 1 / N »; the category filter pages;
     « Nom » and « Taille » still sort every document; a search still
     finds.
  4. *The request log*: `/documents/` without `dossier_id`, its p50
     against 4.2 s.
  A rollback is the code alone; the index can stay.

- **Suppliers and stenographers among the contacts, suppliers kept off the
  phone (2026-11-11).** Two contact roles join « Impliqués »: « Fournisseur »
  (a supplier the firm pays) and « Sténographe ». The roles have one source,
  `utils/contact_roles.py`; the contact model, the connector's three enums
  (`list_parties`, `create_partie`, `update_partie`), the generated
  documents' work-address rule (both new roles are written to at their work
  address) and the vCard CATEGORIES label — the role's wire format to DavX5
  — read it, and tests pin each copy. DavX5's address book carries every
  contact EXCEPT the suppliers (the lawyer's decision). Every contact write
  passes through one door, `services/carnet.apres_ecriture_contact`, which
  bumps the « parties » CTag in the same commit as the contact's tombstone
  (a supplier: the next sync deletes it from the phone) or as its removal
  (a contact that returns); a delete goes through `apres_suppression_contact`,
  and a sweep refuses any address-book signal outside the door. The CardDAV
  handlers never list or serve a supplier (404, the tombstone's answer; a
  phone DELETE of a stale copy never reaches it), report every supplier as
  deleted at each full sync — from the same read, so a supplier leaves the
  phone even if its tombstone never landed or was pruned — and refuse a
  phone card naming « Fournisseur » (422 « Un fournisseur s'inscrit dans
  Athena, jamais depuis le téléphone. ») or a stale copy of one (412). Also: the
  contact list's `?role=` is checked against the vocabulary (an unknown
  value listed every contact under « Client »), the role menu marks the
  stored role `selected` on the server, and an edit posted without a role
  keeps it (it became « client »). Change Impact: item 1 touched (three
  enums grow, opt-in, so no existing call matches or defaults differently;
  two descriptions; the outputs keep their keys, but `dav_synced` reads
  false after a supplier write — the contact is not in the address book —
  and its description says so; plugin 1.9.1 — the registry changed, and its
  skill now says the phone carries every contact but the suppliers) —
  without the §11 train: no new kind of data, no consent text;
  item 2 (the exclusion, the tombstones, the one door — curl below, then
  the phone); item 3 lightly (the work-address rule names the two roles and
  the gabarits guide says so; no contact holds them yet); item 5 lightly
  (the role filter checked); item 6 (one badge colour, teal, already
  compiled with its dark value); items 4 and 7 not touched.
  In order:
  1. *The push.*
  2. *On the web*: switch one contact that really is a supplier (an
     « Autre » one, say) to « Fournisseur » — its badge turns teal, it gets
     no « Conformité » card.
  3. *curl* (§15-16, with the DAV credentials): `PROPFIND
     /dav/addressbook/` Depth 1 no longer lists that contact's href, `GET`
     of it answers 404, and a `sync-collection` REPORT with an empty token
     reports it with a 404 status.
  4. *On the phone*: at DavX5's next sync the contact leaves the address
     book; the other contacts are untouched. Switch it back to « Autre »
     once to see it return, then to « Fournisseur » again.
  5. *Plugin 1.9.1*: rebuild it, unpack it into the mirror `athena-plugin`
     (the old copy removed first), bump the mirror's marketplace.json,
     commit and push it the same day.
  6. *In claude.ai*, after reloading the connector's tools: `list_parties`
     with `contact_role="fournisseur"` lists the suppliers.
  Never roll back below this release once a supplier or a stenographer
  exists. The older code knows neither role: its web form shows such a
  contact as « Client » and a save silently stores « client » (with the
  identity-check dot); its phone sync serves them labelled « Autre » —
  suppliers back on the phone — and a phone edit stores « autre »; its
  connector and mandataire writes refuse them (« Rôle de contact
  invalide »), and its compliance tool refuses a non-client. If a rollback
  is forced, edit no such contact anywhere until the roll-forward, redeliver
  plugin 1.9.0, and after the roll-forward check the role of every supplier
  and stenographer.

- **The supplier record (2026-11-11).** A supplier's or a professional's
  contact (`utils/contact_roles.PAYEE_ROLES`) holds, under « Informations de
  fournisseur », its TPS and TVQ numbers (stored without spaces, shown
  « 123456789 RT 0001 », any other shape refused — ASCII digits only), its
  usual expense
  category (a key of `admin_ledger.ADMIN_EXPENSE_CATEGORIES`) and the firm's
  account number there (at most 100 characters, refused — never trimmed —
  past that or when the sanitizer would alter it); the address its invoices
  print is the contact's business block, and an empty row reads « — »
  (never « Non inscrit », which would mean « not a registrant »). Another
  role refuses a filled field the write CHANGES, and the form disables the
  section for it — on the server too — so a record kept from a former role
  stays dormant and never locks the contact; a refused save redisplays it
  as stored, so a switch back to a payee keeps it
  (`models/partie._supplier_record_errors` judges only the fields a write
  carries). Never on the vCard (a professional's phone edit keeps it, merge
  by presence) nor in the connector (the lawyer's decision). Change Impact:
  item 1 touched in name only (the contact model its tools reach: no tool
  sends the fields — the `_PARTIE_TEXT_FIELDS` whitelist — and `get_partie`
  builds its card key by key; tested); item 2 lightly (never serialized; a
  PUT keeps it — tested); item 6 (compiled classes only); items 3, 4, 5 and
  7 not touched.
  In order:
  1. *The push.*
  2. *On the web*: a supplier's fiche — « Modifier », fill its TPS and TVQ
     numbers with or without spaces, its usual category and the account
     number; the fiche shows them spaced. A client's form shows no such
     section.
  A rollback is the code alone: the older code keeps the four fields through
  its merge, without showing them.

- **The past payees, read-only (2026-11-11).** `scripts/rapport_contreparties.py`
  helps the lawyer add his suppliers by hand: it lists the payees his past
  entries name (administration dépense and remboursement, trust déboursé à
  un tiers), grouped by folded name with their spellings, their counts
  (reversed entries apart, corrections left out), main category and first
  and last dates, and whether a contact already matches — by name, legal
  name or trade name, one leading title (« Me », « L'hon. »…) aside —,
  flagging a match whose role the forms will not suggest
  (`contact_roles.PAYEE_ROLES`); an entry whose name holds no letter or
  digit is left out and counted. A second file lists the spellings used on
  drawings, contributions, TPS/TVQ remittances and refunds, and internal
  transfers. Both are semicolon-separated with every cell quoted, UTF-8
  with a BOM: a double-click opens them in French Excel. One read-only
  transaction: a failed or truncated read writes nothing; a path inside the
  repository (public) is refused; the terminal prints counts and sha256
  only. It writes nothing in Athena. Change Impact: item 7 (strict whole-collection reads, run
  locally); nothing deployed reads it; items 1 to 6 not touched.
  In order:
  1. *Run it* from `athena/` against production (§4, the variables
     inline), `--sortie` outside the repository.
  2. *Open the CSV* in Excel: add the suppliers by hand in « Impliqués » and
     change the role of each flagged contact.
  3. *Delete both files* once done: they hold names.

## 16. Troubleshooting

| Symptom | Likely cause |
|---|---|
| A list view is unexpectedly empty | A composite index hasn't finished building (§6.3). Check Firestore → Indexes. |
| `403` on every request | You're hitting App Engine directly — use the Cloudflare hostname. `*.appspot.com` is blocked by design. |
| Document upload/download fails silently in prod | Missing `roles/iam.serviceAccountTokenCreator` (self) on the App Engine SA (§6.4). |
| Login rejected with *"Accès non autorisé"* | The Firebase user's email ≠ `AUTHORIZED_USER_EMAIL`. |
| Login rejected after password entry | `REQUIRE_MFA=true` but no second factor enrolled. |
| Warning about App Check in prod logs | `RECAPTCHA_ENTERPRISE_SITE_KEY` unset — App Check is fail-open. |
| DavX5 silently won't sync | A DAV Basic-Auth mismatch, or the account was not re-added after a DAV collection layout change. Test the endpoint with `curl` first — an anonymous `PROPFIND /dav/` must answer `401 WWW-Authenticate: Basic`. |
| Word shows a "repair" prompt on a generated doc | A template-engine change introduced a `docxtpl`/`python-docx` round-trip — forbidden (see CLAUDE.md). |

---

## 17. The steps no command can verify

`python -m scripts.provision` reports everything a read-only `gcloud` call can
establish. These are the rest — console work, edge configuration, a directory
you do not own. They carry **stable ids**, the same ones the script prints
under « à la main », so a checklist and a report can refer to the same thing.

This table is GENERATED from the `manual` rows of
[`athena/utils/deployment_inventory.py`](athena/utils/deployment_inventory.py)
and pinned against them by a test.

| Id | Step | What « done » looks like |
|---|---|---|
| `firebase-auth` | Firebase Auth : fournisseur mot de passe + lien courriel | activés dans la console, domaine du portail autorisé |
| `utilisateur-unique` | L'utilisateur unique, et son second facteur | un seul compte, l'adresse d'AUTHORIZED_USER_EMAIL, MFA inscrite |
| `seau-firebase-storage` | Seau Firebase Storage par défaut | initialisé dans la console, nom == FIREBASE_STORAGE_BUCKET |
| `app-check` | App Check + clé reCAPTCHA Enterprise | application web enregistrée, clé du domaine du portail incluse |
| `cloudflare` | Cloudflare : DNS, Full (Strict), Transform Rule, Configuration Rule | la Transform Rule zone-wide PROUVÉE par le traceur de requêtes AVANT que cf-origin-secret n'existe |
| `entra` | Entra ID : inscription d'application + permissions Graph | Mail.Send et Calendars.ReadWrite, consentement administrateur |
| `declencheur-cloud-build` | Déclencheur Cloud Build sur push vers main | connecté au dépôt, exécutant cloudbuild.yaml |

Each one fails in its own way when skipped, and the script prints that
consequence beside it. Three are worth repeating here because their failure is
**silent**: App Check fails OPEN, so the application works and the protection
simply does not exist; a Cloudflare Transform Rule created *after*
`cf-origin-secret` makes the site answer 403 everywhere with no deploy to
explain it; and an Entra permission change takes 30 minutes to 2 hours to
leave the Exchange cache, so « it still works » proves nothing about a
revocation you just made.

---

**See also:** [README.md](README.md) · [SECURITY.md](SECURITY.md) ·
[CLAUDE.md](CLAUDE.md) (core principles and specifications) ·
[OBSERVABILITY.md](athena/OBSERVABILITY.md)
