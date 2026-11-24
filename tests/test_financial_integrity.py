"""Financial integrity guarantees required by specification s96.

The system must guarantee:
  * no duplicate counting
  * no silent deletion
  * no unexplained historical modification
  * allocation totals reconcile
  * ledger totals reconcile
  * reports reconcile with the database
  * export totals reconcile with the dashboard
"""

from __future__ import annotations

from datetime import date

import pytest

from app.enums import AllocationStatus, PaymentClassification
from app.money import (
    MoneyError,
    format_money,
    split_evenly,
    to_paise,
    to_rupees,
    try_to_paise,
)
from conftest import add_credit, add_sanction, recon_for, rupees, run_pipeline


# ---------------------------------------------------------------------------
# Money arithmetic
# ---------------------------------------------------------------------------
def test_money_is_exact_never_float():
    """A float ledger silently manufactures discrepancies."""
    total = 0
    for _ in range(1000):
        total += to_paise("0.10")
    assert total == 10000
    assert to_rupees(total) == to_rupees(to_paise("100.00"))
    assert format_money(total) == "₹100.00"


def test_indian_grouping_and_parsing():
    assert format_money(to_paise("1,23,456.78")) == "₹1,23,456.78"
    assert format_money(1234567890) == "₹1,23,45,678.90"
    assert to_paise("Rs. 10,000") == 1000000
    assert to_paise("(1,200.00)") == -120000
    assert to_paise("5,000.00 Dr") == -500000


def test_unparseable_amount_returns_none_never_a_guess():
    assert try_to_paise("garbage") is None
    assert try_to_paise("") is None
    assert try_to_paise(None) is None
    with pytest.raises(MoneyError):
        to_paise("not a number")


def test_split_always_sums_exactly():
    for total in [10000, 10001, 99999, 1, 7]:
        for parts in [2, 3, 7, 12]:
            shares = split_evenly(total, parts)
            assert sum(shares) == total, f"{total} into {parts} parts lost money"
            assert len(shares) == parts


# ---------------------------------------------------------------------------
# No duplicate counting (spec s69)
# ---------------------------------------------------------------------------
def test_allocation_cannot_exceed_the_transaction(db, org, employee, periods, aww_rule):
    from app.agents import allocation_agent

    txn = add_credit(db, org, employee, date(2026, 2, 7), 10500, "NEFT TREASURY CREDIT")

    allocation_agent.allocate(db, txn, employee, periods[1], rupees(10500))

    with pytest.raises(allocation_agent.AllocationError) as exc:
        allocation_agent.allocate(db, txn, employee, periods[2], rupees(1))
    assert "unallocated" in str(exc.value)

    db.refresh(txn)
    assert txn.allocated_paise == txn.amount_paise
    assert txn.unallocated_paise == 0


def test_partial_allocation_leaves_visible_remainder(db, org, employee, periods, aww_rule):
    """Spec s70: money that cannot be identified stays unallocated and visible."""
    from app.agents import allocation_agent

    txn = add_credit(db, org, employee, date(2026, 2, 7), 15000, "NEFT TREASURY CREDIT")
    allocation_agent.allocate(db, txn, employee, periods[1], rupees(10500))

    db.refresh(txn)
    assert txn.allocated_paise == rupees(10500)
    assert txn.unallocated_paise == rupees(4500)
    assert txn.status == "PARTIALLY_ALLOCATED"


def test_reprocessing_does_not_double_count(db, org, employee, periods, aww_rule):
    """Running the pipeline repeatedly must be idempotent."""
    add_credit(db, org, employee, date(2026, 2, 7), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="IDEM1")

    run_pipeline(db, employee)
    first = recon_for(db, employee, periods[1]).credited_paise

    for _ in range(3):
        run_pipeline(db, employee)

    again = recon_for(db, employee, periods[1]).credited_paise
    assert again == first == rupees(10500)

    from app.models import PaymentAllocation

    allocations = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.employee_id == employee.id,
                PaymentAllocation.period_id == periods[1].id,
                PaymentAllocation.status != AllocationStatus.REJECTED.value)
        .all()
    )
    assert sum(a.amount_paise for a in allocations) == rupees(10500)


