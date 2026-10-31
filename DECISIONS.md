# Decisions

This is why things are built the way they are, what I gave up for it, what I didn't build, and what I know is weak. Where the brief left something open and I had no answer, I wrote down the assumption I made.

## 1. The Fineract customization: a new `SYNC` command

**What.** A new endpoint, `POST /glaccounts/sync`, backed by a new command (`GLACCOUNT` / `SYNC`) with its own handler, its own permission (`SYNC_GLACCOUNT`), a lookup by `external_id`, a conflict exception mapped to 409, and one Liquibase changelog. It's about 15 files, all in `fineract-customization/patches/erp-gl-sync.patch`.

**First, what 1.15.0 already had.** Clients, loans and loan transactions all have an `external_id` column. GL accounts (`acc_gl_account`) don't, so the column and its unique constraint had to be added.

The brief offered three ways to do it. Here's why I picked a new endpoint:

| Option | Why not |
|---|---|
| Change the existing `POST /glaccounts` | Other clients (the Mifos web app, other scripts) rely on how create works today: a duplicate `glCode` is an error, and there's no "return the existing one". Changing that quietly would surprise them. It would also tie sync to `CREATE_GLACCOUNT`. |
| A Fineract-side component (a scheduler pulling from the ERP, or an event listener) | It puts ERP knowledge inside the core banking system, makes Fineract depend on the ERP being up, and is harder to test. Fineract shouldn't need to know the ERP exists. |
| **New command and endpoint** (chosen) | Existing behaviour stays exactly as it was. It goes through the normal command pipeline, so permission checks, `@Transactional`, the audit trail in `m_portfolio_command_source` and the error mapping all work like any other Fineract write. And it gets its own permission. |

The separate permission is a real benefit. The integration service's API user has `SYNC_GLACCOUNT` but **not** `CREATE_GLACCOUNT`, so it can only create GL accounts that carry an ERP reference. It can't make arbitrary ones.

**How "same payload" is decided.** Every field the request can set is compared: name, glCode, type, usage, manualEntriesAllowed, disabled, description, parent and tag. A stored empty description counts as the same as a missing one (`9190854` fixed a false 409 there).

**The race.** Two syncs for a new reference can both find nothing and both insert. The unique constraint makes one fail. I turn that failure into a `ConcurrencyFailureException` on purpose, because Fineract's command processing already retries that exception (`fineract.retry.instances.executeCommand`, 3 attempts with backoff). On the retry the lookup finds the row and returns it. So two concurrent identical syncs both get 200, and neither gets a 500.

**Where the logic runs, and why the transaction boundary is right.** `syncGLAccount` runs inside the handler's transaction. The lookup, the comparison and the insert are all in that one transaction, and the database's unique constraint is the final word. Nothing is written outside it, so a failure rolls the whole sync back.

## 2. GL sync on the Python side

- **Trigger: an explicit call** (`POST /api/accounts/sync` and `/sync/all`). Polling or a webhook would also work. But the brief says GL accounts must exist before the loan product uses them, and an explicit call in `make setup` makes that order obvious and easy to check: the script fails if any account isn't `SYNCED`. In production I'd add a webhook from the ERP calling the same endpoint.
- **Create-only**, as the brief says. Renames and deletions don't flow through.
- **Type mapping in one place** (`app/gl_mapping.py`), as an explicit dictionary with tests. An unknown type fails the sync instead of being guessed.
- **Retries:** 5xx and connection errors are temporary and retried with backoff. A 4xx is permanent (`FAILED`), because sending the same thing again won't change Fineract's answer. Calling sync on a `FAILED` account starts it again from zero, which is how someone retries after fixing the cause.
- **Currency isn't sent to Fineract.** Fineract GL accounts have no currency field; currency lives on the product. The ERP's currency is stored in the sync row, and the ERP itself rejects entries whose currency doesn't match the account. Everything here is RWF.

## 3. Sync vs async

The partner's request is **synchronous up to Fineract, then async only if the ERP is slow or down.**

