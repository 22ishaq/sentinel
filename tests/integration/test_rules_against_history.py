"""Rules that depend on account history, evaluated against rows in PostgreSQL."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from sentinel.domain import Outcome
from sentinel.service import DecisionService
from sentinel.store.db import Pool
from tests import support


def _age_all_transactions(pool: Pool, seconds: int) -> None:
    support.execute(
        pool,
        "UPDATE transactions SET received_at = received_at - make_interval(secs => %s)",
        seconds,
    )


def test_attempt_velocity_triggers_after_the_configured_prior_attempts(
    make_service: Callable[..., DecisionService],
) -> None:
    service = make_service(support.RAPID_ATTEMPTS)
    outcomes = [service.submit(support.event()).decision.outcome for _ in range(5)]
    assert outcomes == [Outcome.APPROVE] * 3 + [Outcome.REVIEW] * 2


def test_attempts_outside_the_window_are_not_counted(
    make_service: Callable[..., DecisionService], pool: Pool
) -> None:
    service = make_service(support.RAPID_ATTEMPTS)
    for _ in range(3):
        service.submit(support.event())
    _age_all_transactions(pool, 301)
    assert service.submit(support.event()).decision.outcome is Outcome.APPROVE


def test_attempts_are_counted_per_account(make_service: Callable[..., DecisionService]) -> None:
    service = make_service(support.RAPID_ATTEMPTS)
    for _ in range(3):
        service.submit(support.event(account=support.account("acc_bob")))
    assert service.submit(support.event()).decision.outcome is Outcome.APPROVE


def test_spend_velocity_declines_the_payment_that_would_break_the_limit(
    make_service: Callable[..., DecisionService],
) -> None:
    service = make_service(support.DAILY_SPEND)
    outcomes = [service.submit(support.event(amount="400.00")).decision for _ in range(3)]
    assert [d.outcome for d in outcomes] == [Outcome.APPROVE, Outcome.APPROVE, Outcome.DECLINE]
    assert outcomes[2].reasons == ("daily_spend",)


def test_only_approved_spend_counts_towards_the_limit(
    make_service: Callable[..., DecisionService],
) -> None:
    service = make_service(support.DAILY_SPEND, support.BLOCKED_COUNTRY)
    blocked = support.event(amount="900.00", merchant=support.merchant(country="KP"))
    assert service.submit(blocked).decision.outcome is Outcome.DECLINE
    assert service.submit(support.event(amount="900.00")).decision.outcome is Outcome.APPROVE


def test_spend_in_other_currencies_does_not_count(
    make_service: Callable[..., DecisionService],
) -> None:
    service = make_service(support.DAILY_SPEND)
    service.submit(support.event(amount="90000", currency="JPY"))
    assert service.submit(support.event(amount="900.00")).decision.outcome is Outcome.APPROVE


def test_spend_outside_the_window_is_forgotten(
    make_service: Callable[..., DecisionService], pool: Pool
) -> None:
    service = make_service(support.DAILY_SPEND)
    service.submit(support.event(amount="900.00"))
    _age_all_transactions(pool, 86_401)
    assert service.submit(support.event(amount="900.00")).decision.outcome is Outcome.APPROVE


def test_concurrent_payments_cannot_jointly_exceed_the_spend_limit(
    make_service: Callable[..., DecisionService], pool: Pool
) -> None:
    # Without the per-account lock several of these would read the same prior spend
    # and all be approved. Limit 1000.00, ten payments of 300.00: exactly three fit.
    service = make_service(support.DAILY_SPEND)
    with ThreadPoolExecutor(max_workers=10) as executor:
        decisions = list(
            executor.map(lambda _: service.submit(support.event(amount="300.00")), range(10))
        )
    approved = [s for s in decisions if s.decision.outcome is Outcome.APPROVE]
    assert len(approved) == 3
    total = support.scalar(
        pool,
        "SELECT sum(t.amount_minor) FROM transactions t"
        " JOIN decisions d ON d.transaction_id = t.id WHERE d.outcome = 'APPROVE'",
    )
    assert total == 90000


def test_a_large_first_payment_to_a_merchant_is_reviewed(
    make_service: Callable[..., DecisionService],
) -> None:
    service = make_service(support.NEW_MERCHANT)
    first = service.submit(support.event(amount="60.00")).decision
    second = service.submit(support.event(amount="60.00")).decision
    assert (first.outcome, second.outcome) == (Outcome.REVIEW, Outcome.APPROVE)
    assert first.reasons == ("new_merchant",)


def test_small_first_payments_are_not_reviewed(
    make_service: Callable[..., DecisionService],
) -> None:
    service = make_service(support.NEW_MERCHANT)
    assert service.submit(support.event(amount="20.00")).decision.outcome is Outcome.APPROVE


def test_cross_border_ecommerce_is_reviewed_but_card_present_is_not(
    make_service: Callable[..., DecisionService],
) -> None:
    service = make_service(support.CROSS_BORDER)
    abroad = support.merchant(country="FR")
    online = service.submit(support.event(channel="ecommerce", merchant=abroad)).decision
    in_person = service.submit(support.event(channel="card_present", merchant=abroad)).decision
    assert (online.outcome, in_person.outcome) == (Outcome.REVIEW, Outcome.APPROVE)


def test_the_most_severe_triggered_rule_wins(service: DecisionService) -> None:
    payload = support.event(
        amount="600.00",
        channel="ecommerce",
        merchant=support.merchant(category="7995", country="FR"),
    )
    decision = service.submit(payload).decision
    assert decision.outcome is Outcome.DECLINE
    assert set(decision.reasons) >= {"blocked_category", "high_value", "cross_border"}