def test_split_allocation_must_sum_to_the_original(db, org, employee, periods, aww_rule):
    from app.agents import allocation_agent

    txn = add_credit(db, org, employee, date(2026, 6, 8), 21000, "NEFT TREASURY CREDIT")
    allocation = allocation_agent.allocate(db, txn, employee, periods[4], rupees(21000))

    with pytest.raises(allocation_agent.AllocationError):
        allocation_agent.split(db, allocation,
                               [(periods[4].id, rupees(10000)), (periods[5].id, rupees(10000))],
                               user_id=None)

    parts = allocation_agent.split(
        db, allocation, [(periods[4].id, rupees(10500)), (periods[5].id, rupees(10500))],
        user_id=None,
    )
    assert sum(p.amount_paise for p in parts) == rupees(21000)


# ---------------------------------------------------------------------------
# Ledger totals reconcile (spec s96)
# ---------------------------------------------------------------------------
def test_ledger_totals_reconcile_across_the_organisation(db, org, structure, periods, aww_rule):
    from conftest import make_employee

    people = [
        make_employee(db, org, structure, code=f"E-{i}", name=f"Worker {i}")
        for i in range(4)
    ]
    for index, person in enumerate(people):
        add_credit(db, org, person, date(2026, 2, 7), 10500 - index * 500,
                   "NEFT TREASURY JANUARY 2026 HONORARIUM", utr=f"L{index}")
        run_pipeline(db, person)

    from app.models import PaymentAllocation, Reconciliation

    recon_total = sum(
        r.credited_paise for r in db.query(Reconciliation).all()
    )
    allocation_total = sum(
        a.amount_paise for a in db.query(PaymentAllocation)
        .filter(PaymentAllocation.status != AllocationStatus.REJECTED.value).all()
    )
    assert recon_total == allocation_total


def test_no_transaction_is_over_allocated_after_a_full_run(db, org, structure,
                                                           periods, aww_rule):
    from conftest import make_employee
    from app.models import BankTransaction

    person = make_employee(db, org, structure, code="E-OVER", name="Busy Worker")
    add_credit(db, org, person, date(2026, 2, 7), 10500, "NEFT TREASURY JANUARY 2026 HONORARIUM")
    add_credit(db, org, person, date(2026, 6, 8), 21000, "NEFT TREASURY HONORARIUM APRIL MAY 2026")
    add_credit(db, org, person, date(2026, 7, 7), 10500, "NEFT TREASURY JUNE 2026 HONORARIUM")
    run_pipeline(db, person)

    for txn in db.query(BankTransaction).all():
        allocated = sum(a.amount_paise for a in txn.allocations
                        if a.status != AllocationStatus.REJECTED.value)
        assert allocated <= txn.amount_paise, f"transaction {txn.uid} over-allocated"


# ---------------------------------------------------------------------------
# No silent deletion / modification (spec s51, s56)
# ---------------------------------------------------------------------------
def test_recalculation_supersedes_rather_than_overwrites(db, org, employee,
                                                         periods, aww_rule):
    from app.agents import entitlement_agent
    from app.models import Entitlement, SalaryRuleComponent

    _, first = entitlement_agent.calculate_and_store(db, employee, periods[1])
    original_uid, original_net = first.uid, first.net_paise

    # Change the rate, then recalculate.
    component = (
        db.query(SalaryRuleComponent)
        .filter(SalaryRuleComponent.rule_id == aww_rule.id,
                SalaryRuleComponent.code == "ALLOWANCE")
        .first()
    )
    component.amount_paise = rupees(900)
    db.flush()

    _, second = entitlement_agent.calculate_and_store(db, employee, periods[1])
    assert second.uid != original_uid
    assert second.net_paise == rupees(10900)
    assert second.supersedes_id == first.id

    # The original row still exists, unmodified.
    preserved = db.query(Entitlement).filter(Entitlement.uid == original_uid).first()
    assert preserved is not None
    assert preserved.net_paise == original_net
    assert preserved.is_current is False


def test_rejected_allocation_is_kept_not_deleted(db, org, employee, periods, aww_rule):
    from app.agents import allocation_agent
    from app.models import PaymentAllocation

    txn = add_credit(db, org, employee, date(2026, 2, 7), 10500, "NEFT TREASURY CREDIT")
    allocation = allocation_agent.allocate(db, txn, employee, periods[1], rupees(10500))
    allocation_id = allocation.id

    allocation_agent.reject(db, allocation, user_id=None)

    kept = db.get(PaymentAllocation, allocation_id)
    assert kept is not None, "a rejected allocation must remain for audit"
    assert kept.status == AllocationStatus.REJECTED.value

    db.refresh(txn)
    assert txn.allocated_paise == 0, "a rejected allocation must not count"


