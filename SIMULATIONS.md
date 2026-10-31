# Simulations

Every simulation here is a script you can run again. Each one prints the state before and after, and the output of my last run is saved in `evidence/`. Each section follows the template from the brief:

1. Initial state · 2. Request · 3. Response · 4. Fineract state · 5. ERP state · 6. Integration state · 7. Expected result · 8. Actual result · 9. Explanation

Loan numbers, references and timestamps change every run because the scripts generate fresh references. The behaviour and the counts don't change.

To run everything from scratch:

```bash
make reset && make up && make setup
make demo-a demo-a-sync demo-clients demo-loan demo-erp test-live demo-failures demo-reconciliation demo-trace
```

| # | Simulation | Command | Evidence |
|---|---|---|---|
| A1 | GL sync inside Fineract (behaviours 1–4) | `make demo-a` | `evidence/part-a/demo-part-a.log` |
| A2 | GL sync from the ERP through the integration service | `make demo-a-sync` | `evidence/part-a/demo-part-a-sync.log` |
| B | Fineract configuration | `make setup` | `evidence/part-b/fineract-setup.log` |
| C | Client identities | `make demo-clients` | `evidence/part-c/demo-clients.log` |
| D | Loan lifecycle to Closed | `make demo-loan` | `evidence/part-d/loan-lifecycle.log` |
| G | Mock ERP journal | `make demo-erp` | `evidence/part-g/erp-journal.log` |
| H | Loan lifecycle with the ERP ledger | `make demo-loan` | `evidence/part-h/loan-lifecycle-with-erp.log` |
| I1–I3 | Duplicates and concurrency | `make test-live` | `evidence/part-i/parallel-tests.log` |
| I-bug | The Fineract loan lock, before the fix | (historical) | `evidence/part-i/before-fix-fineract-loan-lock.log` |
| J1–J5 | Failure scenarios | `make demo-failures` | `evidence/part-j/failure-scenarios.log` |
| K | Reconciliation | `make demo-reconciliation` | `evidence/part-k/reconciliation.log` |
| O | What happened to FIN-…? | `make demo-trace` | `evidence/observability/trace.log` |

The Java and Python test runs are in `evidence/part-a/` too (`fineract-unit.log`, `fineract-it.log`, `pytest-*.log`), along with the Docker build of the patched Fineract (`docker-build-erp-gl-sync.log`).

---

## A1. GL account sync inside Fineract

This calls the patched `POST /glaccounts/sync` on Fineract directly, as the admin user, to show the Java behaviours on their own.

1. **Initial state.** Patched Fineract running. No GL account with external id `GL-1200`.
2. **Request.**
   - `POST /glaccounts/sync {externalId: "GL-1200", glCode: "1200", name: "Loan Receivable", type: 1, usage: 1, manualEntriesAllowed: true}`
   - then the same request again
   - then the same `externalId` with name `Loans Receivable`
   - then a sync as a new user whose role lacks `SYNC_GLACCOUNT`, and again after the permission is granted
3. **Response.** 200 `{resourceId: 6, resourceExternalId: "GL-1200"}` · 200, same id 6 · 409 `error.msg.glaccount.externalid.conflict` · 403 `error.msg.not.authorized` · 200 once the permission is granted.
4. **Fineract state.** `acc_gl_account` has exactly one row with `external_id = 'GL-1200'`, and the constraint `acc_gl_account_external_id_key UNIQUE (external_id)` exists.
5. **ERP state.** Not involved.
6. **Integration state.** Not involved.
7. **Expected.** The reference is stored and unique. Identical payload returns the existing account. Different payload gives 409. Permission is enforced.
8. **Actual.** All four, as above.
9. **Explanation.** The endpoint goes through Fineract's own command pipeline, so permission checks, the transaction and the audit come for free. The handler looks the account up by `external_id` and compares every field before deciding. The unique constraint, added in a Liquibase changelog, catches the race the lookup can't: two requests that both find nothing and both insert. The loser gets a `ConcurrencyFailureException`, Fineract's command retry runs it again, and the second time it finds the account. The JUnit tests cover the same cases (`fineract-unit.log`: 7 unit tests; `fineract-it.log`: 2 integration tests against the running Fineract).

