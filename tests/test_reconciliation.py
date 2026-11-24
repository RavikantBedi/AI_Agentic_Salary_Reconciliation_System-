"""Reconciliation scenarios required by specification s94.

Each test names the specification section it covers.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.enums import (
    AllocationStatus,
    ArrearStatus,
    DeductionStatus,
    PaymentClassification,
    ReconOutcome,
    TransactionStatus,
)
from conftest import (
    add_credit,
    add_sanction,
    make_employee,
    recon_for,
    rupees,
    run_pipeline,
)


# ---------------------------------------------------------------------------
# Normal salary
# ---------------------------------------------------------------------------
def test_normal_salary_fully_reconciled(db, org, structure, employee, periods, aww_rule):
    """A full, on-time payment reconciles exactly."""
    add_credit(db, org, employee, date(2026, 2, 7), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="UTR202602070001")
    run_pipeline(db, employee)

    recon = recon_for(db, employee, periods[1])
    assert recon.outcome == ReconOutcome.FULLY_RECONCILED.value
    assert recon.entitled_paise == rupees(10500)
    assert recon.credited_paise == rupees(10500)
    assert recon.difference_paise == 0


def test_entitlement_breakdown_is_traceable(db, employee, periods, aww_rule):
    """Spec s91: every component of the expected amount is recorded."""
    from app.agents import entitlement_agent

    result, entitlement = entitlement_agent.calculate_and_store(db, employee, periods[1])
    assert result.ok
    assert entitlement.gross_paise == rupees(10500)
    assert entitlement.net_paise == rupees(10500)
    assert entitlement.central_paise == rupees(4500)
    assert entitlement.state_paise == rupees(6000)

    codes = {c.code for c in entitlement.components}
    assert codes == {"CENTRAL_SHARE", "STATE_SHARE", "ALLOWANCE"}
    # Each line names where it came from.
    assert all(c.source_reference for c in entitlement.components)


# ---------------------------------------------------------------------------
# Partial payment (spec s12)
# ---------------------------------------------------------------------------
def test_partial_payment_creates_unexplained_deduction(db, org, employee, periods, aww_rule):
    add_credit(db, org, employee, date(2026, 3, 9), 8500,
               "NEFT TREASURY FEBRUARY 2026 HONORARIUM", utr="UTR202603090001")
    run_pipeline(db, employee)

    recon = recon_for(db, employee, periods[2])
    assert recon.credited_paise == rupees(8500)
    assert recon.difference_paise == rupees(2000)
    assert recon.outcome == ReconOutcome.UNEXPLAINED_SHORT_CREDIT.value
    assert recon.unexplained_deduction_paise == rupees(2000)
    # It must never be described as fraud.
    assert "fraud" not in (recon.explanation_en or "").lower()


# ---------------------------------------------------------------------------
# Overpayment (spec s13)
# ---------------------------------------------------------------------------
def test_overpayment_is_flagged_not_absorbed(db, org, employee, periods, aww_rule):
    add_credit(db, org, employee, date(2026, 3, 7), 12500,
               "NEFT TREASURY FEBRUARY 2026 HONORARIUM", utr="UTR202603070001")
    run_pipeline(db, employee)

    recon = recon_for(db, employee, periods[2])
    assert recon.outcome == ReconOutcome.EXCESS_CREDIT.value
    assert recon.difference_paise == -rupees(2000)
    # The reason must NOT be asserted.
    assert "not been established" in recon.explanation_en


# ---------------------------------------------------------------------------
# One month paid in several transactions (spec s11)
# ---------------------------------------------------------------------------
def test_one_month_paid_in_two_transactions(db, org, employee, periods, aww_rule):
    add_credit(db, org, employee, date(2026, 4, 6), 6000,
               "NEFT TREASURY MARCH 2026 HONORARIUM PART", utr="UTR1")
    add_credit(db, org, employee, date(2026, 4, 14), 4500,
               "NEFT TREASURY MARCH 2026 HONORARIUM BALANCE", utr="UTR2")
    run_pipeline(db, employee)

    recon = recon_for(db, employee, periods[3])
    assert recon.credited_paise == rupees(10500)
    assert recon.outcome == ReconOutcome.FULLY_RECONCILED.value


# ---------------------------------------------------------------------------
# One payment covering several months (spec s10)
# ---------------------------------------------------------------------------
def test_multi_month_payment_is_split_exactly(db, org, employee, periods, aww_rule):
    txn = add_credit(db, org, employee, date(2026, 6, 8), 21000,
                     "NEFT TREASURY HONORARIUM APRIL MAY 2026", utr="UTRMULTI")
    run_pipeline(db, employee)

    db.refresh(txn)
    allocations = [a for a in txn.allocations if a.status != AllocationStatus.REJECTED.value]
    assert len(allocations) == 2
    # The split must sum EXACTLY to the transaction (spec s69).
    assert sum(a.amount_paise for a in allocations) == txn.amount_paise

    assert recon_for(db, employee, periods[4]).credited_paise == rupees(10500)
    assert recon_for(db, employee, periods[5]).credited_paise == rupees(10500)


# ---------------------------------------------------------------------------
# Missing month (spec s94)
# ---------------------------------------------------------------------------
def test_missing_month_is_not_paid(db, org, employee, periods, aww_rule):
    add_credit(db, org, employee, date(2026, 2, 7), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="U1")
    run_pipeline(db, employee)

    recon = recon_for(db, employee, periods[2])
    assert recon.outcome == ReconOutcome.NOT_PAID.value
    assert recon.credited_paise == 0


def test_future_month_is_not_reported_as_unpaid(db, org, employee, periods, aww_rule):
    """A month whose pay date has not arrived is not 'missing'."""
    run_pipeline(db, employee)

    december = recon_for(db, employee, periods[12])
    assert december.outcome == ReconOutcome.AWAITING_CONFIRMATION.value

    from app.models import Arrear

    assert db.query(Arrear).filter(Arrear.origin_period_id == periods[12].id).count() == 0


# ---------------------------------------------------------------------------
# Three-way reconciliation (spec s29)
# ---------------------------------------------------------------------------
def test_short_sanction(db, org, employee, periods, aww_rule):
    """Entitled 10500, sanctioned 8000, received 8000 -> the gap is upstream."""
    add_sanction(db, org, employee, periods[3], 8000)
    add_credit(db, org, employee, date(2026, 4, 7), 8000,
               "NEFT TREASURY MARCH 2026 HONORARIUM", utr="U3")
    run_pipeline(db, employee)

    recon = recon_for(db, employee, periods[3])
    assert recon.outcome == ReconOutcome.SHORT_SANCTION.value
    assert recon.sanctioned_paise == rupees(8000)
    assert "sanction" in recon.explanation_en.lower()


def test_sanctioned_but_not_credited(db, org, employee, periods, aww_rule):
    add_sanction(db, org, employee, periods[3], 10500)
    run_pipeline(db, employee)

    recon = recon_for(db, employee, periods[3])
    assert recon.outcome == ReconOutcome.SANCTIONED_NOT_CREDITED.value
    assert recon.credited_paise == 0


def test_sanctioned_partially_credited(db, org, employee, periods, aww_rule):
    add_sanction(db, org, employee, periods[3], 10500)
    add_credit(db, org, employee, date(2026, 4, 7), 8500,
               "NEFT TREASURY MARCH 2026 HONORARIUM", utr="U4")
    run_pipeline(db, employee)

    recon = recon_for(db, employee, periods[3])
    assert recon.outcome == ReconOutcome.SANCTIONED_PARTIALLY_CREDITED.value


def test_missing_sanction_is_unknown_not_zero(db, org, employee, periods, aww_rule):
    """Spec s52: absence of evidence must not become a figure of zero."""
    add_credit(db, org, employee, date(2026, 2, 7), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="U5")
    run_pipeline(db, employee)

    recon = recon_for(db, employee, periods[1])
    assert recon.sanctioned_paise is None
    assert "not on record" in recon.explanation_en or "does not" in recon.explanation_en


# ---------------------------------------------------------------------------
# Reversal (spec s15)
# ---------------------------------------------------------------------------
def test_reversed_payment_nets_to_zero(db, org, employee, periods, aww_rule):
    add_credit(db, org, employee, date(2026, 2, 7), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="UTRREV")
    add_credit(db, org, employee, date(2026, 2, 12), 10500,
               "NEFT RETURN PAYMENT REVERSED", utr="UTRREV", direction="DEBIT")
    run_pipeline(db, employee)

    recon = recon_for(db, employee, periods[1])
    assert recon.outcome == ReconOutcome.PAYMENT_REVERSED.value
    assert recon.credited_paise == 0


# ---------------------------------------------------------------------------
# Duplicates (spec s14) - flag, never delete
# ---------------------------------------------------------------------------
def test_duplicate_payment_is_flagged_and_kept(db, org, employee, periods, aww_rule):
    from app.models import Anomaly, BankTransaction

    add_credit(db, org, employee, date(2026, 2, 7), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="UTRDUP")
    add_credit(db, org, employee, date(2026, 2, 7), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM SECOND", utr="UTRDUP")
    run_pipeline(db, employee)

    # Both records still exist -- nothing was deleted.
    assert db.query(BankTransaction).filter(BankTransaction.employee_id == employee.id).count() == 2

    flagged = db.query(Anomaly).filter(Anomaly.code == "POSSIBLE_DUPLICATE").count()
    assert flagged >= 1


# ---------------------------------------------------------------------------
# Unknown month (spec s9) - do NOT auto-assign
# ---------------------------------------------------------------------------
def test_payment_without_month_is_not_auto_assigned(db, org, employee, periods, aww_rule):
    from app.models import MonthCandidate

    txn = add_credit(db, org, employee, date(2026, 2, 7), 10500, "NEFT GOVT CREDIT")
    run_pipeline(db, employee)
    db.refresh(txn)

    assert txn.status == TransactionStatus.REVIEW_REQUIRED.value
    assert txn.allocated_paise == 0
    assert txn.unallocated_paise == txn.amount_paise

    candidates = db.query(MonthCandidate).filter(MonthCandidate.transaction_id == txn.id).all()
    assert len(candidates) >= 2, "alternatives must be offered, not one silent guess"
    assert all(c.reasons_json for c in candidates), "each candidate must explain itself"


def test_government_marker_alone_does_not_classify(db, org, employee, periods, aww_rule):
    """Spec s7: never classify from the word GOVT alone."""
    from app.agents import classification_agent

    txn = add_credit(db, org, employee, date(2026, 5, 20), 7777, "NEFT GOVT")
    result = classification_agent.classify(db, txn, employee)
    assert result.classification == PaymentClassification.UNKNOWN.value


# ---------------------------------------------------------------------------
# Arrears (spec s18)
# ---------------------------------------------------------------------------
def test_arrear_created_and_settled(db, org, employee, periods, aww_rule):
    from app.models import Arrear

    # January short by 2000.
    add_credit(db, org, employee, date(2026, 2, 7), 8500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="A1")
    run_pipeline(db, employee)

    arrear = db.query(Arrear).filter(Arrear.origin_period_id == periods[1].id).first()
    assert arrear is not None
    assert arrear.outstanding_paise == rupees(2000)
    assert arrear.status == ArrearStatus.OPEN.value

    # An arrear payment arrives later and is applied to January.
    from app.agents import allocation_agent

    txn = add_credit(db, org, employee, date(2026, 4, 20), 2000,
                     "NEFT TREASURY ARREAR JANUARY 2026", utr="A2")
    allocation = allocation_agent.allocate(
        db, txn, employee, periods[1], rupees(2000),
        kind=PaymentClassification.ARREAR.value,
        status=AllocationStatus.CONFIRMED.value,
    )
    allocation_agent.settle_arrears(db, allocation)

    db.refresh(arrear)
    assert arrear.settled_paise == rupees(2000)
    assert arrear.remaining_paise == 0
    assert arrear.status == ArrearStatus.SETTLED.value


# ---------------------------------------------------------------------------
# Joining and leaving mid-month (spec s21)
# ---------------------------------------------------------------------------
def test_joining_mid_month_prorates(db, org, structure, periods, aww_rule):
    from app.agents import entitlement_agent

    joiner = make_employee(db, org, structure, code="EMP-JOIN", name="Late Joiner",
                           joining=date(2026, 1, 16))
    result = entitlement_agent.calculate(db, joiner, periods[1])

    assert result.ok
    # 16 days served out of 31 in January.
    # proration_factor is stored rounded to 6 decimal places
    assert result.proration_factor == pytest.approx(16 / 31, abs=1e-6)
    assert result.net_paise < rupees(10500)


def test_not_in_service_yields_no_entitlement(db, org, structure, periods, aww_rule):
    from app.agents import entitlement_agent

    joiner = make_employee(db, org, structure, code="EMP-FUTURE", name="Future Joiner",
                           joining=date(2026, 6, 1))
    result = entitlement_agent.calculate(db, joiner, periods[1])
    assert not result.ok
    assert "not in service" in (result.problem or "").lower()


def test_leaving_mid_month_prorates(db, org, structure, periods, aww_rule):
    from app.agents import entitlement_agent

    leaver = make_employee(db, org, structure, code="EMP-LEAVE", name="Early Leaver",
                           relieving=date(2026, 1, 10))
    result = entitlement_agent.calculate(db, leaver, periods[1])
    assert result.ok
    assert result.proration_factor == pytest.approx(10 / 31, abs=1e-6)


def test_proration_policy_none_pays_full_month(db, org, structure, gov_order, periods):
    """Spec s20: do NOT assume every rule prorates."""
    from app.agents import entitlement_agent
    from app.enums import ComponentKind, FundingSource, ProrationPolicy
    from conftest import make_rule

    make_rule(db, org, structure["awh"], "TEST-AWH-NOPRO", [
        ("BASE", ComponentKind.BASE.value, FundingSource.STATE.value, 5000),
    ], proration=ProrationPolicy.NONE.value)

    joiner = make_employee(db, org, structure, code="EMP-AWH-J", name="Helper Joiner",
                           designation_key="awh", joining=date(2026, 1, 20))
    result = entitlement_agent.calculate(db, joiner, periods[1])
    assert result.ok
    assert result.net_paise == rupees(5000), "this rule must not prorate"


# ---------------------------------------------------------------------------
# No rule configured (spec s52)
# ---------------------------------------------------------------------------
def test_missing_rule_never_invents_an_amount(db, org, structure, periods):
    from app.agents import entitlement_agent

    orphan = make_employee(db, org, structure, code="EMP-NORULE", name="No Rule Worker")
    result = entitlement_agent.calculate(db, orphan, periods[1])

    assert not result.ok
    assert result.gross_paise == 0
    assert "no active salary rule" in (result.problem or "").lower()

    run_pipeline(db, orphan)
    recon = recon_for(db, orphan, periods[1])
    assert recon.outcome == ReconOutcome.NO_ENTITLEMENT_RULE.value


# ---------------------------------------------------------------------------
# Delay tracking (spec s17)
# ---------------------------------------------------------------------------
def test_delay_days_recorded(db, org, employee, periods, aww_rule):
    add_credit(db, org, employee, date(2026, 2, 22), 10500,
               "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="D1")
    run_pipeline(db, employee)

    recon = recon_for(db, employee, periods[1])
    assert recon.expected_pay_date == date(2026, 2, 7)
    assert recon.first_credit_date == date(2026, 2, 22)
    assert recon.delay_days == 15


# ---------------------------------------------------------------------------
# Retroactive rule change (spec s19)
# ---------------------------------------------------------------------------
def test_retroactive_increase_creates_adjustment_not_a_rewrite(db, org, structure,
                                                              employee, periods, aww_rule):
    from app.agents import arrear_agent, entitlement_agent
    from app.enums import ComponentKind, FundingSource, RuleStatus
    from app.models import Entitlement
    from conftest import make_rule

    # Pay April at the old rate.
    add_credit(db, org, employee, date(2026, 5, 7), 10500,
               "NEFT TREASURY APRIL 2026 HONORARIUM", utr="R1")
    run_pipeline(db, employee)
    original = recon_for(db, employee, periods[4])
    assert original.entitled_paise == rupees(10500)

    # A new order raises the rate from 1 April.
    new_rule = make_rule(db, org, structure["aww"], "TEST-AWW-002", [
        ("CENTRAL_SHARE", ComponentKind.BASE.value, FundingSource.CENTRAL.value, 5000),
        ("STATE_SHARE", ComponentKind.BASE.value, FundingSource.STATE.value, 6500),
        ("ALLOWANCE", ComponentKind.ALLOWANCE.value, FundingSource.STATE.value, 500),
    ], effective_from=date(2026, 4, 1), status=RuleStatus.DRAFT.value, version=2)

    # Simulation must not write anything.
    before_rows = db.query(Entitlement).count()
    result = arrear_agent.simulate_retroactive_change(db, new_rule, [employee])
    assert db.query(Entitlement).count() == before_rows
    assert new_rule.status == RuleStatus.DRAFT.value
    assert result.total_delta_paise > 0

    # Activate and recalculate.
    from app.agents import rule_agent

    rule_agent.activate(db, new_rule)
    old_entitlement = (
        db.query(Entitlement)
        .filter(Entitlement.employee_id == employee.id,
                Entitlement.period_id == periods[4].id,
                Entitlement.is_current.is_(True))
        .first()
    )
    _, new_entitlement = entitlement_agent.calculate_and_store(db, employee, periods[4])
    assert new_entitlement.net_paise == rupees(12000)

    adjustment, arrear = arrear_agent.apply_retroactive_change(
        db, employee, periods[4], new_entitlement, old_entitlement, "New government order"
    )
    assert adjustment.delta_paise == rupees(1500)
    assert arrear is not None
    assert arrear.outstanding_paise == rupees(1500)

    # History is preserved, not overwritten.
    superseded = (
        db.query(Entitlement)
        .filter(Entitlement.employee_id == employee.id,
                Entitlement.period_id == periods[4].id,
                Entitlement.is_current.is_(False))
        .all()
    )
    assert superseded, "the original calculation must still exist"
    assert superseded[0].net_paise == rupees(10500)


# ---------------------------------------------------------------------------
# Bank account change (spec s24)
# ---------------------------------------------------------------------------
def test_bank_account_change_is_not_a_duplicate(db, org, employee, periods, aww_rule):
    from app.agents import allocation_agent
    from app.models import EmployeeBankAccount
    from app.security import encrypt_value, last4

    old = employee.bank_accounts[0]
    old.effective_to = date(2026, 2, 28)
    old.is_primary = False
    db.add(EmployeeBankAccount(
        employee_id=employee.id, bank_name="New Bank",
        account_number_encrypted=encrypt_value("99998888777766"),
        account_last4=last4("99998888777766"), effective_from=date(2026, 3, 1),
        is_primary=True,
    ))
    db.flush()

    a = add_credit(db, org, employee, date(2026, 2, 7), 10500,
                   "NEFT TREASURY JANUARY 2026 HONORARIUM", utr="ACC1")
    b = add_credit(db, org, employee, date(2026, 3, 7), 10500,
                   "NEFT TREASURY FEBRUARY 2026 HONORARIUM", utr="ACC2")
    run_pipeline(db, employee)

    # Different months, different accounts: not duplicates.
    assert allocation_agent.find_possible_duplicates(db, a) == []
    assert recon_for(db, employee, periods[1]).outcome == ReconOutcome.FULLY_RECONCILED.value
    assert recon_for(db, employee, periods[2]).outcome == ReconOutcome.FULLY_RECONCILED.value


# ---------------------------------------------------------------------------
# Cash payment (spec s25)
# ---------------------------------------------------------------------------
def test_cash_payment_stays_at_level_zero(db, org, employee, periods, aww_rule):
    from app.services.importer import record_manual_transaction

    txn = record_manual_transaction(
        db, organization=org, employee=employee, txn_date=date(2026, 2, 7),
        amount_paise=rupees(10500), description="January 2026 honorarium paid in cash",
        is_cash=True,
    )
    assert txn.is_cash
    assert txn.verification_level == 0
    assert "DOCUMENT VERIFICATION REQUIRED" in txn.notes

    run_pipeline(db, employee)
    recon = recon_for(db, employee, periods[1])
    # Reconciled in amount, but never treated as independently verified.
    assert recon.credit_verification_level == 0


# ---------------------------------------------------------------------------
# Zero and extreme amounts (spec s94)
# ---------------------------------------------------------------------------
def test_very_large_and_very_small_amounts(db, org, employee, periods, aww_rule):
    from app.money import format_money

    tiny = add_credit(db, org, employee, date(2026, 8, 3), 0.01, "NEFT TEST TINY")
    huge = add_credit(db, org, employee, date(2026, 8, 4), 99999999.99, "NEFT TEST HUGE")
    assert tiny.amount_paise == 1
    assert huge.amount_paise == 9999999999
    assert format_money(huge.amount_paise) == "₹9,99,99,999.99"


def test_leap_year_and_month_end(db):
    from app.services.textutils import days_in_month, month_end

    assert days_in_month(2024, 2) == 29
    assert days_in_month(2026, 2) == 28
    assert month_end(2026, 12) == date(2026, 12, 31)
    assert month_end(2026, 2) == date(2026, 2, 28)


def test_financial_year_boundary(db):
    from app.services.textutils import financial_year

    assert financial_year(date(2026, 3, 31)) == "2025-26"
    assert financial_year(date(2026, 4, 1)) == "2026-27"