- The caller waits while we post to Fineract, because Fineract decides whether the money movement is allowed at all: loan state, amounts, dates. If it says no, the caller needs to know right away (422), and nothing has been posted anywhere.
- Then we try the ERP once, inline. Usually it works and the caller gets 201 `COMPLETED` in one round trip.
- If the ERP is down, the caller gets 202 `ERP_PENDING` and the background loop finishes the job. The money is real at that point (Fineract has it), so the ERP posting is a "must happen eventually" task. Making the teller wait for the ERP would only add outages.

A fully async design (accept everything into a queue and answer 202) would take more load. But then a rejected repayment would only show up later, and the teller would need a second channel to find out. For a teller app at a counter, an immediate answer from Fineract matters more.

## 4. Database constraints

I rely on the database for every rule that must never be broken, and on code only for the friendly error message. Code checks have race windows; constraints don't.

| Rule | Constraint |
|---|---|
| One client per `client_uid` | `clients.client_uid` unique, plus Fineract client `external_id` unique |
| One loan per application reference | `loans.external_reference` unique |
| A loan approved once | `loans.approval_reference` unique, plus a check of the loan's state |
| A transaction reference used once | `loan_transactions.external_reference` unique, plus Fineract `m_loan_transaction.external_id` unique |
| **One disbursement per loan** | partial unique index `one_active_disbursement_per_loan ON loan_transactions(loan_id) WHERE type = 'DISBURSEMENT' AND status <> 'FAILED'` |
| One idempotency record per operation | unique (`operation`, `external_reference`) |
| One ERP entry per reference | `journal_entries.external_reference` unique |
| ERP lines are sane | check constraints: amounts ≥ 0, exactly one of debit or credit, total > 0 |
| One GL account per ERP reference in Fineract | `acc_gl_account_external_id_key` unique |

The disbursement index excludes `FAILED`. If Fineract rejected a disbursement (say, the wrong date), the loan can still be disbursed with a new reference.

**No Alembic.** Tables are created with SQLAlchemy's `create_all` at startup. For a take-home that's always started from an empty database (`make reset`) that was enough, and it kept the moving parts down. The cost showed up during development: when I added columns (`overpayment_portion`, `erp_attempts`, `next_attempt_at`, `erp_transaction_id`), `create_all` doesn't alter existing tables, and I had to `ALTER` my local database by hand. For anything real I'd add Alembic migrations, as the Fineract side does with Liquibase.

## 5. Idempotency

**The mechanism that prevents double processing**, step by step, for a repayment:

1. `INSERT` into `idempotency_records (operation, external_reference, request_hash, status='IN_PROGRESS')`. The unique key means only one request can win.
2. A request that loses the insert reads the existing record:
   - different hash: 409 `IDEMPOTENCY_CONFLICT`
   - same hash, `COMPLETED`: replay the stored status and body (201 becomes 200)
   - same hash, `IN_PROGRESS`: 202, and nothing is started
3. The winner inserts the `loan_transactions` row. Its unique reference, plus the disbursement index, catch anything that slipped past step 1, for example a different reference for the same disbursement.
4. Under the per-loan advisory lock, it looks the reference up in Fineract before sending. If Fineract already has it, nothing is sent.
5. The ERP post carries the same reference, and the ERP deduplicates on it.

**Details worth knowing:**

- The hash is SHA-256 of the canonical JSON body (sorted keys) plus the loan reference from the URL. So the same repayment body sent to a different loan is a conflict, not a replay.
- **A 202 is never stored as the final answer.** If the ERP was down, the record is released instead of completed. The next identical request re-checks the real state and gets 201 or 200 once it's done, instead of a stale 202 forever.
- **The 60-second lease.** If the process dies mid-request, its record stays `IN_PROGRESS`. Without a lease, that reference would answer 202 forever. After 60 s a new identical request can take the record over, using an `UPDATE … WHERE updated_at < now - 60s RETURNING`, so only one taker wins. 60 s is longer than any normal request: the Fineract and ERP timeouts are 10 s each, and the loan lock waits 30 s at most.
- **Scope.** References are unique per operation, so a client reference and a repayment reference can be the same string without clashing.

