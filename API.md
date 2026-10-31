# API

This is the API of the integration service, at `http://localhost:8080`. The same information, generated from the code, is in Swagger at `/docs` and in `docs/openapi.json`. The mock ERP's API is summarised at the end.

All the examples below were captured from the running stack, with only the line breaks changed.

## Basics

### Authentication

Send an API key in the `X-API-Key` header.

| Key | Can call |
|---|---|
| teller key (`dev-teller-key` locally) | everything under `/api/` except `/api/admin/*` |
| admin key (`dev-admin-key` locally) | everything, including `/api/admin/*` |

A missing or wrong key gives 401, and a teller key on an admin route gives 403. `/health`, `/docs` and `/openapi.json` are open. If no keys are configured at all, every request is refused.

### Correlation ID

Send `X-Correlation-ID` if you have one: 1 to 100 characters, letters, digits and `. _ : -`. If it's missing or not valid, the service makes up a UUID. Either way it comes back in the response header and in every error body. It's also written on every log line and passed on to Fineract and the ERP, so you can grep one ID across all the logs.

### Money and dates

Amounts are whole numbers in RWF (RWF has no cents). Dates are `YYYY-MM-DD`. Any date field you leave out defaults to today.

### Errors

Every error has the same shape:

```json
{"error": {"code": "LOAN_NOT_FOUND", "message": "no loan LN-2026-999999", "correlation_id": "doc-100613"}}
```

Validation errors also have `details`, one item per bad field:

```json
{"error": {"code": "VALIDATION_ERROR", "message": "the request is not valid", "correlation_id": "doc-100613",
  "details": [
    {"field": "body.external_reference", "message": "String should have at least 1 character", "type": "string_too_short"},
    {"field": "body.client_uid", "message": "String should match pattern '^SME-[0-9]{6}$'", "type": "string_pattern_mismatch"},
    {"field": "body.principal", "message": "Input should be greater than 0", "type": "greater_than"}]}}
```

| HTTP | Code | Meaning |
|---|---|---|
| 401 | `UNAUTHORIZED` | no API key, or a wrong one |
| 403 | `FORBIDDEN` | the route needs an admin key |
| 404 | `CLIENT_NOT_FOUND`, `LOAN_NOT_FOUND`, `TRANSACTION_NOT_FOUND`, `NOT_FOUND` | nothing with that reference |
| 404 | `NOTHING_TO_REPAIR` | the reference is in neither Fineract nor the ERP for that loan |
| 409 | `IDEMPOTENCY_CONFLICT` | this `external_reference` was already used with a different payload |
| 409 | `CLIENT_UID_CONFLICT` | the `client_uid` already belongs to another client |
| 409 | `LOAN_ALREADY_APPROVED`, `LOAN_ALREADY_DISBURSED`, `LOAN_STATE_CONFLICT` | the loan isn't in a state where this makes sense |
| 409 | `TRANSACTION_REFERENCE_CONFLICT` | the reference is already used by a transaction on another loan or of another type |
| 409 | `REPAIR_NOT_SUPPORTED` | reconciliation can't fix this one automatically |
| 422 | `VALIDATION_ERROR` | the request body or a parameter is invalid |
| 422 | `UNKNOWN_CLIENT` | loan application for a `client_uid` we don't know |
| 422 | `FINERACT_REJECTED` | Fineract refused the operation; its message is passed on |
| 422 | `REPAIR_FAILED` | a repair was attempted but the entry couldn't be built or the ERP refused it |
| 503 | `FINERACT_UNAVAILABLE`, `ERP_UNAVAILABLE` | a downstream system is down; safe to retry with the same reference |
| 503 | `LOAN_BUSY` | another write on the same loan held the lock for more than 30 s; retry |
| 500 | `INTERNAL_ERROR` | a bug. No internals in the body; the traceback is in the log under the same correlation ID |

## Idempotency contract

Every request that changes something carries an `external_reference`, chosen by the caller and unique per business operation. GL sync is the exception: it's keyed by the GL account's own `external_reference`.

| Case | What you get |
|---|---|
| New reference | Processed once: 201 (or 200 for approve and GL sync) |
| Same reference, same payload, already finished | The original response again, with 200 instead of 201. Nothing is sent to Fineract or the ERP. |
| Same reference, same payload, still running | 202 with `"status": "IN_PROGRESS"`. A second execution is never started. |
| Same reference, different payload | 409 `IDEMPOTENCY_CONFLICT` |

