# Salary, Honorarium & Government Payment Reconciliation System

A financial reconciliation platform for government scheme workers — built first for
Anganwadi Workers and Helpers under ICDS, and generic enough for ASHA workers,
contractual staff, teachers, municipal employees, NGOs and private companies.

It answers one question, with evidence:

> **Did I actually get paid what I was owed — and if not, what exactly is missing?**

---

## Contents

- [At a glance](#at-a-glance)
- [What it does](#what-it-does)
- [Quick start (evaluation)](#quick-start-evaluation)
- [Production deployment](#production-deployment)
  - [Before you deploy](#before-you-deploy-read-this)
  - [Install](#1-install)
  - [Keys and secrets](#2-keys-and-secrets)
  - [Configuration reference](#3-configuration-reference)
  - [Database](#4-database)
  - [Running the service](#5-running-the-service)
  - [Reverse proxy and TLS](#6-reverse-proxy-and-tls)
  - [Create the first administrator](#7-create-the-first-administrator)
- [Operations](#operations)
- [Security](#security)
- [Performance and scaling](#performance-and-scaling)
- [Troubleshooting](#troubleshooting)
- [Testing](#testing)
- [Known limits](#known-limits)

---

## At a glance

| | |
|---|---|
| **Stack** | Python 3.10+, FastAPI, SQLAlchemy 2.0, Jinja2, SQLite / PostgreSQL |
| **Interface** | Server-rendered HTML. Works without JavaScript. Mobile-first. English + Hindi |
| **Size** | ~24,000 lines · 17 agents · 46 tables · 38 pages |
| **Tests** | 157 passing (reconciliation, security, financial integrity, parsing, end-to-end) |
| **External services** | None required. Optional AI layer supports free tiers (Groq, Gemini) or fully offline (Ollama) |
| **Deployment target** | One office on one machine, up to a district. See [scaling](#performance-and-scaling) |

---

## What it does

For every employee and every month it establishes four separate figures and compares them:

```
ENTITLEMENT  →  SANCTION  →  DISBURSEMENT  →  BANK CREDIT
 (the rule)     (approved)     (released)       (arrived)
```

then explains the difference in plain English or Hindi, with the evidence attached.

### The principles that shape the whole system

**A payment date is not a salary month.** A credit landing on 18 April carrying
"MARCH HONORARIUM" is March's salary. Where the narration does not say, the system
produces a ranked distribution of possible months with reasons — and refuses to pick
one silently.

**A bank credit and a government sanction prove different things.** A credit proves
money arrived. A sanction proves it was authorised. Neither implies the other, so
verification is tracked on separate axes and never conflated.

**Absence of evidence is not zero.** A month with no sanction document shows
"no record", never ₹0.

**A smaller payment is not proof of a legitimate deduction.** If money is missing and
no authorising record exists, it is recorded as an *unexplained* deduction — never
quietly accepted, and never called fraud.

**Money is never forced into a month.** What cannot be confidently attributed stays
visible as unallocated rather than being absorbed to make a total look tidy.

**History is never rewritten.** A recalculation supersedes the old record; the original
stays queryable. Documents are versioned, not overwritten. The audit log is append-only.

### The agents

| Agent | Responsibility |
|---|---|
| Document Intelligence | Store evidence immutably, extract text and fields |
| Salary Rule Intelligence | Resolve which versioned, source-backed rule applies |
| Entitlement | Compute what is due, component by component |
| Bank Extraction | Parse PDF / CSV / Excel statements into one model |
| Classification | Label each credit from several independent signals |
| Salary Month Matching | Decide which month a payment belongs to |
| Allocation | Score matches transparently; protect the sum invariant |
| Reconciliation | The three- and four-way comparison |
| Deduction Analysis | Distinguish authorised from unexplained |
| Arrear | Track what is owed and what settled it |
| Verification | Enforce the six-level evidence hierarchy |
| Anomaly Detection | Find discrepancies in neutral language |
| Explanation | Render every finding in English and Hindi |
| Document Request | Say exactly what would close each gap |
| Query / Assistant | Answer questions from the database, with evidence |
| Monthly Close | Freeze months, produce annual figures, monitor continuously |
| Orchestrator | Sequence the agents and know when to stop and ask a human |

### The confidence gate

```
strong evidence + high confidence  →  auto-process
medium confidence                  →  suggest, await confirmation
low confidence                     →  do not post; ask a person
conflicting evidence               →  stop and request review
```

---

## Quick start (evaluation)

**Windows:** double-click `start.bat`  **macOS / Linux:** `./start.sh`

First run installs dependencies, creates the database, offers demonstration data, and
opens <http://localhost:8000>.

### Demo accounts

| Email | Password | Sees |
|---|---|---|
| `sunita@demo.gov.in` | `Worker@12345` | One worker's own records (Hindi interface) |
| `admin@demo.gov.in` | `Admin@12345` | Everything — administrator |
| `auditor@demo.gov.in` | `Audit@12345` | Everything, read-only, full evidence access |
| `block@demo.gov.in` | `Block@12345` | Only employees in one block |

The demo ledger deliberately contains the hard cases: a short payment, a month paid in
two instalments, one credit covering two months, a payment with no month named, an
unpaid month, a reversed payment, an over-payment, and a sanction never credited.

> Demo credentials are displayed on the login page **only** while seeded demo data is
> present. Never run `manage.py seed` on a production instance.

---

## Production deployment

### Before you deploy: read this

Three constraints have real consequences. They are stated here rather than buried.

#### 1. Run a **single web worker process**

The background job queue and the rate limiter are in-process. With multiple worker
processes, every process runs the startup orphan-requeue and would re-execute the same
queued job concurrently.

The ledger itself is protected — reprocessing is idempotent and covered by
`test_reprocessing_does_not_double_count` — so this will not corrupt financial data.
It will waste work and can produce duplicate notifications.

**Until the queue moves to a shared broker, run one worker process.** One process
handles a single office or district comfortably; see [scaling](#performance-and-scaling).

#### 2. Set `ENCRYPTION_KEY` separately from `SECRET_KEY`

Bank account numbers are encrypted at rest. If `ENCRYPTION_KEY` is unset, the cipher key
is derived from `SECRET_KEY`.

**Rotating `SECRET_KEY` without a separate `ENCRYPTION_KEY` makes every stored bank
account number permanently unreadable.** Decryption fails silently — accounts render as
`XXXXXXXXXX` rather than raising an error, so the damage is easy to miss.

Verified behaviour:

| Configuration | After rotating `SECRET_KEY` |
|---|---|
| `ENCRYPTION_KEY` unset | Account numbers **unrecoverable** |
| `ENCRYPTION_KEY` set and unchanged | Account numbers intact |

Set both from the start. Treat `ENCRYPTION_KEY` as permanent; rotate `SECRET_KEY` freely
(it only invalidates active sessions).

#### 3. Back up the evidence vault, not just the database

`data/documents/` holds the original uploaded files. The database stores paths and
checksums, **not** file contents. A database-only backup restores a system whose evidence
links all point at missing files.

---

### 1. Install

Requirements: Python 3.10 or newer, 1 GB RAM, and disk for the evidence vault
(budget ~2 MB per employee per year of scanned documents).

**Windows (PowerShell)** — note it is `python`, not `python3`:

```powershell
git clone <your-repository> D:\reconciliation
cd D:\reconciliation

python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Linux / macOS:**

```bash
git clone <your-repository> /opt/reconciliation
cd /opt/reconciliation

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

> **Command paths differ by platform.** The rest of this guide writes
> `.venv/bin/python` (Linux/macOS). On Windows use `.venv\Scripts\python.exe`
> everywhere it appears. If your virtual environment is already activated — your
> prompt starts with `(.venv)` — plain `python` works on both and you can drop the
> path entirely.

Optional: OCR for scanned images requires the Tesseract binary plus `pip install pytesseract`.
Without it the system reports `UNAVAILABLE` and asks for manual entry — it never fabricates
an extraction.

### 2. Keys and secrets

```bash
python -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))"
python -c "import secrets; print('ENCRYPTION_KEY=' + secrets.token_urlsafe(48))"
```

Create `.env` in the project root (copy `.env.example` as a starting point):

```ini
ENVIRONMENT=production
DEBUG=false

SECRET_KEY=<generated above>
ENCRYPTION_KEY=<generated above — treat as permanent>
COOKIE_SECURE=true

DATABASE_URL=sqlite:////opt/reconciliation/data/reconciliation.db
STORAGE_DIR=/opt/reconciliation/data/documents
```

Lock it down:

```bash
chmod 600 .env
chown app:app .env
```

`DEBUG=false` matters: it withdraws both the interactive API docs at `/api/docs` **and**
the `/openapi.json` schema behind them (which would otherwise enumerate every route and
parameter), and it reduces log verbosity.

### 3. Configuration reference

Every setting is environment-overridable and has a working default.

#### Core

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `development` | Set to `production` |
| `DEBUG` | `true` | **Set `false`.** Withdraws `/api/docs` and `/openapi.json` |
| `DATABASE_URL` | SQLite in `data/` | See [Database](#4-database) |
| `STORAGE_DIR` | `data/documents` | The evidence vault — back this up |
| `SQL_ECHO` | `false` | Logs every query; diagnostics only |
| `WORKER_THREADS` | `2` | Background job threads *within* the process |

#### Security

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | generated to `data/.secret_key` | **Set explicitly.** Signs sessions |
| `ENCRYPTION_KEY` | falls back to `SECRET_KEY` | **Set explicitly.** See [warning](#2-set-encryption_key-separately-from-secret_key) |
| `COOKIE_SECURE` | `false` | **Set `true`** when serving over HTTPS |
| `SESSION_MAX_AGE` | `28800` (8 h) | Seconds |
| `MAX_LOGIN_ATTEMPTS` | `6` | Then the account locks |
| `LOCKOUT_MINUTES` | `15` | Lockout duration |
| `MAX_UPLOAD_MB` | `25` | Must be ≤ the proxy's body limit |

#### Rate limiting

| Variable | Default | Notes |
|---|---|---|
| `RATE_LIMIT_WINDOW` | `60` | Window in seconds |
| `RATE_LIMIT_MAX` | `300` | General requests per window per IP |
| `RATE_LIMIT_LOGIN_MAX` | `10` | Sign-in attempts per window per IP |
| `RATE_LIMIT_UPLOAD_MAX` | `40` | Uploads per window per IP |

#### Reconciliation tuning

Change these only with a clear reason — they alter financial outcomes.

| Variable | Default | Effect |
|---|---|---|
| `MATCH_TOLERANCE_PAISE` | `100` (₹1) | Differences at or below this count as exact |
| `DELAY_THRESHOLD_DAYS` | `7` | Days past expected before a payment is "delayed" |
| `AUTO_ALLOCATE_SCORE` | `85` | Match score at or above which allocation auto-confirms |
| `REVIEW_ALLOCATE_SCORE` | `55` | Below this nothing is auto-posted; a person decides |

Raising `AUTO_ALLOCATE_SCORE` sends more to human review (safer, slower). Lowering
`REVIEW_ALLOCATE_SCORE` auto-posts weaker matches — not recommended.

#### Localisation

| Variable | Default |
|---|---|
| `DEFAULT_LOCALE` | `en` (`en` or `hi`) |
| `DEFAULT_CURRENCY` | `INR` |
| `TIMEZONE` | `Asia/Kolkata` |

#### Optional AI layer

Several providers are supported, including free tiers — see
[Is the AI required?](#is-the-ai-required).

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `auto` | `auto`, `groq`, `gemini`, `openrouter`, `ollama`, `anthropic`, `none` |
| `GROQ_API_KEY` | unset | **Free tier.** https://console.groq.com/keys |
| `GEMINI_API_KEY` | unset | **Free tier.** https://aistudio.google.com/apikey |
| `OPENROUTER_API_KEY` | unset | Free models available. https://openrouter.ai/keys |
| `ANTHROPIC_API_KEY` | unset | Paid |
| `OLLAMA_HOST` | `http://localhost:11434` | Local, offline, no key |
| `LLM_MODEL` | provider default | Blank uses a sensible default per provider |
| `LLM_ENABLED` | `true` | Master switch; ineffective without a provider |
| `LLM_TIMEOUT` | `45` | Seconds |
| `LLM_MAX_TOKENS` | `1600` | |

`auto` selects whichever key is present, preferring the free tiers. Verify any
provider with `python manage.py testllm`.

### 4. Database

**SQLite (default).** What ships, and what the 154-test suite runs against. WAL mode,
enforced foreign keys, 30-second busy timeout. Suitable for a single office or district
— tens of thousands of transactions.

```ini
DATABASE_URL=sqlite:////absolute/path/to/data/reconciliation.db
```

Note the four slashes for an absolute path.

**PostgreSQL.** The data layer is portable and the model uses no SQLite-specific types.
Install a driver first — none ships:

```bash
.venv/bin/python -m pip install "psycopg[binary]"      # Windows: .venv\Scripts\python.exe -m pip install "psycopg[binary]"
```

```ini
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/reconciliation
```

> **Honest status:** PostgreSQL is supported by the data layer but has **not** been
> exercised in this build. Run the full test suite against your instance before trusting
> it with real data: `DATABASE_URL=<your-url> python -m pytest`.

Create the schema:

```bash
.venv/bin/python manage.py init
```

There is no migration framework. `init` is create-only and will not alter existing
tables. Schema changes between versions require a documented migration — see
[Upgrades](#upgrades).

### 5. Running the service

```bash
.venv/bin/python manage.py serve --host 127.0.0.1 --port 8000
```

Bind to `127.0.0.1` and put a reverse proxy in front. Do not expose the application
port directly.

#### systemd (Linux)

`/etc/systemd/system/reconciliation.service`:

```ini
[Unit]
Description=Salary & Payment Reconciliation System
After=network.target

[Service]
Type=simple
User=app
Group=app
WorkingDirectory=/opt/reconciliation
EnvironmentFile=/opt/reconciliation/.env
ExecStart=/opt/reconciliation/.venv/bin/python manage.py serve --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/reconciliation/data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now reconciliation
sudo systemctl status reconciliation
journalctl -u reconciliation -f
```

#### Windows service

Run as a service with [NSSM](https://nssm.cc/):

```
nssm install Reconciliation "D:\reconciliation\.venv\Scripts\python.exe" "manage.py serve --host 127.0.0.1 --port 8000"
nssm set Reconciliation AppDirectory "D:\reconciliation"
nssm set Reconciliation Start SERVICE_AUTO_START
nssm start Reconciliation
```

### 6. Reverse proxy and TLS

nginx:

```nginx
server {
    listen 443 ssl http2;
    server_name reconciliation.example.gov.in;

    ssl_certificate     /etc/letsencrypt/live/example/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example/privkey.pem;

    # Must be at least MAX_UPLOAD_MB
    client_max_body_size 30M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Statement imports and annual reports can be slow
        proxy_read_timeout 300s;
    }
}

server {
    listen 80;
    server_name reconciliation.example.gov.in;
    return 301 https://$host$request_uri;
}
```

Set `COOKIE_SECURE=true` once TLS is in place, or sessions will not persist.

The application already sends `X-Content-Type-Options`, `X-Frame-Options`,
`Referrer-Policy` and a strict `Content-Security-Policy`. It adds
`Strict-Transport-Security` when `COOKIE_SECURE=true`. Do not duplicate these in nginx.

### 7. Create the first administrator

```bash
.venv/bin/python manage.py createadmin
```

Prompts for organisation name, email, full name and password. Non-interactive:

```bash
.venv/bin/python manage.py createadmin \
  --org "ICDS Uttar Pradesh" --email admin@example.gov.in \
  --name "Programme Officer" --password '<strong-password>'
```

Then, signed in as that administrator:

1. Add locations, departments, schemes and designations.
2. Upload the government orders that fix the rates.
3. Build salary rules against those orders — a rule with no source cannot be activated.
4. **Simulate** each rule before activating it.
5. Add employees and their bank accounts.
6. Upload bank statements, or let each worker upload their own.
7. Run **Process everything**.

Until an account exists, the login page displays setup instructions rather than an
unexplained locked door.

---

## Operations

### Command line

```bash
python manage.py init          # create the database schema
python manage.py createadmin   # create an administrator
python manage.py process       # reconcile every employee
python manage.py monitor       # periodic inspection + notifications
python manage.py status        # ledger summary
python manage.py testllm       # check the AI provider responds
python manage.py serve         # start the web application
python manage.py seed          # demo data — never in production
python manage.py reset --yes   # delete ALL data

python backup.py create --to /backups    # database + evidence vault
python backup.py verify <file>           # check a backup is usable
python backup.py list --in /backups
python backup.py prune --in /backups --keep 30
```

On Windows prefix these with `.venv\Scripts\python.exe` instead of `python`,
unless your virtual environment is already activated.

### Scheduled inspection

`monitor` re-scans for anomalies and raises notifications. Run it nightly.

cron:

```cron
30 2 * * * cd /opt/reconciliation && .venv/bin/python manage.py monitor >> data/monitor.log 2>&1
```

Windows Task Scheduler:

```
schtasks /create /tn "Reconciliation Monitor" /tr "D:\reconciliation\.venv\Scripts\python.exe D:\reconciliation\manage.py monitor" /sc daily /st 02:30
```

Notifications are de-duplicated, so a nightly run does not re-alert on unchanged facts.

### Backup and restore

Back up **both** the database and the evidence vault.

`backup.py` handles both halves in one command and works on every platform.
It uses SQLite's online backup API, so it is safe to run while the application
is serving traffic.

```bash
python backup.py create --to /backups     # database + evidence vault
python backup.py list   --in /backups     # what exists
python backup.py verify /backups/db-20260827-154437.db
python backup.py prune  --in /backups --keep 30
```

`create` writes two files sharing one timestamp — `db-<stamp>.db` and
`documents-<stamp>.tar.gz`. **Restore them together, using the same stamp.**

Always verify before trusting a backup. `verify` runs an integrity check and
reports the row counts:

```
Integrity : ok
Tables    : 46
  employees            3
  bank_transactions    16
  reconciliations      24
```

PostgreSQL: use `pg_dump` for the database, and `backup.py create` still handles
the evidence vault.

#### Nightly job

Linux — add to cron:

```cron
0 2 * * * cd /opt/reconciliation && .venv/bin/python backup.py create --to /backups >> data/backup.log 2>&1
30 3 * * * cd /opt/reconciliation && .venv/bin/python backup.py prune --in /backups --keep 30 >> data/backup.log 2>&1
```

Windows — Task Scheduler:

```
schtasks /create /tn "Reconciliation Backup" ^
  /tr "D:\reconciliation\.venv\Scripts\python.exe D:\reconciliation\backup.py create --to D:\backups" ^
  /sc daily /st 02:00
```

#### Restore

```bash
sudo systemctl stop reconciliation                        # Windows: nssm stop Reconciliation

cp /backups/db-<stamp>.db data/reconciliation.db
rm -f data/reconciliation.db-wal data/reconciliation.db-shm   # stale WAL must go
tar xzf /backups/documents-<stamp>.tar.gz -C data

sudo systemctl start reconciliation                       # Windows: nssm start Reconciliation
.venv/bin/python manage.py status                         # confirm the ledger
```

On Windows, replace the middle three commands with:

```powershell
Copy-Item D:\backups\db-<stamp>.db data\reconciliation.db -Force
Remove-Item data\reconciliation.db-wal, data\reconciliation.db-shm -ErrorAction SilentlyContinue
tar -xzf D:\backups\documents-<stamp>.tar.gz -C data
```

Restore with the **same `ENCRYPTION_KEY`** that was in use when the backup was taken,
or bank account numbers will not decrypt.

### Health and monitoring

`GET /healthz` returns `{"status":"ok"}` — use it for uptime checks and load balancers.
It requires no authentication and touches no employee data.

Watch for:

| Signal | Meaning |
|---|---|
| `/healthz` non-200 | Service down |
| Jobs stuck in `PROCESSING` | Worker wedged; restart the service (jobs requeue) |
| Growth in `data/` | Evidence vault filling the disk |
| Repeated 429s | Rate limiting triggering — legitimate load or abuse |
| `LOGIN_FAILED` bursts in the audit log | Credential guessing |

Logs go to stdout (`journalctl -u reconciliation`). Application events are also in the
`audit_logs` table, visible at `/audit` to administrators and auditors.

### Upgrades

```bash
sudo systemctl stop reconciliation
# take a backup first — see above
git pull
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest          # must pass before restarting
.venv/bin/python manage.py init     # create-only; safe
sudo systemctl start reconciliation
```

`init` never alters existing tables. If a release changes the schema, its notes will
carry an explicit migration step. Always take a backup first.

---

## Security

### What is implemented

- **Passwords** — bcrypt, cost 12. Passphrases over 72 bytes are pre-hashed so they keep
  full entropy instead of being silently truncated.
- **Account numbers** — Fernet-encrypted at rest; only the last four digits are ever
  rendered, including in generated dispute statements.
- **Authorisation** — role scoping is applied **inside the SQL query**, not in templates,
  so a route cannot accidentally return rows the user may not see.
- **CSRF** — tokens derived from the session on every state-changing form.
- **Sessions** — signed, expiring, `HttpOnly`, `SameSite=Lax`.
- **Uploads** — validated by magic number, not extension. Executables and script
  shebangs rejected. Extension/content mismatch rejected. Path traversal neutralised.
- **Rate limiting** — separate budgets for sign-in, uploads and general requests.
- **Account lockout** — after `MAX_LOGIN_ATTEMPTS` failures.
- **Audit log** — append-only: who, what, when, before, after, why, source. No update or
  delete path exists anywhere in the code.

### Roles

| Role | Access |
|---|---|
| `SUPER_ADMIN` | Everything in the organisation |
| `DEPARTMENT_ADMIN` | Their department |
| `DISTRICT_ADMIN` / `BLOCK_ADMIN` | Their location subtree |
| `EMPLOYEE` | Exactly one employee record — their own |
| `AUDITOR` | Everything, read-only, full evidence access |
| `READ_ONLY` | Read-only |

Cross-employee access, privilege escalation, CSRF forgery and malicious uploads are
covered by 27 tests in `tests/test_security.py`.

### Verification levels

| Level | Meaning |
|---|---|
| 0 | Someone typed it in (cash entries start here) |
| 1 | An uploaded bank statement shows the credit |
| 2 | An official sanction or payment document |
| 3 | An official government portal record |
| 4 | An authorised government API |
| 5 | Two independent official sources agree |

Level 2 and above **require an attached document** — enforced in code, not just the UI.
Level 4 cannot be set by hand at all: there is no fake government integration in this
system. Where none exists, the honest path offered is "upload the official record".

### Deployment checklist

- [ ] `SECRET_KEY` set explicitly
- [ ] `ENCRYPTION_KEY` set explicitly and recorded somewhere permanent
- [ ] `DEBUG=false` and `ENVIRONMENT=production` (verify `/openapi.json` returns 404)
- [ ] `COOKIE_SECURE=true` and TLS terminating at the proxy
- [ ] `.env` is `chmod 600`, owned by the service user
- [ ] Application bound to `127.0.0.1`, not `0.0.0.0`
- [ ] `client_max_body_size` ≥ `MAX_UPLOAD_MB`
- [ ] Demo data **not** seeded
- [ ] Default demo accounts absent (`manage.py status` shows your real organisation)
- [ ] Nightly backup covering database **and** `data/documents/`
- [ ] A restore actually tested end to end
- [ ] `data/` not served by the web server directly

### Is the AI required?

**No.** Every figure comes from deterministic engines. With no provider configured
nothing is lost from the reconciliation — the assistant answers from structured queries
instead of prose.

A provider enables one narrow thing: rewording an answer that has *already been
computed*. The model is given the figures and forbidden to change them, and any
response introducing a number absent from the source is **discarded entirely** rather
than shown. That guard is asserted by `test_invented_figures_are_discarded`.

The database is the authority. The AI is never the source of a financial fact.
When nothing is configured, the interface says so plainly rather than implying AI
is at work.

### Enabling it for free

| Provider | Cost | Get a key |
|---|---|---|
| **Groq** | Free tier, very fast | https://console.groq.com/keys |
| **Google Gemini** | Free tier | https://aistudio.google.com/apikey |
| **OpenRouter** | Free models available | https://openrouter.ai/keys |
| **Ollama** | Free, fully offline, no key | https://ollama.com |
| Anthropic | Paid | https://console.anthropic.com |

Add one line to `.env`:

```ini
GROQ_API_KEY=gsk_your_key_here
```

Then confirm it actually works — this calls the provider for real, so it proves the
key is valid rather than merely present:

```bash
python manage.py testllm
```

```
Provider : Groq (groq)
Model    : openai/gpt-oss-20b
Key      : gsk_ab...cd12
------------------------------------------------------------
PASS  Groq responded using model 'openai/gpt-oss-20b'.
```

Restart the application and the assistant will phrase its answers in natural language.
The figures behind them do not change.

**Fully offline:** install [Ollama](https://ollama.com), run `ollama pull llama3.2`,
set `LLM_PROVIDER=ollama`. No key, no internet, no data leaving the machine — worth
considering given the sensitivity of payment records.

---

## Performance and scaling

Measured shape on the reference build:

| Employees | Database | Notes |
|---|---|---|
| Up to ~500 | SQLite | Comfortable on modest hardware |
| ~500–5,000 | SQLite or PostgreSQL | Move to PostgreSQL if writes contend |
| 5,000+ | PostgreSQL | Required |

Design choices that carry the load: pagination on every list, indexes on the hot
query paths (employee+date, organisation+status, UTR, period ordering), background
processing for imports and reports, and money as integers so no decimal work happens
per row.

To scale beyond one process, three things must change together:

1. Move the job queue to a shared broker (Redis / RQ / Celery) and drop the in-process
   worker and startup orphan-requeue.
2. Move the rate limiter to the reverse proxy or a shared store.
3. Move to PostgreSQL.

Until then, one worker process is the supported configuration.

---

## Troubleshooting

**Login page shows "No accounts exist yet"**
Expected on a fresh install. Run `manage.py createadmin`.

**Sessions do not persist / immediately logged out**
`COOKIE_SECURE=true` without HTTPS. Either terminate TLS or set it `false`.

**All bank accounts show `XXXXXXXXXX`**
`SECRET_KEY` was rotated without a fixed `ENCRYPTION_KEY`. Restore the previous key value
— the ciphertext is intact and will decrypt again once the original key is restored.
This is why `ENCRYPTION_KEY` should be set from day one.

**Upload rejected: "content does not match a supported format"**
Working as intended — the file's magic number contradicts its extension. Re-export from
the bank rather than renaming.

**Upload fails at the proxy with 413**
`client_max_body_size` is below `MAX_UPLOAD_MB`.

**"No transactions could be read from page(s) 3, 4"**
Those pages are scanned images with no text layer. Upload a text-based PDF, or enter
those rows manually. The rest of the statement imported normally.

**A month shows `NO_ENTITLEMENT_RULE`**
No active salary rule matches that employee for that period. Check `/data-quality`,
then `/rules`. A rule needs a source before it can be activated.

**A payment sits in "review required"**
The evidence did not identify a salary month confidently. Open the transaction — the
candidate months and their reasons are listed. Confirming one teaches the organisation's
pay lag for future matches.

**`testllm` fails with "model does not exist or you do not have access"**
The provider retired that model name, or your account does not offer it. Run
`python manage.py testllm --models` to list what your key can actually use, then set
`LLM_MODEL` in `.env` to one of them.

**`testllm` says the model "used its whole token budget on internal reasoning"**
Reasoning models (gpt-oss, o-series) think before answering. Raise `LLM_MAX_TOKENS`
to 2000, or pick a non-reasoning model.

**AI answers work sometimes and not others (HTTP 429)**
Free tiers cap tokens per minute. The system retries once, then falls back to the
deterministic answer — the figures are identical either way, only the phrasing
differs. No action needed unless it is constant.

**Job stuck in `PROCESSING`**
Restart the service; interrupted jobs are requeued at startup.

**`database is locked`**
Another process holds the SQLite file — usually a second instance. Confirm only one
service is running. Under sustained write contention, move to PostgreSQL.

**Hindi renders as boxes**
The host lacks a Devanagari font. Install `fonts-noto-devanagari` (or the OS equivalent).
This is a client/OS font issue, not an application one.

---

## Testing

```bash
python -m pytest                              # 157 tests
python -m pytest tests/test_security.py -v    # one suite
DATABASE_URL=<your-url> python -m pytest      # against your database
```

| Suite | Covers |
|---|---|
| `test_reconciliation.py` | Every payment scenario: short, excess, split, multi-month, reversal, arrears, proration, retroactive rules |
| `test_security.py` | Access control, cross-employee leakage, escalation, uploads, injection, sessions |
| `test_financial_integrity.py` | No double counting, no silent deletion, totals reconcile with exports |
| `test_parsing.py` | Statement formats, date ambiguity, narrations, name matching, OCR honesty |
| `test_web.py` | The full user journey end to end |

The suite uses a throwaway database in a temp directory and never touches production data.

Run it before every deployment. It is the acceptance gate.

---

## Known limits

- **Single worker process** — see [the constraint above](#1-run-a-single-web-worker-process).
- **No migration framework** — schema changes need documented manual migrations.
- **PostgreSQL untested in this build** — supported by the data layer; verify with the
  test suite before production use.
- **OCR needs Tesseract** — absent it, the system says so rather than guessing.
- **No government portal integrations ship.** The framework accepts them and enforces
  levels 3–5, but none are fabricated.
- **Onboarding is CLI-first** — organisation setup runs through `manage.py createadmin`
  plus the admin screens, not an 11-step guided web wizard.
- **Notifications are in-app only.** Email and SMS require configuring an authorised
  provider; no gateway ships.

---

## Further reading

`docs/ARCHITECTURE.md` — data model, the reconciliation chain, why specific design
decisions were made, and the import pipeline in detail.

---

## What this system will not do

It will not tell you a payment was officially verified when no official document exists.
It will not convert an uncertainty into a certainty to make a screen look cleaner. It
will not accuse anyone of anything.

It finds and explains differences. Deciding what they mean remains a human job.