## 6. Locking

**A Postgres advisory lock per loan** (`pg_advisory_lock(1, loan_id)`) is held while we post to Fineract.

I didn't plan this. I found it. The first version had no lock, and the I3 test (10 repayments × 2 at once) left only 1 repayment applied and 8 `FAILED` with "Loan is locked by the COB job". Fineract 1.15 takes its own lock on a loan while it writes, and refuses any other write on that loan with a 409. It isn't really about the COB job; the message is just misleading. Evidence: `evidence/part-i/before-fix-fineract-loan-lock.log`.

The fix has two parts:

- **Serialise writes per loan ourselves**, so Fineract never sees two at once.
- **Treat that 409 as "busy, retry"**, not as a rejection, in case something else (the Fineract UI, a COB job) is writing to the loan.

**Why an advisory lock** and not `SELECT … FOR UPDATE` on the loan row:

- It's held on a separate connection, so the main session can commit its state changes while the lock is held.
- It doesn't block readers of the `loans` table.
- It has a timeout (`lock_timeout` 30 s): after that the caller gets 503 `LOAN_BUSY` instead of hanging.

**The trade-off:** we hold a lock across an HTTP call, which is usually a smell. Here it's deliberate. The call is bounded by a 10-second timeout, it's per loan (other loans aren't affected), and the alternative is Fineract rejecting the writes anyway. Throughput per loan is one write at a time, which is plenty: a single SME loan doesn't get dozens of repayments a second.

For the ERP step, the transaction row is locked with `FOR UPDATE` while posting, so the background loop and a caller's retry can't both post it. The ERP's own idempotency would catch a double post anyway, so this lock is about not wasting attempts.

## 7. Retries and partial failure

The only partial failure that matters is **Fineract succeeded and the ERP didn't**. The money moved, but the ledger doesn't show it yet. Everything is designed around that:

- **The state machine** (ARCHITECTURE.md) commits each step before the next network call, so after any crash the row says exactly where it stopped.
- **Retry policy for the ERP:** backoff of 2, 4, 8, 16, 32, then 60, 60, 60, 60 s, so 10 attempts and about 5 minutes. The ERP is idempotent, so retrying is always safe. After 10 attempts the transaction moves to `NEEDS_RECONCILIATION` and shows up in `GET /api/admin/transactions?status=NEEDS_RECONCILIATION`.
  - **Why 10 attempts:** most ERP blips are seconds to minutes, so 10 attempts covers a restart or a short deploy. Something longer is an incident, and someone should be looking anyway. Reconciliation can still repair it later.
- **Retry policy for Fineract:** never blind. Every path that could send looks up the `externalId` first. The background sweep only looks up; it never sends a transaction to Fineract by itself.
  - If a request died before Fineract got it, the row stays `FINERACT_PENDING`. The caller can resend safely.
  - If nothing shows up in Fineract for **15 minutes**, the sweep marks it `FAILED` ("never applied"). 15 minutes is far longer than any request can be in flight (10 s timeout), so by then we know it never landed.
- **What a 4xx from the ERP means:** the entry is wrong, so retrying won't help. It goes straight to `NEEDS_RECONCILIATION`.
- **The recovery sweep** runs at startup and then every 5 s, and can also be triggered with `POST /api/admin/recovery`.

**Fault injection** is at the HTTP transport layer (`app/faults.py`). It wraps the real `httpx` transport, so the real request really reaches Fineract or the ERP, and then we pretend the response was lost (`timeout_after`) or kill the process (`crash_after`). That's the only honest way to test "did it go through?". Mocking the client would never actually write to Fineract. It's off unless `FAULT_INJECTION_ENABLED=true` and needs the admin key.

## 8. Accounting

- **Cash-based accounting in the Fineract product.** The brief allows either. With cash-based, Fineract books interest when it's paid, which is exactly what our ERP entries do: interest income is credited on repayment from Fineract's split. With accrual, Fineract would book interest every day and the ERP would need accrual entries too, which roughly doubles the mapping and the reconciliation for no gain in this exercise.
- **The split comes from Fineract.** Every journal line comes from the portions on Fineract's transaction (`principalPortion`, `interestPortion`, `feeChargesPortion`, `penaltyChargesPortion`, `overpaymentPortion`). We never calculate interest. The mapping (`app/accounting.py`) is a pure function, with tests, and refuses if the portions don't add up to the amount.
- **Overpayments** are credited to 2100 Overpayment Liability: the customer paid more than they owed, so we owe it back. Tested in the mapping, but not shown live.
- **Reversals** post a mirror entry `REV-<reference>` with debits and credits swapped, never a deletion. Reconciliation compares the net amounts, and its repair posts the mirror entry.
- **Loan product terms:** 18% a year, 12 monthly repayments, declining balance, equal instalments, **360 days a year / 30 days a month**. 360/30 gives equal monthly interest periods, which is common for SME loans and keeps the schedule easy to check by hand. That's why the first instalment's interest is exactly 75,000 (5,000,000 × 18% / 12).
- **10 GL accounts**, not just the 3 the brief mentions. Fineract's cash-based product needs an account for every mapping slot (fund source, portfolio, suspense, overpayment, interest, fees, penalties, recovery, write-off, goodwill), so the ERP seeds all of them and the product uses them all.

## 9. Reconciliation

- **Keying:** one Fineract disbursement or repayment matched to one ERP entry with the same `external_reference`. It's per loan the integration service knows about, scoped by date range, loan references or transaction references.
- **Only types 1 and 2** (disbursement and repayment) are compared. Fineract also creates an `ACCRUAL` transaction (type 10) when a loan closes, even on a cash-based product. It isn't a money movement and has no ERP counterpart, so it's ignored on purpose.
- **Reversals:** a reversed Fineract transaction counts as 0. An ERP entry plus its `REV-` entry also counts as 0. So a reversal done on both sides is `MATCHED`, and one done only in Fineract is an `AMOUNT_MISMATCH` that can be repaired.
- **Two extra statuses** beyond the brief's four: `TYPE_MISMATCH` and `SPLIT_MISMATCH`. Without them, a repayment whose total matches but whose principal/interest split differs would show as `MATCHED`, and the interest income in the ERP would be wrong.
- **Read-only by default.** Repairs are a separate call, one reference at a time, and idempotent. Only `MISSING_IN_ERP` and an unmirrored Fineract reversal are repaired automatically, because there Fineract is clearly right and the fix is purely additive. Everything else needs a person, because the system can't tell which side is wrong.
- **SPLIT_MISMATCH is manual-only.** When a repayment is reversed, Fineract re-allocates the other repayments on the same date, so their split changes after they were posted. The fix would be to reverse and re-post those entries in the ERP. I left that as manual, because doing it automatically means rewriting history in the ledger.

## 10. Security

- **API keys** in `X-API-Key`, compared in constant time. There's a teller key and a separate admin key for recovery, the stuck list and fault injection. If no keys are configured, everything is refused. I chose keys over OAuth because this is system-to-system traffic: one key per calling system is simple and enough. In production I'd load the keys from a secrets manager, or use mTLS.
- **Least-privilege Fineract user.** `integration_service` has 13 permissions: create/read/activate clients, create/approve/disburse/read loans, repayments, read the product, office, GL and journal, and `SYNC_GLACCOUNT`. It can't create offices (tested in `make setup`: 403), can't create GL accounts outside sync, and can't reverse or write off.
- **Fineract IDs never leave the service.** They're stored in the mapping tables and in the logs (useful for debugging), but never in API responses.
- **The mock ERP has no auth.** It's an internal service on the Docker network. A real ERP would need its own credentials.

## 11. Observability, and "What happened to FIN-2026-000001?"

Both Python services write one JSON object per line, with `timestamp`, `service`, `event` and, wherever they apply: `correlation_id`, `external_reference`, `client_uid`, `loan_reference`, `fineract_transaction_id`, `erp_transaction_id`, `status`.

- **Two IDs, two jobs.** `external_reference` follows a **payment** across its whole life, including retries the background loop does minutes later. `correlation_id` follows one **HTTP request** across all three services (it's forwarded to Fineract and the ERP). Background passes use `recovery-<uuid>` so you can tell their work apart from a caller's.
- **Every status change is a log line** (`transaction_status_changed`, with `from_status` and `status`), written by the same function that enforces the state machine. So the logs can't disagree with the database.

**How an engineer answers the question:**

Step 1. One API call gives the current state:

```bash
curl -s localhost:8080/api/transactions/FIN-2026-000001 -H 'X-API-Key: …' | jq
```

```json
{"external_reference": "FIN-2026-135721", "loan_reference": "LN-2026-000015", "type": "REPAYMENT", "status": "COMPLETED",
 "amount": 458400, "principal_portion": 383400, "interest_portion": 75000,
 "erp_transaction_id": "JE-000056", "erp_attempts": 2, "last_error": null}
```

So it's done, but it took two attempts to reach the ERP. Why?

Step 2. The logs from both services, filtered by the reference:

```bash
docker compose logs --no-log-prefix integration-service mock-erp \
  | jq -c 'select(type == "object" and .external_reference == "FIN-2026-000001")'
```

Here's the sample trail, from `make demo-trace` (`evidence/observability/trace.log`). The demo generates a fresh reference, `FIN-2026-135721`; the commands are the same for any reference.

```
11:57:23.390 integration-service transaction_received       corr=teller-135721  status=FINERACT_PENDING  LN-2026-000015 SME-235721
11:57:23.651 integration-service transaction_status_changed corr=teller-135721  FINERACT_PENDING → FINERACT_DONE  fineract_transaction_id=70
11:57:23.656 integration-service transaction_status_changed corr=teller-135721  FINERACT_DONE → ERP_PENDING
11:57:23.661 mock-erp            journal_entry_created      corr=teller-135721  erp_transaction_id=JE-000056
11:57:23.663 integration-service erp_post_failed            corr=teller-135721  ReadTimeout: response lost
11:57:26.982 mock-erp            journal_entry_replayed     corr=recovery-0c4c…  erp_transaction_id=JE-000056
11:57:26.985 integration-service transaction_status_changed corr=recovery-0c4c…  ERP_PENDING → COMPLETED  JE-000056
```

Reading it: the teller's request reached Fineract (transaction 70) and the ERP wrote `JE-000056`, but the ERP's answer never came back. So the teller got a 202. Three seconds later the background loop sent the same entry again, the ERP recognised it (`replayed`, not `created`), and the transaction completed. There's one journal entry, not two.

Step 3. For everything the teller's request did, filter by `select(.correlation_id == "teller-135721")`. That adds the `http_request` lines: the ERP returned 201, and our API returned 202.

## 12. Smaller decisions and assumptions

- **`RECEIVED` merged into `FINERACT_PENDING`.** The brief's example state machine starts with `RECEIVED`. Our row is written right before the Fineract call, so the two would always be the same state.
- **Approval is a separate endpoint** (`POST /api/loans/{ref}/approve`), not part of the application. In real life approval is a different person's decision.
- **Disbursement is always the full approved principal.** The product is single-tranche, so a partial amount would only invite mistakes.
- **Dates default to today** when the caller leaves them out.
- **Money is a whole number.** RWF has no minor unit (Fineract is set to 0 decimal places). A decimal amount from Fineract is refused, not rounded.
- **`client_uid` is `SME-` plus 6 digits** and `loan_reference` is `LN-<year>-<6 digits>`, issued by us from the local row ID. The year is the year the loan was applied for.
- **Reads go to Fineract.** Loan status and balances are fetched live; we don't cache them. Simpler and never stale, at the cost of a Fineract call per read.
- **Balances come from the loan summary**, never from the per-transaction `outstandingLoanBalance`. I noticed Fineract's per-transaction running balance is wrong when several transactions share a date.
- **One Postgres for everything locally,** with separate databases and users per service. In production each would be separate.

## 13. What I chose not to build

- **Write-offs, charge-offs, waivers, rescheduling.** The product's GL mapping already has the accounts (5100, 4300), but there's no API or ERP mapping for them. Adding write-off would mean: a new endpoint, a new transaction type in the state machine (it would follow the same path), and a mapping DR 5100 Write-off Expense / CR 1200 Loan Receivable for the principal written off, all from Fineract's breakdown.
- **Fees and penalties in the product.** The mapping handles them, but the product has none, as the brief allows.
- **GL updates and deletions**, and GL master-data reconciliation (Part L).
- **Scheduled reconciliation, Prometheus metrics, signed webhooks** (Part L).
- **A real queue** (Kafka, SQS) for the ERP posting. The `loan_transactions` table plus the background loop is effectively an outbox. For this volume it's simpler and has the same guarantees.
- **Alembic migrations** (see section 4).
- **Multiple tenants, multiple currencies, multiple offices.** All hard-wired to `default`, RWF and Kigali Main Office.

## 14. Known weaknesses

- **A 429 from the ERP is treated as a rejection.** Today any ERP 4xx sends the transaction to `NEEDS_RECONCILIATION`. A rate limit should be retried instead: treat 429 like a 5xx, honour `Retry-After`, and maybe add a client-side rate limiter. It's a small change in `gateways/erp.py` plus a test.
- **One background loop per instance, no leader election.** Two instances would both sweep. That's safe (row locks, `SKIP LOCKED` for GL sync, idempotent ERP), but wasteful. With more instances I'd move it to one scheduled job.
- **The 15-minute "never applied" rule assumes nothing reaches Fineract later.** If a request were somehow delayed more than 15 minutes in transit, we'd have marked it `FAILED` while Fineract applied it. Reconciliation would then show it as `MISSING_IN_ERP` and could repair it, so it's recoverable, not silent.
- **Fineract TLS isn't verified** (`FINERACT_VERIFY_TLS=false`) because of the self-signed certificate in the local stack. In production this must be on.
- **Default secrets in `docker-compose.yml`**: dev passwords and API keys as fallbacks, so the stack starts with no setup. Fine locally, never for a real deployment.
- **Reconciliation only sees loans the integration service knows.** A loan created directly in Fineract is invisible to it.
- **Reconciliation fetches every transaction of each loan in scope from Fineract.** That's fine for hundreds of loans, but too slow for a whole portfolio. It would need paging, or reading from Fineract's journal entries instead.
- **The ERP sees per-loan Fineract writes in order, but not across loans,** and nothing guarantees entries arrive in date order after a retry. A real ERP closing periods would need to handle back-dated entries.
- **"Today" is UTC in the service but Africa/Kigali in Fineract.** The container clock is UTC, so between 22:00 and 24:00 UTC a request without a date gets yesterday's date in Kigali terms. The fix is to compute today in the tenant's timezone.
- **No auth on the mock ERP** (section 10).

## 15. Fineract behaviour I ran into

These cost me time, and are worth knowing for anyone working on this:

1. **"Loan is locked by the COB job"** (409 `error.msg.loan.locked`) on concurrent writes to the same loan, even with no COB running. That's what led to the per-loan lock (section 6).
2. **Reversing a repayment re-splits other repayments on the same date.** Their principal/interest portions change after the fact, which is why `SPLIT_MISMATCH` exists.
3. **An `ACCRUAL` transaction (type 10) appears when a loan closes,** even on a cash-based product. Reconciliation ignores it.
4. **The per-transaction `outstandingLoanBalance` is unreliable** when transactions share a date. Use the loan summary.
5. **Fineract's loan state is the last line of defence.** Ten direct disbursements on one loan, bypassing our service entirely: 1 accepted, 9 refused with 403 `validation.msg.domain.rule.violation` (`make test-live`).
