"""도메인 단위 테스트 — DB 없이 돈다 (설계 문서 §9 계층 규칙)

policy.py 의 doctest 를 실제로 실행해서, 설계 문서에 적힌 수수료 구간과
코드가 어긋나면 CI가 잡아내게 한다.
"""

import doctest
from datetime import datetime, timedelta, timezone

import pytest

from app.domain import policy


def test_policy_doctests() -> None:
    """§1.1 정책 상수와 §7.3 수수료 계산의 예시가 실제로 맞는지."""
    result = doctest.testmod(policy, verbose=False)
    assert result.failed == 0, f"{result.failed} doctest(s) failed"


def test_seat_gate_ttl_is_shorter_than_hold_ttl() -> None:
    """§5.3 — 게이트가 DB보다 오래 남으면 유령 매진이 생긴다.

    이 부등식이 깨지는 순간 진단하기 가장 고약한 버그가 열리므로
    상수 변경을 테스트로 막아둔다.
    """
    assert policy.SEAT_GATE_TTL < policy.HOLD_TTL


def test_payment_window_fits_inside_hold() -> None:
    """§4 — 결제 승인 왕복이 hold 잔여보다 길면 승인 후 만료 창이 열린다."""
    assert policy.MIN_HOLD_REMAINING_FOR_PAYMENT < policy.HOLD_TTL


def test_cancel_closed_on_show_day() -> None:
    """§4 — 관람일 당일 취소 불가."""
    now = datetime(2026, 9, 30, 10, tzinfo=timezone.utc)
    with pytest.raises(policy.CancelClosed):
        policy.cancel_fee(100_000, now, now + timedelta(hours=9))


# TODO(1주차): app/domain/seat.py 의 can_transition() 을 §4 전이 표 전체로 검증
# TODO(3주차): app/domain/order.py 의 사가 전이 가드 검증
