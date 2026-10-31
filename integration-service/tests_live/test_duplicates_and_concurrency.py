from collections import Counter
from datetime import date

from tests_live.conftest import (
    Api,
    Reply,
    erp_entries,
    fineract_http,
    fineract_transactions,
    fire,
    integration_transactions,
    report,
    unique_suffix,
)

DISBURSEMENT = 1
REPAYMENT = 2


def statuses(replies: list[Reply]) -> dict[int, int]:
    return dict(sorted(Counter(r.status_code for r in replies).items()))


def disbursement_counts(loan: str) -> tuple[int, int, int]:
    return (
        fineract_transactions(loan, DISBURSEMENT),
        erp_entries(loan, "DISBURSEMENT"),
        integration_transactions(loan, "DISBURSEMENT"),
    )


def test_i1_same_disbursement_four_times_sequentially(api: Api, approved_loan: str) -> None:
    body = {"external_reference": f"DSB-{unique_suffix()}"}

    replies = [api.post(f"/api/loans/{approved_loan}/disburse", body) for _ in range(4)]

    fineract, erp, integration = disbursement_counts(approved_loan)
    report(
        f"I1 sequential duplicates on {approved_loan}",
        {
            "Requests received": len(replies),
            "HTTP statuses": [r.status_code for r in replies],
            "Fineract disbursements": fineract,
            "ERP journal entries": erp,
            "Integration transactions": integration,
        },
    )
    assert [r.status_code for r in replies] == [201, 200, 200, 200]
    assert {r.body["external_reference"] for r in replies} == {body["external_reference"]}
    assert (fineract, erp, integration) == (1, 1, 1)


def test_i2a_ten_concurrent_disbursements_same_reference(api: Api, approved_loan: str) -> None:
    body = {"external_reference": f"DSB-{unique_suffix()}"}

    replies = fire(10, lambda _: api.post(f"/api/loans/{approved_loan}/disburse", body))

    fineract, erp, integration = disbursement_counts(approved_loan)
    report(
        f"I2a 10 concurrent disbursements, same reference, on {approved_loan}",
        {
            "Requests received": len(replies),
            "HTTP statuses": statuses(replies),
            "Fineract disbursements": fineract,
            "ERP journal entries": erp,
            "Integration transactions": integration,
        },
    )
    assert statuses(replies).get(201) == 1
    assert set(statuses(replies)) <= {200, 201, 202}
    assert (fineract, erp, integration) == (1, 1, 1)


def test_i2b_ten_concurrent_disbursements_different_references(
    api: Api, approved_loan: str
) -> None:
    suffix = unique_suffix()

    replies = fire(
        10,
        lambda i: api.post(
            f"/api/loans/{approved_loan}/disburse", {"external_reference": f"DSB-{suffix}-{i}"}
        ),
    )

    fineract, erp, integration = disbursement_counts(approved_loan)
    codes = Counter(r.body.get("error", {}).get("code") for r in replies if r.status_code == 409)
    report(
        f"I2b 10 concurrent disbursements, different references, on {approved_loan}",
        {
            "Requests received": len(replies),
            "HTTP statuses": statuses(replies),
            "409 error codes": dict(codes),
            "Fineract disbursements": fineract,
            "ERP journal entries": erp,
            "Integration transactions": integration,
        },
    )
    assert statuses(replies) == {201: 1, 409: 9}
    assert codes == Counter({"LOAN_ALREADY_DISBURSED": 9})
    assert (fineract, erp, integration) == (1, 1, 1)


def test_i3_concurrent_repayments_apply_once_each(api: Api, active_loan: str) -> None:
    suffix = unique_suffix()
    amounts = [100_000 + 25_000 * i for i in range(10)]
    requests = [
        {"external_reference": f"RPY-{suffix}-{i}", "amount": amount}
        for i, amount in enumerate(amounts)
    ]
    before = api.get(f"/api/loans/{active_loan}")["balances"]

    replies = fire(
        len(requests) * 2,
        lambda i: api.post(f"/api/loans/{active_loan}/repayments", requests[i % len(requests)]),
    )

    after = api.get(f"/api/loans/{active_loan}")
    recorded = [api.get(f"/api/transactions/{r['external_reference']}") for r in requests]
    recorded_total = sum(t["amount"] for t in recorded)
    recorded_principal = sum(t["principal_portion"] for t in recorded)
    recorded_interest = sum(t["interest_portion"] for t in recorded)
    fineract = fineract_transactions(active_loan, REPAYMENT)
    erp = erp_entries(active_loan, "REPAYMENT")
    integration = integration_transactions(active_loan, "REPAYMENT")
    report(
        f"I3 {len(requests)} repayments, each sent twice concurrently, on {active_loan}",
        {
            "Requests received": len(replies),
            "HTTP statuses": statuses(replies),
            "Distinct references": len(requests),
            "Fineract repayments": fineract,
            "ERP journal entries": erp,
            "Integration transactions": integration,
            "Initial outstanding": before["total_outstanding"],
            "Repaid per Fineract breakdown": recorded_total,
            "  principal / interest": f"{recorded_principal} / {recorded_interest}",
            "Final outstanding (Fineract)": after["balances"]["total_outstanding"],
            "Initial - repaid": before["total_outstanding"] - recorded_total,
            "Statuses": dict(Counter(t["status"] for t in recorded)),
        },
    )
    assert (fineract, erp, integration) == (10, 10, 10)
    assert all(t["status"] == "COMPLETED" for t in recorded)
    assert recorded_principal + recorded_interest == recorded_total == sum(amounts)
    assert after["balances"]["total_outstanding"] == before["total_outstanding"] - recorded_total
    assert (
        after["balances"]["principal_outstanding"]
        == before["principal_outstanding"] - recorded_principal
    )


def test_last_line_of_defence_fineract_loan_state_without_our_service(
    approved_loan: str,
) -> None:
    suffix = unique_suffix()
    with fineract_http() as http:
        loan = http.get(f"/loans/external-id/{approved_loan}").raise_for_status().json()
        loan_id = loan["id"]
        approved_on = date(*loan["timeline"]["approvedOnDate"]).isoformat()

        def disburse(i: int) -> Reply:
            response = http.post(
                f"/loans/{loan_id}?command=disburse",
                json={
                    "actualDisbursementDate": approved_on,
                    "transactionAmount": 5_000_000,
                    "externalId": f"DIRECT-{suffix}-{i}",
                    "paymentTypeId": 1,
                    "dateFormat": "yyyy-MM-dd",
                    "locale": "en",
                },
            )
            return Reply(response.status_code, response.json())

        replies = fire(10, disburse)

    reasons = Counter(
        r.body.get("userMessageGlobalisationCode")
        or (r.body.get("errors") or [{}])[0].get("userMessageGlobalisationCode")
        for r in replies
        if r.status_code != 200
    )
    fineract = fineract_transactions(approved_loan, DISBURSEMENT)
    report(
        f"Last line of defence: 10 direct Fineract disbursements on {approved_loan}",
        {
            "Requests sent to Fineract": len(replies),
            "HTTP statuses": statuses(replies),
            "Rejection reasons": dict(reasons),
            "Fineract disbursements": fineract,
        },
    )
    assert statuses(replies).get(200) == 1
    assert fineract == 1