"Same payload" means the same request body (plus the loan reference from the URL), compared with a SHA-256 hash of the canonical JSON.

References are scoped by operation, so `ABC-1` as a client reference and `ABC-1` as a repayment reference don't clash. On top of that:

| Operation | Extra rule |
|---|---|
| Client | `client_uid` is unique in our database and in Fineract. A new reference with a `client_uid` that's already taken gets 409 `CLIENT_UID_CONFLICT`. |
| Loan application | one loan per reference |
| Approval | a loan is approved once. A second approval with a different reference gets 409 `LOAN_ALREADY_APPROVED`. |
| Disbursement | one per loan. A second disbursement with a different reference gets 409 `LOAN_ALREADY_DISBURSED`, even if both arrive at the same moment. |
| Repayment | one per reference, and as many repayments per loan as you like |

**Why a 202 isn't a final answer.** For disbursements and repayments, a 202 means Fineract has the money but the ERP doesn't yet. The service keeps retrying the ERP on its own. Send the same request again or call `GET /api/transactions/{external_reference}` to see when it reaches `COMPLETED`. In that case the idempotency record isn't saved, so a repeated request re-checks the current state instead of replaying the old 202.

How double processing is actually prevented (idempotency table, unique indexes, per-loan lock, Fineract `externalId`, Fineract loan state) is explained in [ARCHITECTURE.md](ARCHITECTURE.md#concurrency).

## Endpoints

| Method | Path | Key | Purpose |
|---|---|---|---|
| POST | `/api/accounts/sync` | teller | sync one ERP account to Fineract |
| POST | `/api/accounts/sync/all` | teller | sync every ERP account |
| GET | `/api/accounts/sync` | teller | sync status of every account |
| GET | `/api/accounts/sync/{external_reference}` | teller | sync status of one account |
| POST | `/api/clients` | teller | create a client |
| GET | `/api/clients/{client_uid}` | teller | find and validate a client |
| POST | `/api/loans` | teller | loan application |
| GET | `/api/loans/{loan_reference}` | teller | loan status, balances and transactions |
| POST | `/api/loans/{loan_reference}/approve` | teller | approve |
| POST | `/api/loans/{loan_reference}/disburse` | teller | disburse the full principal |
| POST | `/api/loans/{loan_reference}/repayments` | teller | record a repayment |
| GET | `/api/transactions/{external_reference}` | teller | where a disbursement or repayment is up to |
| POST | `/api/reconciliation` | teller | compare Fineract and the ERP (read-only) |
| POST | `/api/reconciliation/repairs` | teller | fix one reconciliation item |
| POST | `/api/admin/recovery` | admin | run the recovery sweep now |
| GET | `/api/admin/transactions?status=` | admin | list transactions in a status |
| POST, GET, DELETE | `/api/admin/faults` | admin | fault injection for testing |
| GET | `/health` | none | liveness |

### GL account sync

The ERP owns the chart of accounts. These endpoints copy an ERP account into Fineract through the patched `/glaccounts/sync` endpoint.

```http
POST /api/accounts/sync
{"external_reference": "GL-4000"}
```

```http
HTTP/1.1 200 OK
{"external_reference": "GL-4000", "erp_code": "4000", "erp_name": "Interest Income", "erp_type": "INCOME",
 "status": "SYNCED", "attempts": 1, "last_error": null, "next_attempt_at": null,
 "updated_at": "2026-10-30T14:07:54.999153Z"}
```

| Status | HTTP | Meaning |
|---|---|---|
| `SYNCED` | 200 | the GL account exists in Fineract |
| `SYNC_PENDING` | 202 | Fineract is down; retried in the background at `next_attempt_at` |
| `FAILED` | 422 | Fineract rejected it (for example the same `external_id` with a different payload). Not retried. Calling sync again starts over. |

The account has to exist in the ERP first, otherwise you get a 404. If the ERP itself is down you get a 503. Calling sync four times for the same account still leaves one ERP account, one Fineract GL account and one sync row.

`POST /api/accounts/sync/all` takes no body and returns a list of the same objects, one per ERP account.

### Clients

```http
POST /api/clients
{"external_reference": "CLI-100613", "client_uid": "SME-100613", "name": "Umucyo Hardware Ltd"}
```

```http
HTTP/1.1 201 Created
{"client_uid": "SME-100613", "external_reference": "CLI-100613", "name": "Umucyo Hardware Ltd", "status": "ACTIVE"}
```

`client_uid` must look like `SME-` plus six digits. The client is created active, in the Kigali Main Office, with `client_uid` as its Fineract `externalId`.

The same request again gives 200 with the same body. The same reference with another name gives:

```http
HTTP/1.1 409 Conflict
{"error": {"code": "IDEMPOTENCY_CONFLICT", "message": "external_reference CLI-100613 was already used for CREATE_CLIENT with a different payload", "correlation_id": "doc-100613"}}
```

A new reference with a `client_uid` that's already taken:

```http
HTTP/1.1 409 Conflict
{"error": {"code": "CLIENT_UID_CONFLICT", "message": "client_uid SME-100613 already belongs to a client", "correlation_id": "doc-100613"}}
```

```http
GET /api/clients/SME-100613
```

```http
HTTP/1.1 200 OK
{"client_uid": "SME-100613", "external_reference": "CLI-100613", "name": "Umucyo Hardware Ltd", "status": "ACTIVE",
 "office": "Kigali Main Office", "activation_date": "2026-10-31",
 "checks": {"exists": true, "active": true, "name_matches": true}}
```

The details are read live from Fineract. `checks` answers the brief's "exists, active, correct name" in one place. If someone deleted the client in Fineract, you still get a 200, with `exists: false`, so the caller can see the mismatch.

### Loans

```http
POST /api/loans
{"external_reference": "APP-100613-B", "client_uid": "SME-100613", "principal": 1000000, "submitted_on": "2026-10-31"}
```

```http
HTTP/1.1 201 Created
{"loan_reference": "LN-2026-000064", "external_reference": "APP-100613-B", "client_uid": "SME-100613", "principal": 1000000,
 "status": "SUBMITTED_AND_PENDING_APPROVAL", "submitted_on": "2026-10-31", "approved_on": null, "disbursed_on": null, "closed_on": null,
 "balances": {"principal_disbursed": 0, "principal_outstanding": 0, "interest_outstanding": 0, "total_outstanding": 0,
              "total_repaid": 0, "total_expected_repayment": 0},
 "transactions": []}
```

The service issues the `loan_reference` (`LN-<year>-<6 digits>`) and uses it as the loan's `externalId` in Fineract. Use it for everything after this. The terms (18% a year, 12 monthly repayments) come from the SME Asset Finance product.

```http
POST /api/loans/LN-2026-000063/approve
{"external_reference": "APR-100613", "approved_on": "2026-10-31"}
```

This returns 200 with the loan body, `"status": "APPROVED"`.

```http
GET /api/loans/LN-2026-000063
```

```http
HTTP/1.1 200 OK
{"loan_reference": "LN-2026-000063", "external_reference": "APP-100613", "client_uid": "SME-100613", "principal": 5000000,
 "status": "ACTIVE", "submitted_on": "2026-10-31", "approved_on": "2026-10-31", "disbursed_on": "2026-10-31", "closed_on": null,
 "balances": {"principal_disbursed": 5000000, "principal_outstanding": 4616600, "interest_outstanding": 384199,
              "total_outstanding": 5000799, "total_repaid": 500000, "total_expected_repayment": 5500799},
 "transactions": [
   {"external_reference": "DSB-100613", "type": "DISBURSEMENT", "status": "COMPLETED", "amount": 5000000, ...},
   {"external_reference": "FIN-2026-100613", "type": "REPAYMENT", "status": "COMPLETED", "amount": 500000, ...}]}
```

Status and balances come from Fineract on every call. `status` is Fineract's own loan status, for example `ACTIVE`, or `CLOSED_OBLIGATIONS_MET` once the loan is paid off.

### Disbursement and repayments

```http
POST /api/loans/LN-2026-000063/disburse
{"external_reference": "DSB-100613", "disbursed_on": "2026-10-31"}
```

```http
HTTP/1.1 201 Created
{"external_reference": "DSB-100613", "loan_reference": "LN-2026-000063", "type": "DISBURSEMENT", "status": "COMPLETED",
 "amount": 5000000, "transaction_date": "2026-10-31",
 "principal_portion": 0, "interest_portion": 0, "fee_portion": 0, "penalty_portion": 0, "overpayment_portion": 0,
 "erp_transaction_id": "JE-000185", "erp_attempts": 1, "next_attempt_at": null, "last_error": null}
```

The full approved principal is disbursed; partial disbursement isn't supported because the product is single-tranche. A second disbursement with another reference:

```http
HTTP/1.1 409 Conflict
{"error": {"code": "LOAN_ALREADY_DISBURSED", "message": "LN-2026-000063 already has a disbursement", "correlation_id": "doc-100613"}}
```

```http
POST /api/loans/LN-2026-000063/repayments
{"external_reference": "FIN-2026-100613", "amount": 500000, "transaction_date": "2026-10-31"}
```

```http
HTTP/1.1 201 Created
{"external_reference": "FIN-2026-100613", "loan_reference": "LN-2026-000063", "type": "REPAYMENT", "status": "COMPLETED",
 "amount": 500000, "transaction_date": "2026-10-31",
 "principal_portion": 383400, "interest_portion": 116600, "fee_portion": 0, "penalty_portion": 0, "overpayment_portion": 0,
 "erp_transaction_id": "JE-000186", "erp_attempts": 1, "next_attempt_at": null, "last_error": null}
```

The portions are Fineract's split of the repayment, read back after it's posted. The ERP journal entry is built from them: DR 1100 Bank 500,000 / CR 1200 Loan Receivable 383,400 / CR 4000 Interest Income 116,600.

When the ERP is down, the money is still in Fineract and you get a 202:

```http
HTTP/1.1 202 Accepted
{"external_reference": "FIN-2026-100614", "loan_reference": "LN-2026-000063", "type": "REPAYMENT", "status": "ERP_PENDING",
 "amount": 450000, "transaction_date": "2026-10-31",
 "principal_portion": 389151, "interest_portion": 60849, "fee_portion": 0, "penalty_portion": 0, "overpayment_portion": 0,
 "erp_transaction_id": null, "erp_attempts": 1, "next_attempt_at": "2026-10-31T11:34:53.620547+00:00",
 "last_error": "ERP unavailable: ConnectError: injected erp.journal_entry fail_before"}
```

A few seconds later the background loop had posted it: `"status": "COMPLETED", "erp_attempts": 2, "erp_transaction_id": "JE-000187"`.

| Response | `status` | What it means for the caller |
|---|---|---|
| 201 | `COMPLETED` | done in both Fineract and the ERP |
| 202 | `FINERACT_PENDING`, `FINERACT_DONE`, `ERP_PENDING` | the money is (or may be) in Fineract and the ERP will catch up on its own. Don't send a new reference; poll or resend the same one. |
| 202 | `NEEDS_RECONCILIATION` | the money is in Fineract but the ERP posting stopped; someone needs to look at it |
| 422 | (`FINERACT_REJECTED`) | Fineract refused it. Nothing was posted anywhere. |
| 503 | (`FINERACT_UNAVAILABLE`) | Fineract couldn't be reached. Safe to resend the same request: the service looks the reference up in Fineract before sending again. |

### Transactions

```http
GET /api/transactions/FIN-2026-100613
```

This returns the same body as the repayment response above. It's the "one API call" for answering "what happened to this payment?". Fineract IDs are never returned; `erp_transaction_id` is the ERP's journal entry number.

### Reconciliation

```http
POST /api/reconciliation
{"loan_references": ["LN-2026-000063"]}
```

You can scope it by `from_date` / `to_date`, `loan_references` or `external_references`, or combine them.

```http
HTTP/1.1 200 OK
{"generated_at": "2026-10-31T11:34:40.390830+00:00",
 "scope": {"from_date": null, "to_date": null, "loan_references": ["LN-2026-000063"], "external_references": null},
 "summary": {"MATCHED": 2, "MISSING_IN_ERP": 0, "MISSING_IN_FINERACT": 0, "AMOUNT_MISMATCH": 0, "TYPE_MISMATCH": 0, "SPLIT_MISMATCH": 0},
 "items": [
   {"external_reference": "FIN-2026-100613", "loan_reference": "LN-2026-000063", "status": "MATCHED", "difference": 0, "note": null,
    "fineract": {"type": "REPAYMENT", "amount": 500000, "date": "2026-10-31", "reversed": false, "net_amount": 500000},
    "erp": {"type": "REPAYMENT", "amount": 500000, "date": "2026-10-31", "entry_id": "JE-000186",
            "reversal_entry_id": null, "reversal_amount": 0, "credits": {"1200": 383400, "4000": 116600}, "net_amount": 500000}},
   ...]}
```

| Status | Meaning |
|---|---|
| `MATCHED` | in both, same type, same net amount, same split |
| `MISSING_IN_ERP` | in Fineract, no journal entry |
| `MISSING_IN_FINERACT` | journal entry with no Fineract transaction |
| `AMOUNT_MISMATCH` | in both, net amounts differ (`difference` = Fineract − ERP) |
| `TYPE_MISMATCH` | in both, but one says disbursement and the other repayment |
| `SPLIT_MISMATCH` | same total, but the principal/interest split differs |

"Net" means reversals are counted. A reversed Fineract transaction counts as 0, and an ERP entry plus its `REV-` entry also nets to 0, so a reversal done properly on both sides comes out `MATCHED`.

```http
POST /api/reconciliation/repairs
{"external_reference": "FIN-2026-100613", "loan_reference": "LN-2026-000063"}
```

```http
HTTP/1.1 200 OK
{"action": "none", "item": {...the item, still MATCHED...}}
```

| Item status | Repair action |
|---|---|
| `MISSING_IN_ERP` | posts the journal entry, built from Fineract's split |
| Fineract reversed it, ERP didn't | posts the `REV-<reference>` mirror entry |
| already `MATCHED` | `"action": "none"`, so a repair is safe to run twice |
| anything else | 409 `REPAIR_NOT_SUPPORTED`. These need a person, because the system can't know which side is right. |

### Admin

These all need the admin key.

```http
POST /api/admin/recovery
```

```http
HTTP/1.1 200 OK
{"checked": 0, "transactions": []}
```

This runs the same sweep as the background loop and lists each transaction it touched with `from`, `to`, `erp_attempts` and a `note`.

`GET /api/admin/transactions?status=NEEDS_RECONCILIATION` lists transactions in any status. It's the queue of things a person has to look at.

Fault injection only works when `FAULT_INJECTION_ENABLED=true`. Otherwise these routes return 404 `FAULT_INJECTION_DISABLED`.

```http
POST /api/admin/faults
{"operation": "erp.journal_entry", "mode": "fail_before", "times": 1}
```

| Field | Values |
|---|---|
| `operation` | `fineract.disburse`, `fineract.repayment`, `erp.journal_entry` |
| `mode` | `fail_before`: fail without sending. `timeout_after`: send, then pretend it timed out. `crash_after`: send, then kill the process. |
| `times` | how many calls it applies to (1 to 100) |

`GET /api/admin/faults` lists the faults still armed, and `DELETE /api/admin/faults` clears them (204).

## Mock ERP

Base URL `http://localhost:8081`, with Swagger at `/docs`. It has no API key because it's only reachable from the internal network (see DECISIONS.md). It uses the same error shape.

| Method | Path | Notes |
|---|---|---|
| POST | `/api/accounts` | create an account. Idempotent on `external_reference`; `code` is unique. |
| GET | `/api/accounts` | chart of accounts (10 seeded accounts) |
| GET | `/api/accounts/{code}` | one account |
| GET | `/api/accounts/{code}/balance` | totals and balance on the account's normal side |
| POST | `/api/journal-entries` | idempotent on `external_reference`: 201 new, 200 same payload, 409 different payload, 422 unbalanced or unknown account |
| GET | `/api/journal-entries` | filter by `from_date`, `to_date`, `loan_reference`, `external_reference` (repeatable), `limit` |
| GET | `/api/journal-entries/{external_reference}` | one entry |

```http
GET /api/journal-entries/FIN-2026-100613
```

```json
{"entry_id": "JE-000186", "external_reference": "FIN-2026-100613", "entry_date": "2026-10-31", "currency": "RWF",
 "description": "Repayment FIN-2026-100613 on LN-2026-000063", "loan_reference": "LN-2026-000063",
 "transaction_type": "REPAYMENT", "total_amount": "500000.00",
 "lines": [{"account_code": "1100", "debit": "500000.00", "credit": "0.00"},
           {"account_code": "1200", "debit": "0.00", "credit": "383400.00"},
           {"account_code": "4000", "debit": "0.00", "credit": "116600.00"}],
 "created_at": "2026-10-31T11:34:40.179834Z"}
```

An unbalanced entry is rejected before anything is written:

```json
{"error": {"code": "VALIDATION_ERROR", "message": "the request is not valid", "correlation_id": "ae2e331e-…",
  "details": [{"field": "body", "message": "Value error, entry is not balanced: debits 100.00 != credits 90.00"}]}}
```