## A2. GL sync from the ERP through the integration service

1. **Initial state.** Each case uses a new ERP account (`GL-1…`, `GL-5…`, `GL-2…`, `GL-3…` with a timestamp suffix) that exists nowhere yet.
2. **Request.**
   - **Case 1:** the same account `POST`ed to the ERP 4 times, then `POST /api/accounts/sync` 4 times.
   - **Case 2:** 4 concurrent syncs of a new account.
   - **Case 3:** Fineract stopped, then sync.
   - **Case 4:** a GL account with that `external_id` but a different name and code inserted straight into Fineract's table (as if someone created it by hand), then sync.
3. **Response.**
   - **Case 1:** ERP 201, 200, 200, 200; sync 200 ×4.
   - **Case 2:** 200 ×4.
   - **Case 3:** 202, `SYNC_PENDING`.
   - **Case 4:** 422, `FAILED`.
4. **Fineract state.** One GL account per reference in cases 1–3. Case 4 keeps its original account; nothing is overwritten.
5. **ERP state.** One account per reference.
6. **Integration state.**
   - **Case 1:** one `gl_account_sync` row, `SYNCED`, `attempts=1`.
   - **Case 3:** `SYNC_PENDING`, `attempts` 1 then 2 with `next_attempt_at` moving out. Once Fineract was back: `SYNCED` after 5 attempts, without any new request.
   - **Case 4:** `FAILED` with Fineract's 409 message in `last_error`. Not retried.
7. **Expected.** Four sends give one ERP account, one Fineract GL account and one mapping. Retry while Fineract is down. A rejection is final.
8. **Actual.** `ERP accounts: 1 | Fineract GL accounts: 1 | mappings: 1` for cases 1–3. Case 4 `FAILED` with `attempts=1`.
9. **Explanation.** The sync row is unique on `external_reference` and locked with `SELECT … FOR UPDATE` while Fineract is called, so concurrent syncs queue up and the later ones see `SYNCED`. A 5xx or connection error is treated as temporary (backoff 2 s, 4 s, 8 s… up to 60 s, 10 attempts), while a 4xx is treated as final. The status list (step 5 of the log) shows no Fineract IDs.

## B. Fineract configuration

1. **Initial state.** Empty Fineract after `make reset && make up`.
2. **Request.** `make setup`, run twice.
3. **Response.** The first run creates everything. The second run prints `exists` for every step and changes nothing.
4. **Fineract state.**
   - RWF with 0 decimals, and the Kigali Main Office.
   - Role `integration-service` with 13 permissions, and API user `integration_service`.
   - 10 GL accounts.
   - Loan product SME Asset Finance: 5,000,000 RWF, 12 monthly repayments, 18% a year, declining balance, equal instalments, 360/30 days, single disbursement, **cash-based** accounting mapped to the synced GL codes.
5. **ERP state.** The 10 seeded accounts, unchanged.
6. **Integration state.** 10 `gl_account_sync` rows, all `SYNCED`.
7. **Expected.** GL accounts synced before the product references them. An API user that can do loan operations but nothing else. A setup that's safe to rerun.
8. **Actual.** As expected. `create office as integration_service -> 403`.
9. **Explanation.** The GL accounts come from the ERP through the integration service (Part A), and the script stops if any isn't `SYNCED`, because the product would fail to map. Why cash-based and why these permissions: see DECISIONS.md.

## C. Client identities

1. **Initial state.** No client `SME-161214`.
2. **Request.**
   - Create, then find by `client_uid`.
   - The same create 3 more times.
   - The same reference with another name.
   - A new reference with the same `client_uid`.
   - 10 concurrent creates of `SME-161215` with one reference.
   - `GET` an unknown `SME-999999`.