def test_reversal_keeps_both_transactions(db, org, employee, periods, aww_rule):
    from app.models import BankTransaction

    add_credit(db, org, employee, date(2026, 2, 7), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="KEEP1")
    add_credit(db, org, employee, date(2026, 2, 12), 10500,
               "NEFT RETURN PAYMENT REVERSED", utr="KEEP1", direction="DEBIT")
    run_pipeline(db, employee)

    assert db.query(BankTransaction).filter(
        BankTransaction.employee_id == employee.id).count() == 2


def test_every_change_writes_an_audit_entry(db, org, employee, periods, aww_rule):
    from app.audit import diff, record, snapshot
    from app.models import AuditLog

    before = snapshot(employee, ("full_name", "status"))
    employee.status = "ON_LEAVE"
    after = snapshot(employee, ("full_name", "status"))

    record(db, action="UPDATE_EMPLOYEE", entity_type="Employee", entity_id=employee.id,
           organization_id=org.id, old_value=before, new_value=after,
           reason="Went on leave")

    entry = db.query(AuditLog).filter(AuditLog.action == "UPDATE_EMPLOYEE").first()
    assert entry is not None
    assert entry.old_value_json["status"] == "ACTIVE"
    assert entry.new_value_json["status"] == "ON_LEAVE"
    assert entry.reason == "Went on leave"

    changed = diff(before, after)
    assert changed == {"status": {"from": "ACTIVE", "to": "ON_LEAVE"}}


# ---------------------------------------------------------------------------
# Reports reconcile with the database (spec s96)
# ---------------------------------------------------------------------------
def test_report_totals_match_the_database(db, org, employee, periods, aww_rule):
    from app.models import Reconciliation
    from app.services import reports

    add_credit(db, org, employee, date(2026, 2, 7), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="RP1")
    add_credit(db, org, employee, date(2026, 3, 9), 8500,
               "NEFT TREASURY FEBRUARY 2026 HONORARIUM", utr="RP2")
    run_pipeline(db, employee)

    report = reports.employee_statement(db, employee, 2026)

    db_rows = [r for r in db.query(Reconciliation)
               .filter(Reconciliation.employee_id == employee.id).all()
               if r.period.year == 2026]
    db_expected = sum(r.entitled_paise for r in db_rows)
    db_received = sum(r.credited_paise for r in db_rows)

    assert report.totals["expected_paise"] == db_expected
    assert report.totals["received_paise"] == db_received
    assert report.totals["received_paise"] == rupees(19000)


def test_exports_carry_the_same_totals(db, org, employee, periods, aww_rule):
    from app.services import reports

    add_credit(db, org, employee, date(2026, 2, 7), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="EX1")
    run_pipeline(db, employee)

    report = reports.employee_statement(db, employee, 2026)

    csv_bytes, _, _ = reports.export(report, "csv")
    text = csv_bytes.decode("utf-8-sig")
    assert format_money(report.totals["received_paise"]) in text

    for fmt in ("pdf", "excel"):
        content, mime, ext = reports.export(report, fmt)
        assert len(content) > 500, f"{fmt} export looks empty"
    assert reports.export(report, "pdf")[0][:4] == b"%PDF"


def test_report_labels_evidence_standing(db, org, employee, periods, aww_rule):
    """Spec s92: FACT vs OFFICIAL vs INFERENCE vs UNVERIFIED."""
    from app.services import reports

    add_credit(db, org, employee, date(2026, 3, 9), 8500,
               "NEFT TREASURY FEBRUARY 2026 HONORARIUM", utr="PV1")
    run_pipeline(db, employee)

    report = reports.employee_statement(db, employee, 2026)
    kinds = {p for p, _ in report.provenance_notes}
    assert "FACT" in kinds
    assert "UNVERIFIED" in kinds or "INFERENCE" in kinds

    joined = " ".join(text for _, text in report.provenance_notes)
    assert "fraud" not in joined.lower()


# ---------------------------------------------------------------------------
# Dashboard agrees with the ledger
# ---------------------------------------------------------------------------
def test_dashboard_totals_match_the_ledger(db, org, employee, periods, aww_rule):
    from app.models import Reconciliation
    from app.routers.dashboard import _employee_totals

    add_credit(db, org, employee, date(2026, 2, 7), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="DB1")
    add_credit(db, org, employee, date(2026, 3, 9), 8500,
               "NEFT TREASURY FEBRUARY 2026 HONORARIUM", utr="DB2")
    run_pipeline(db, employee)

    totals = _employee_totals(db, employee)

    due = [r for r in db.query(Reconciliation)
           .filter(Reconciliation.employee_id == employee.id).all()
           if r.outcome != "AWAITING_CONFIRMATION"]

    assert totals["received_paise"] == sum(r.credited_paise for r in due)
    assert totals["entitled_paise"] == sum(r.entitled_paise for r in due)
    assert totals["pending_paise"] == sum(max(0, r.difference_paise) for r in due)


