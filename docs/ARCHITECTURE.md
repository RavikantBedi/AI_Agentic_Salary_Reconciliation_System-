# Architecture

## Layers

```
  Browser  ── server-rendered HTML, works without JavaScript
     │
  Routers  ── FastAPI: auth, RBAC, CSRF, rate limiting
     │
  Agents   ── deterministic engines (the entire financial brain)
     │
  Services ── parsers, importer, reports, jobs, storage
     │
  Models   ── SQLAlchemy 2.0, 46 tables
     │
  Database ── SQLite by default, PostgreSQL supported
```

The AI layer sits *beside* this stack, not inside it. It reads finished results and
rewords them. It has no write path to any financial column.

---

## Money

Every monetary value is an **integer number of paise**, in a column suffixed `_paise`.

Floats are never used for money. In a reconciliation system a float ledger silently
manufactures the very discrepancies the platform exists to detect: accumulate `0.10`
a thousand times in binary floating point and you do not get `100.00`. The test
`test_money_is_exact_never_float` pins this.

`split_evenly()` distributes remainder paise to the leading shares so a split always
sums *exactly* to the original — rounding can never break the allocation invariant.

---

## The reconciliation chain

```
SOURCE → EXTRACTION → NORMALIZATION → CLASSIFICATION → MATCHING
       → CALCULATION → RECONCILIATION → VERIFICATION → EXPLANATION → AUDIT
```

Ordering matters and is not arbitrary:

**Entitlements are computed before transactions are matched.** Month matching and match
scoring both compare a credit against the expected amount. Running entitlement
calculation afterwards starves the matcher of its strongest signal — during
development this silently suppressed nearly every allocation.

**Periods are reconciled oldest first.** An arrear created in an early month must exist
before a later payment can settle it.

---

## Data model

46 tables. The financially significant groups:

### Rules
`GovernmentOrder` → `SalaryRule` → `SalaryRuleComponent`

A rule is **data**, never code. It carries a scope (state, district, department,
scheme, designation, category), a validity window, a proration policy, and a source.
A rule with no source cannot be activated.

Scope columns are nullable, and `NULL` means "any value" — which is how a state-wide
default and a district-specific override coexist. Resolution scores specificity, so
the most precisely scoped rule wins. Two rules of equal specificity produce an
*ambiguity*, and the system refuses to choose.

### Periods and the ledger
`SalaryPeriod` → `Entitlement` → `EntitlementComponent`

Entitlements are append-only. A recalculation writes a new row with `supersedes_id`
pointing at the old one and flips the old row's `is_current` to `False`.

### The four legs
```
Entitlement      what the rule says is due
PaymentSanction  what the authority approved      (NULL = unknown, not zero)
Disbursement     what the treasury released       (NULL = unknown, not zero)
BankTransaction  what actually arrived
```

`Reconciliation` holds the comparison, the outcome, per-dimension verification levels,
delay statistics, and the bilingual explanation.

### Allocation
`PaymentAllocation` links a *slice* of a transaction to a period.

This is the model's most important design choice. It is what allows:
- one credit covering several months (many allocations, one transaction)
- one month paid in several credits (many allocations, one period)
- money that cannot be identified to stay visibly **unallocated**

The invariant — the sum of a transaction's non-rejected allocations never exceeds its
amount — is enforced in `allocation_agent.allocate()`, which raises rather than
trimming. Silent trimming would hide a bug in a financial system.

### Evidence
`Document` → `EvidenceLink` → any record
`VerificationRecord` → (entity, dimension, level)

Verification is **multi-dimensional**: `CREDIT_RECEIVED`, `OFFICIALLY_SANCTIONED`,
`DISBURSED`, `ENTITLEMENT_BASIS`. A bank statement raises the first and nothing else.

---

## Design decisions worth knowing

### Why is a fully-unpaid month not an "unexplained deduction"?
A deduction means money was paid but reduced. A month where nothing arrived is a
*missing payment*, already represented by an arrear and a `MISSING_MONTH` finding.
Recording it as a deduction would double-count it and misdescribe what happened.