3. **Response.** 201 · 200 with `checks: {exists: true, active: true, name_matches: true}` · 200 ×3 · 409 `IDEMPOTENCY_CONFLICT` · 409 `CLIENT_UID_CONFLICT` · 1×201 and 9×202 · 404 `CLIENT_NOT_FOUND`.
4. **Fineract state.** One client with `externalId SME-161214`, and one with `SME-161215`.
5. **ERP state.** Not involved.
6. **Integration state.** One `clients` row each.
7. **Expected.** The same reference never creates two clients, and a reused `client_uid` is rejected.
8. **Actual.** `Fineract clients with externalId SME-161214: 1 | integration clients: 1`, and the same for `SME-161215` after the concurrent burst.
9. **Explanation.** The first request inserts an `IN_PROGRESS` idempotency record. The other nine hit the unique key and get 202 instead of starting a second create. `client_uid` is unique in our table, and it's also Fineract's `externalId`, so it's unique there as well. Before creating, the service checks Fineract for that `externalId`, which covers a create that went through but whose response was lost.

## D. Loan lifecycle to Closed (obligations met)

1. **Initial state.** A new client `SME-665030`.
2. **Request.**
   - Apply for 5,000,000, approve, disburse.
   - Repay 458,400, then 300,000, then 1,000,000.
   - Pay off exactly the `total_outstanding` Fineract reports.
3. **Response.** 201, 200, 201, then 201 for every repayment. `loan_reference` `LN-2026-000003`.
4. **Fineract state** after each stage (read from `m_loan` and through the API):

   | Stage | Status | Principal outstanding | Interest outstanding | Total outstanding |
   |---|---|---|---|---|
   | Application | 100 Submitted | 0 | 0 | 0 |
   | Approval | 200 Approved | 0 | 0 | 0 |
   | Disbursement | 300 Active | 5,000,000 | 500,799 | 5,500,799 |
   | Repayment 458,400 (383,400 P + 75,000 I) | 300 Active | 4,616,600 | 425,799 | 5,042,399 |
   | Repayment 300,000 (230,751 P + 69,249 I) | 300 Active | 4,385,849 | 356,550 | 4,742,399 |
   | Repayment 1,000,000 (879,101 P + 120,899 I) | 300 Active | 3,506,748 | 235,651 | 3,742,399 |
   | Payoff 3,742,399 (3,506,748 P + 235,651 I) | 600 Closed (obligations met) | 0 | 0 | 0 |

5. **ERP state.** Covered in H.
6. **Integration state.** 5 transactions, each with Fineract's principal/interest split, and no Fineract IDs in the responses.
7. **Expected.** The loan reaches `CLOSED_OBLIGATIONS_MET` and the principal portions add up to the amount disbursed.
8. **Actual.** Closed. Total repaid 5,500,799, and the principal portions add up to exactly 5,000,000.
9. **Explanation.** The final amount isn't calculated by us. The script asks Fineract for `total_outstanding` and pays exactly that. The interest is Fineract's own declining-balance schedule on 360/30 days, which is why total interest is 500,799 and not a round 18%.

## G. Mock ERP journal

1. **Initial state.** Accounts 1100, 1200 and 4000 at zero.
2. **Request.**
   - The same disbursement entry 4 times.
   - 10 concurrent posts of a new repayment entry.
   - The same reference with different amounts.
   - An unbalanced entry (DR 458,400, CR 453,400).
   - An entry on unknown account 9999.
