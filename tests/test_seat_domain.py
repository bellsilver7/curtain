"""도메인 단위 테스트 — DB 없이 돈다 (설계 문서: 계층 규칙)

policy.py 의 doctest 를 실제로 실행해서, 설계 문서에 적힌 수수료 구간과
코드가 어긋나면 CI가 잡아내게 한다.
"""

import doctest
from datetime import UTC, datetime, timedelta

import pytest

from app.domain import order, payment, policy, seat


def test_policy_doctests() -> None:
    """정책 상수 정책 상수와 취소와 환불 수수료 계산의 예시가 실제로 맞는지."""
    result = doctest.testmod(policy, verbose=False)
    assert result.failed == 0, f"{result.failed} doctest(s) failed"


def test_seat_doctests() -> None:
    """좌석 상태 전이 상태 전이 규칙의 예시가 실제로 맞는지."""
    result = doctest.testmod(seat, verbose=False)
    assert result.failed == 0, f"{result.failed} doctest(s) failed"


def test_transition_table_matches_design() -> None:
    """좌석 상태 전이 전이 표 전체를 코드로 고정한다.

    허용 목록을 넓히는 변경은 이 테스트를 반드시 깨야 한다 — 좌석 상태가
    한 칸 늘어나는 것은 설계 변경이고, 조용히 통과해서는 안 된다.
    """
    S = seat.SeatStatus
    allowed = {(src, dst) for src, dsts in seat.ALLOWED.items() for dst in dsts}
    assert allowed == {
        (S.AVAILABLE, S.HELD),  # 선점
        (S.HELD, S.BOOKED),  # 결제 승인
        (S.HELD, S.AVAILABLE),  # TTL 만료 · 결제 실패 · 이탈
        (S.BOOKED, S.AVAILABLE),  # 취소 + 환불 완료 후 재고 복원
    }


def test_booked_seat_never_becomes_claimable() -> None:
    """팔린 좌석은 어떤 경우에도 선점 대상이 아니다.

    hold 메타데이터가 남아 있는 이상한 행이 생겨도 재판매되면 안 된다.
    """
    for expired in (True, False):
        assert not seat.is_claimable(seat.SeatStatus.BOOKED, hold_expired=expired)


def test_seat_gate_ttl_is_shorter_than_hold_ttl() -> None:
    """Redis 좌석 게이트 — 게이트가 DB보다 오래 남으면 유령 매진이 생긴다.

    이 부등식이 깨지는 순간 진단하기 가장 고약한 버그가 열리므로
    상수 변경을 테스트로 막아둔다.
    """
    assert policy.SEAT_GATE_TTL < policy.HOLD_TTL


def test_payment_window_fits_inside_hold() -> None:
    """좌석 상태 전이 — 결제 승인 왕복이 hold 잔여보다 길면 승인 후 만료 창이 열린다."""
    assert policy.MIN_HOLD_REMAINING_FOR_PAYMENT < policy.HOLD_TTL


def test_cancel_closed_on_show_day() -> None:
    """좌석 상태 전이 — 관람일 당일 취소 불가."""
    now = datetime(2026, 9, 30, 10, tzinfo=UTC)
    with pytest.raises(policy.CancelClosed):
        policy.cancel_fee(100_000, now, now + timedelta(hours=9))


def test_order_doctests() -> None:
    """주문 전이 규칙의 예시가 실제로 맞는지."""
    result = doctest.testmod(order, verbose=False)
    assert result.failed == 0, f"{result.failed} doctest(s) failed"


def test_payment_doctests() -> None:
    """결제 결과 어휘의 예시가 실제로 맞는지."""
    result = doctest.testmod(payment, verbose=False)
    assert result.failed == 0, f"{result.failed} doctest(s) failed"


def test_order_transition_table_matches_design() -> None:
    """주문 전이 표 전체를 코드로 고정한다 (결제 사가).

    이 표가 곧 멱등성의 정의다. 확정이 PENDING → PAID 뿐이어야 두 번째 확정이
    0행을 갱신하고 조용히 끝난다 — 허용 목록이 넓어지는 변경은 반드시 이 테스트를
    깨야 한다.
    """
    st = order.OrderStatus
    allowed = {(src, dst) for src, dsts in order.ALLOWED.items() for dst in dsts}
    assert allowed == {
        (st.PENDING, st.PAID),      # 승인 확인 (동기 응답 · 웹훅 · 리컨실러)
        (st.PENDING, st.FAILED),    # 승인 거절, 또는 승인 후 좌석 불가 → 환불
        (st.PENDING, st.CANCELED),  # 되물어보니 미승인
        (st.PAID, st.CANCELED),     # 사용자 취소 (환불 성공 확인 후)
    }


def test_paid_order_cannot_fail() -> None:
    """확정된 주문은 실패로 갈 수 없다.

    PAID → FAILED 를 허용하면 "결제는 됐는데 실패로 닫힌" 주문이 생기고,
    그 주문의 돈이 어디 있는지 아무 기록도 남지 않는다.
    """
    assert not order.can_transition(order.OrderStatus.PAID, order.OrderStatus.FAILED)
    assert order.is_terminal(order.OrderStatus.CANCELED)
    assert order.is_terminal(order.OrderStatus.FAILED)


def test_unknown_is_the_only_inconclusive_result() -> None:
    """무응답만 결론이 아니다 (원칙 "외부 호출은 리컨실러가 받친다").

    이 성질이 깨지면 — 예를 들어 UNKNOWN 이 결론으로 취급되면 — 호출부가
    무응답에서 바로 취소하게 되고, 실제로 승인됐던 결제가 좌석 없이 남는다.
    """
    inconclusive = {r for r in payment.PayResult if not r.is_conclusive}
    assert inconclusive == {payment.PayResult.UNKNOWN}


def test_seatmap_cache_ttl_is_short_enough() -> None:
    """좌석맵 캐시 TTL 은 hold TTL 보다 훨씬 짧아야 한다.

    좌석맵이 hold 보다 오래 낡아 있으면 사용자가 이미 팔린 좌석을 계속
    고르게 되고, 선점 API 가 매번 409 를 내는 상황이 된다.

    인프라가 필요 없는 정책 검사라 여기 둔다 — make test-unit 에서도 돈다.
    """
    assert policy.SEATMAP_CACHE_TTL.total_seconds() > 0
    assert policy.SEATMAP_CACHE_TTL < policy.HOLD_TTL