# ---------------------------------------------------------------------------
# Monthly close (spec s36)
# ---------------------------------------------------------------------------
def test_closing_a_month_freezes_and_audits(db, org, employee, periods, aww_rule):
    from app.agents import monthly_close_agent
    from app.models import AuditLog

    add_credit(db, org, employee, date(2026, 2, 7), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="MC1")
    run_pipeline(db, employee)

    statement = monthly_close_agent.close_period(db, periods[1], None, "Test close")

    assert periods[1].status == "CLOSED"
    assert periods[1].closing_summary_json["received_paise"] == rupees(10500)
    assert statement.expected_paise == rupees(10500)

    assert db.query(AuditLog).filter(AuditLog.action == "CLOSE_PERIOD").count() == 1


def test_reopening_requires_a_reason(db, org, employee, periods, aww_rule):
    from app.agents import monthly_close_agent
    from app.models import AuditLog, User
    from app.security import hash_password

    user = User(organization_id=org.id, email="close@test.gov.in", full_name="Closer",
                password_hash=hash_password("Test1234"), role="SUPER_ADMIN")
    db.add(user)
    db.flush()

    monthly_close_agent.close_period(db, periods[1], user, "Close")

    with pytest.raises(ValueError):
        monthly_close_agent.reopen_period(db, periods[1], user, "")

    monthly_close_agent.reopen_period(db, periods[1], user, "Late statement arrived")
    assert periods[1].status == "REOPENED"

    entry = db.query(AuditLog).filter(AuditLog.action == "REOPEN_PERIOD").first()
    assert entry.reason == "Late statement arrived"


def test_manual_allocation_cannot_resurrect_a_reversed_credit(db, org, employee,
                                                              periods, aww_rule):
    """A human confirming a month must not make reversed money count as received.

    Found in practice: assigning a salary month to a credit that was later
    clawed back produced a CONFIRMED manual allocation, and the reconciliation
    counted it. The ledger then reported money the employee did not have.
    """
    from app.agents import allocation_agent, reconciliation_agent
    from app.enums import ReconOutcome

    credit = add_credit(db, org, employee, date(2026, 2, 7), 10500,
                        "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="REVMAN")
    add_credit(db, org, employee, date(2026, 2, 12), 10500,
               "NEFT RETURN PAYMENT REVERSED", utr="REVMAN", direction="DEBIT")
    run_pipeline(db, employee)

    db.refresh(credit)
    assert credit.is_reversed is True

    # A person now assigns the month by hand, exactly as the web form does.
    allocation_agent.allocate(
        db, credit, employee, periods[1], rupees(10500),
        method="MANUAL", status=AllocationStatus.CONFIRMED.value,
    )
    reconciliation_agent.reconcile(db, employee, periods[1])

    recon = recon_for(db, employee, periods[1])
    assert recon.credited_paise == 0, "reversed money must never count as received"
    assert recon.outcome == ReconOutcome.PAYMENT_REVERSED.value
    assert recon.first_credit_date is None, "a reversed credit is not a receipt date"


def test_reversed_arrear_payment_does_not_settle_anything(db, org, employee,
                                                          periods, aww_rule):
    """The same protection applies to arrear payments."""
    from app.agents import allocation_agent, reconciliation_agent
    from app.enums import PaymentClassification

    credit = add_credit(db, org, employee, date(2026, 5, 20), 2000,
                        "NEFT TREASURY ARREAR JANUARY 2026", utr="ARRREV")
    add_credit(db, org, employee, date(2026, 5, 25), 2000,
               "NEFT RETURN PAYMENT REVERSED", utr="ARRREV", direction="DEBIT")
    run_pipeline(db, employee)

    db.refresh(credit)
    assert credit.is_reversed is True

    allocation_agent.allocate(
        db, credit, employee, periods[1], rupees(2000),
        kind=PaymentClassification.ARREAR.value,
        method="MANUAL", status=AllocationStatus.CONFIRMED.value,
    )
    reconciliation_agent.reconcile(db, employee, periods[1])

    recon = recon_for(db, employee, periods[1])
    assert recon.arrear_received_paise == 0