3. **Response.** 201, 200, 200, 200 · one 201 and nine 200 · 409 · 422 `entry is not balanced` · 422 `unknown account codes: 9999`.
4. **Fineract state.** Not involved.
5. **ERP state.** One entry per reference, none for the rejected ones. Balances afterwards: 1200 = 4,616,600, 1100 = −4,541,600, 4000 = 75,000.
6. **Integration state.** Not involved.
7. **Expected.** The ERP enforces its own uniqueness and only accepts balanced entries on known accounts.
8. **Actual.** As expected. The balances match the two entries exactly.
9. **Explanation.** `journal_entries.external_reference` is unique, and each entry stores a hash of its payload. A repeat with the same hash returns the stored entry (200); a different hash gives 409. Balance, known accounts and currency are all checked before anything is written. Check constraints on the lines (no negatives, one side only) stop bad rows even if the code had a bug. *(This log was captured before the common error format went in, so its errors still show FastAPI's `detail` shape. The clean run regenerates it.)*

## H. Loan lifecycle with the ERP ledger

1. **Initial state.** The same flow as D, on a new client `SME-680937`.
2. **Request.** Apply, approve, disburse, three partial repayments and the payoff.
3. **Response.** Every money movement returns 201 with `status: COMPLETED`.
4. **Fineract state.** `LN-2026-000005`, Closed (obligations met). Principal repaid 5,000,000, interest repaid 500,799.
5. **ERP state.** One balanced entry per Fineract transaction, under the same `external_reference`:

   | Reference | Lines |
   |---|---|
   | DSB-180937 | 1200 DR 5,000,000 / 1100 CR 5,000,000 |
   | RPY-180937-1 | 1100 DR 458,400 / 1200 CR 383,400 / 4000 CR 75,000 |
   | RPY-180937-2 | 1100 DR 300,000 / 1200 CR 230,751 / 4000 CR 69,249 |
   | RPY-180937-3 | 1100 DR 1,000,000 / 1200 CR 879,101 / 4000 CR 120,899 |
   | RPY-180937-FINAL | 1100 DR 3,742,399 / 1200 CR 3,506,748 / 4000 CR 235,651 |

6. **Integration state.** 5 transactions, all `COMPLETED`.
7. **Expected.** The ERP ledger for the loan agrees with Fineract.
8. **Actual.** 1200 Loan Receivable: debits 5,000,000, credits 5,000,000, so zero once the loan is closed. 4000 Interest Income: 500,799, the same as Fineract's interest repaid. 1100 Bank: net +500,799.
9. **Explanation.** The split in each entry is Fineract's `principalPortion` and `interestPortion`, read back from the transaction after it's posted. We never calculate interest ourselves, so the ERP can't drift from Fineract's schedule.

## I1. Sequential duplicates

1. **Initial state.** An approved loan (`LN-2026-000015`).
2. **Request.** The same disbursement (same reference, same body) 4 times in a row.
3. **Response.** 201, 200, 200, 200, all with the same body.
4. **Fineract state.** 1 disbursement.
5. **ERP state.** 1 journal entry.
6. **Integration state.** 1 transaction, `COMPLETED`.
7. **Expected.** 4 / 1 / 1 / 1.
8. **Actual.**
   ```
   Requests received:         4
   Fineract disbursements:    1
   ERP journal entries:       1
   Integration transactions:  1
   ```
9. **Explanation.** After the first request, the idempotency record is `COMPLETED` with the stored response. The next three are replayed from it, and Fineract and the ERP are never called again.

## I2. Concurrent disbursements

1. **Initial state.** Two approved loans: `LN-2026-000016` for (a), `LN-2026-000017` for (b).
2. **Request.** (a) 10 simultaneous disbursements with the **same** reference. (b) 10 simultaneous disbursements with **10 different** references.
3. **Response.** (a) one 201 and nine 202 (`IN_PROGRESS`). (b) one 201 and nine 409 `LOAN_ALREADY_DISBURSED`.
4. **Fineract state.** 1 disbursement on each loan.
5. **ERP state.** 1 journal entry for each loan.
6. **Integration state.** 1 transaction for each loan.
7. **Expected.** Exactly one disbursement in every case.
8. **Actual.** As expected, in an automated parallel test (`tests_live/test_duplicates_and_concurrency.py`).
9. **Explanation.** Different layers stop the two cases:
   - **(a)** stops at the idempotency table. The unique key on `(operation, external_reference)` lets one request in and turns the others into 202s.
   - **(b)** gets past the idempotency table, because the references are all different. It stops at the partial unique index `one_active_disbursement_per_loan`: only one insert succeeds, and the other nine get 409.
   - **Behind both:** the per-loan advisory lock serialises the Fineract calls, and Fineract's `externalId` is unique.
   - **The last line of defence** is Fineract's loan state. The extra test sends 10 disbursements **straight to Fineract**, bypassing our service entirely: 1 is accepted (200) and 9 are refused (403 `validation.msg.domain.rule.violation`), because an active loan can't be disbursed again.

   One layer alone isn't enough. Idempotency doesn't catch different references. The DB index only protects our own database. Fineract's state doesn't stop the ERP being posted twice. So each layer covers a gap in another.

## I3. Concurrent repayments

1. **Initial state.** A disbursed loan (`LN-2026-000018`) with 5,500,799 outstanding.
2. **Request.** 10 repayments with different references (100,000 to 325,000), each sent **twice** at the same moment: 20 requests in parallel.
3. **Response.** Ten 201 and ten 202 (the duplicate of each pair).
4. **Fineract state.** 10 repayments. Final outstanding 3,375,799.
5. **ERP state.** 10 journal entries.
6. **Integration state.** 10 transactions, all `COMPLETED`.
7. **Expected.** Each reference applies once. The final outstanding equals the initial outstanding minus what Fineract recorded as repaid.
8. **Actual.**
   ```
   Initial outstanding:            5500799
   Repaid per Fineract breakdown:  2125000   (principal 1808379 / interest 316621)
   Final outstanding (Fineract):   3375799
   Initial - repaid:               3375799
   ```
9. **Explanation.** The test adds up the portions from Fineract's transactions, not the amounts it sent, so it would catch Fineract applying something differently from what we asked. The duplicates are absorbed by the idempotency table. The repayments themselves queue on the per-loan advisory lock, because Fineract can't take two writes on one loan at the same time (see I-bug).

## I-bug. Fineract's loan lock, before the fix

This one isn't a demo you'd run, but it's worth keeping. It's how I found out the advisory lock was needed.

1. **Initial state.** Commit `2eccab1`, which had no per-loan lock. Loan `LN-2026-000009`, disbursed.
2. **Request.** The I3 test: 10 repayments × 2, all at once.
3. **Response.** The 8 requests that hit Fineract's lock got 422 `FINERACT_REJECTED`.
4. **Fineract state.** Only **1** repayment applied, plus an `ACCRUAL` row that Fineract adds on its own.
5. **ERP state.** Entries only for the transactions that completed.
6. **Integration state.** 1 `COMPLETED`, 1 stuck in `FINERACT_PENDING`, 8 `FAILED` with `Loan is locked by the COB job. Loan ID: 10`.
7. **Expected.** All 10 apply.
8. **Actual.** Only 1 applied. Fineract 1.15 locks a loan while it writes to it and answers any other write with 409 `error.msg.loan.locked`.
9. **Explanation.** Two fixes went in (`25d1b87`). First, a Postgres advisory lock per loan around the Fineract call. Second, that 409 is now treated as "busy, retry" instead of "rejected". After the fix, I3 passes with all 10 applied.

## J. Failure scenarios

All five run against one loan (`LN-2026-000046`) with a 100,000 repayment each. Failures are injected either by stopping a container or with `POST /api/admin/faults`. Each scenario prints the integration row, the count of Fineract transactions with that `externalId`, and the count of ERP entries, before, during and after.

### J1. ERP down after Fineract succeeded

1. **Initial state.** No row, 0 in Fineract, 0 in the ERP for `RPY-F220450-1`.
2. **Request.** Mock ERP container stopped, then the repayment.
3. **Response.** 202 `ERP_PENDING`, `last_error: "ERP unavailable: ConnectError…"`.
4. **Fineract state.** 1 repayment the whole time.
5. **ERP state.** 0 while it's down, then 1.
6. **Integration state.** `ERP_PENDING` with `erp_attempts` 1, then 2, and `next_attempt_at` moving out. After the ERP is started again: `COMPLETED` with `erp_attempts=4`, without any new request. When the caller resends, it gets 200 `COMPLETED` and nothing changes.
7. **Expected.** The caller is told it's pending. The ERP catches up on its own, with no double posting.
8. **Actual.** As expected: 1 in Fineract, 1 in the ERP.
9. **Explanation.** Fineract is never called again; the row is past `FINERACT_DONE`. The background loop retries only the ERP post, with backoff. It can't double post, because the ERP is idempotent on `external_reference`, and a retry of the same entry would just get 200.

### J2. Fineract down

1. **Initial state.** Nothing for `RPY-F220450-2`.
2. **Request.** Fineract container stopped, then the repayment. Resent while still down. Fineract started. Resent again.
3. **Response.** 503 `FINERACT_UNAVAILABLE` · 503 · 201 `COMPLETED`.
4. **Fineract state.** 0 until the last request, then 1.
5. **ERP state.** 0, then 1.
6. **Integration state.** The row is persisted as `FINERACT_PENDING` from the first request. After Fineract came back, the sweep looked the reference up, found nothing, and **didn't send it**. The caller's retry completed it.
7. **Expected.** The caller knows it failed. The state is persisted. A retry with the same reference is safe.
8. **Actual.** As expected.
9. **Explanation.** The sweep never sends a transaction to Fineract by itself. It can't know whether the caller has given up, or retried with a new reference, and sending it anyway could charge the customer twice. It only looks up. If the caller never comes back, the row becomes `FAILED` ("never applied") after 15 minutes.

### J3. Fineract timeout after sending

1. **Initial state.** Nothing for `RPY-F220450-3`.
2. **Request.** Fault `fineract.repayment timeout_after` armed, then the repayment. The request reaches Fineract and is applied, but our client reports a timeout.
3. **Response.** 503 `FINERACT_UNAVAILABLE`, because from the caller's side we genuinely don't know yet.
4. **Fineract state.** **1**: the repayment did go through.
5. **ERP state.** 0, then 1.
6. **Integration state.** `FINERACT_PENDING`, then `COMPLETED` with `erp_attempts=1`, without any new request. A later resend got 200.
7. **Expected.** Find out whether it succeeded, without blindly retrying.
8. **Actual.** Fineract has 1 and the ERP has 1. Nothing was sent twice.
9. **Explanation.** This is the case the `externalId` is for. The sweep asks Fineract for `RPY-F220450-3`, finds it, reads its split and carries on to the ERP. Every path that might send to Fineract looks the reference up first.

### J4. ERP timeout after the entry was written

1. **Initial state.** Nothing for `RPY-F220450-4`.
2. **Request.** Fault `erp.journal_entry timeout_after` armed, then the repayment. The ERP writes the entry, but our client reports a timeout.
3. **Response.** 202 `ERP_PENDING`.
4. **Fineract state.** 1.
5. **ERP state.** **1** right away, and still 1 after the retry.
6. **Integration state.** `ERP_PENDING` with `erp_attempts=1`, then `COMPLETED` with `erp_attempts=2`.
7. **Expected.** The retry doesn't create a second entry.
8. **Actual.** `ERP entries for RPY-F220450-4: 1 (the retry got 200 for the identical entry)`.
9. **Explanation.** The retry sends the identical payload. The ERP matches it by reference and payload hash and returns the stored entry with 200, which we treat as success.

### J5. Crash between Fineract success and the ERP call

1. **Initial state.** Nothing for `RPY-F220450-5`.
2. **Request.** Fault `fineract.repayment crash_after` armed, then the repayment. The process dies (`os._exit`) right after Fineract applied it.
3. **Response.** None: the connection dropped (`HTTP 000`). The container exited.
4. **Fineract state.** 1.
5. **ERP state.** 0 while the service is dead, then 1.
6. **Integration state.**
   - **While dead:** the row is `FINERACT_PENDING` and the idempotency record is `IN_PROGRESS`.
   - **On restart:** the startup sweep logged `RPY-F220450-5: FINERACT_PENDING → COMPLETED`.
   - **Caller resends within 60 s:** 202 `IN_PROGRESS`, because the crashed request still holds the lease.
   - **Caller resends after the lease:** 200 `COMPLETED`.
7. **Expected.** The interrupted transaction is found after restart and completed.
8. **Actual.** As expected. The loan ends with 5 repayments in Fineract (500,000) and 5 entries in the ERP (500,000), and all 6 transactions are `COMPLETED`.
9. **Explanation.** Every state is committed before the next network call, so the row shows where the crash happened. The sweep runs at startup, looks the reference up in Fineract (found), and carries on to the ERP. The 60-second lease is there so that a crashed request's idempotency record doesn't block that reference forever.

## K. Reconciliation

1. **Initial state.** Loan `LN-2026-000048`, disbursed and repaid three times through the service, all `COMPLETED`. The other outcomes are then seeded on purpose:
   - **MISSING_IN_ERP:** `MISS-ERP-224001`, a repayment posted straight into Fineract.
   - **MISSING_IN_FINERACT:** `MISS-FIN-224001`, a manual journal entry posted straight into the ERP.
   - **AMOUNT_MISMATCH:** `MISMATCH-224001`, 100,000 in Fineract and 90,000 in the ERP.
   - **Reversal:** `RPY-K224001-2` reversed in Fineract after it reached the ERP.
2. **Request.** `POST /api/reconciliation` for the loan. Then repairs for `MISS-ERP-224001` and `RPY-K224001-2`, each twice, and for the two that can't be repaired. Then the report again.
3. **Response.**

   | Reference | Before | After repairs |
   |---|---|---|
   | DSB-K224001 | MATCHED | MATCHED |
   | MISS-ERP-224001 | MISSING_IN_ERP (100,000) | MATCHED, `posted_to_erp`, then `none` on the second call |
   | MISS-FIN-224001 | MISSING_IN_FINERACT (75,000) | unchanged, `REPAIR_NOT_SUPPORTED` |
   | MISMATCH-224001 | AMOUNT_MISMATCH (diff 10,000) | unchanged, `REPAIR_NOT_SUPPORTED` |
   | RPY-K224001-2 | AMOUNT_MISMATCH (Fineract 0 after reversal, ERP 458,400) | MATCHED, `posted_reversal_to_erp`, then `none` |
   | RPY-K224001-1, -3 | SPLIT_MISMATCH | unchanged |

4. **Fineract state.** Unchanged by reconciliation. Repairs never write to Fineract.
5. **ERP state.** Two new entries from the repairs: `JE-000141` for `MISS-ERP-224001`, built from Fineract's split, and `JE-000142` `REV-RPY-K224001-2`, the mirror image of the original entry.
6. **Integration state.** Unchanged by the report.
7. **Expected.** All four statuses appear. The report is read-only. Repairs are explicit and idempotent.
8. **Actual.** As expected. The summary went from `MATCHED=1 MISSING_IN_ERP=1 MISSING_IN_FINERACT=1 AMOUNT_MISMATCH=2 SPLIT_MISMATCH=2` to `MATCHED=3 MISSING_IN_FINERACT=1 AMOUNT_MISMATCH=1 SPLIT_MISMATCH=2`.
9. **Explanation.**
   - **Keying.** One Fineract transaction is matched to the one ERP entry with the same `external_reference`.
   - **Reversals.** Amounts are compared after reversals, so a properly reversed pair nets to 0 and matches.
   - **Repairs.** Only two cases are repaired, because only there is Fineract clearly right and the fix clearly safe. An amount mismatch or an entry with no Fineract transaction needs a person to decide which side is wrong.
   - **The SPLIT_MISMATCH rows.** These were a surprise. When `-2` was reversed, Fineract re-allocated the other repayments made on the same date, so `-1` and `-3` now have a different principal/interest split from the one posted to the ERP. The totals still match. I report it as its own status instead of hiding it, and left it as a manual fix (see DECISIONS.md).

## O. What happened to FIN-2026-…?

1. **Initial state.** Loan `LN-2026-000050`, active.
2. **Request.** A teller posts repayment `FIN-2026-224349` with `X-Correlation-ID: teller-224349`. A fault makes the first ERP response get lost after the entry is written.
3. **Response.** 202 `ERP_PENDING`, with `x-correlation-id: teller-224349` echoed back.
4. **Fineract state.** Transaction 217.
5. **ERP state.** `JE-000146`, written on the first attempt and replayed (not duplicated) on the second.
6. **Integration state.** `COMPLETED`, `erp_attempts=2`, `erp_transaction_id=JE-000146`.
7. **Expected.** One API call and the logs are enough to explain the whole story.
8. **Actual.** `GET /api/transactions/FIN-2026-224349` gives the final state. Filtering both services' logs by `external_reference` gives seven lines, in order:
   1. received;
   2. `FINERACT_DONE` (Fineract id 217);
   3. `ERP_PENDING`;
   4. ERP `journal_entry_created` `JE-000146`;
   5. `erp_post_failed` (read timeout);
   6. ERP `journal_entry_replayed` under a `recovery-…` correlation ID;
   7. `COMPLETED`.
9. **Explanation.** The `external_reference` ties together everything that happened to the payment, including retries done later by the background loop. The `correlation_id` ties together everything one HTTP request did across both services. The full walkthrough is in DECISIONS.md.