### Why does the "not yet due" guard exist?
Without it the current month permanently reports itself as unpaid, and every real
finding drowns under a recurring false alarm. `is_period_due()` gates arrear creation,
unexplained-deduction creation, and the `NOT_PAID` verdict.

### Why does an explicit month bypass the score threshold?
The match score measures confidence in the **month**, not agreement on the **amount**.
When a narration names the month outright, the credit is recorded against that month
even if the amount differs — a short or excess payment is precisely the finding the
system exists to surface. Gating on the amount would hide it as "not paid".

### Why is the government marker weighted so low?
The specification forbids classifying a payment from the word "GOVT" alone. It
contributes 0.12 against a keyword's 0.55, and a transaction whose *only* signals are
a government marker and a reference number is returned as `UNKNOWN` regardless of
accumulated score.

### Why is an IFSC excluded from UTR extraction?
`SBIN0001234` identifies a *branch*, not a payment. Recording it as a transaction
reference would create false UTR matches between unrelated payments. Unlabelled tokens
must also contain a digit, so ordinary words like `WITHDRAWAL` are not mistaken for
references.

### Why are names never used as identifiers?
`name_similarity()` handles case, joined words and initials, but it only ever produces
*candidates*. Merging two employee records is a human decision requiring corroboration
(shared date of birth, designation, location) — and even then the system only suggests.

---

## Import pipeline

```
detect format → parse → validate → de-duplicate → normalise
→ match employee → classify → match month → reconcile → audit
```

**Row-level de-duplication** uses a SHA-256 of organisation, employee, date, direction,
amount, normalised narration and UTR. Deliberately excluded: the running balance —
some banks re-issue statements with recalculated balances, which would otherwise
create phantom rows.

Re-importing the same statement is therefore idempotent, while a genuine duplicate
*payment* is a different matter entirely and is flagged for a human, never deleted.

**Nothing is silently discarded.** Every unparseable row becomes an `ImportError` row
carrying its original text, shown on the import result page.

---

## Statement parsing

Handled because real Indian bank statements do all of it:

- separate Debit/Credit columns, a single signed Amount column, or Amount plus a Dr/Cr
  type column
- Indian digit grouping (`1,23,456.78`) and trailing `Cr`/`Dr` markers
- many date formats, day-first by default (`03/04/2026` is 3 April)
- multi-line narrations wrapping across PDF rows
- repeated page headers, footers, opening/closing balance and total lines
- OCR digit confusion (`1O,50O.OO` → `10,500.00`), applied only when the repaired
  string is a well-formed number, so real words are never mangled

PDFs try table extraction first, then fall back to line-based text parsing per page.
Pages yielding nothing are reported **by number**, so the user knows exactly what was
missed rather than trusting a silent partial import.

---

## OCR honesty

Most machines have no Tesseract binary. Rather than pretend, the document agent reports
`UNAVAILABLE` with specific guidance ("enter the values manually, or upload a PDF").

A PDF with no text layer is a scan; it is reported as such rather than yielding an
empty extraction that looks like a successful read of an empty document.

---

## Background jobs

A small in-process worker pool backed by the `ai_jobs` table — no Redis or Celery to
operate. Job state lives in the database, so progress survives a page reload and the
user can watch it.

A worker may pick up a job before the enqueuing request has committed. Rather than
coordinate transactions across sessions, the worker retries briefly until the row
becomes visible. Jobs left `PROCESSING` by a crash are requeued at startup.

---

## Authorisation

`employee_scope_filter()` returns SQLAlchemy conditions applied **inside the query**.

Filtering after the fact is how cross-employee leaks happen: one template that forgets
the check exposes everything. Applying it in the query means a route cannot
accidentally return rows the user may not see. An `EMPLOYEE` user resolves to exactly
one employee id, or none — never all.

---

## Known limits

- The rate limiter and job queue are process-local. Behind multiple workers, move the
  limiter to the reverse proxy and the queue to a shared broker.
- OCR for scanned images requires Tesseract; without it the system says so.
- No government portal integrations ship with the product. The framework accepts them
  (verification levels 3–5 exist and are enforced), but none are fabricated.
- SQLite suits a single office. Use PostgreSQL for a district or state deployment.
- Back up `data/documents/` alongside the database — the evidence vault is not
  reconstructible from the database alone.
