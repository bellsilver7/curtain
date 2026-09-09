"""좌석 상태 전이 규칙 — 설계 문서: 좌석 상태 전이

설계 문서의 전이 표가 그대로 코드가 되는 자리. 표준 라이브러리만 import 한다 (계층 규칙).
DB 도 Redis 도 HTTP 도 모르므로, 규칙 자체는 인프라 없이 테스트할 수 있다.

이 모듈은 규칙을 *선언*하고, 실제 강제는 두 곳에서 이뤄진다.

  1. SQL 의 조건부 WHERE status = 기대값 (원칙 "모든 상태 전이는 조건부 쓰기") — 동시성 하에서의 실제 방어
  2. DB 제약 ck_schedule_seats_hold_shape — 어긋난 행 자체를 거부

여기 있는 함수는 그 둘보다 앞단에서 "요청이 애초에 말이 되는가"를 판정하고,
어긋남을 테스트로 고정하기 위한 것이다. 여기서 통과한다고 선점이 성공하는 것은
아니다 — 경합에서 지면 SQL 이 0행을 돌려준다.
"""

from __future__ import annotations

from enum import StrEnum


class SeatStatus(StrEnum):
    """DB 의 seat_status ENUM 과 1:1. 값을 바꾸면 마이그레이션이 필요하다 (결정 기록: 마이그레이션 전략)."""

    AVAILABLE = "AVAILABLE"
    HELD = "HELD"
    BOOKED = "BOOKED"


#: 허용된 전이. 설계 문서의 전이 표와 같은 내용이며, 테스트가 둘의 일치를 지킨다.
#:
#: 취소는 좌석 상태가 아니라 주문 상태(orders.status = 'CANCELED')로 표현한다.
#: 좌석의 상태는 "지금 팔 수 있는가"만 답하면 되고, 환불 대기 중인 좌석은 팔 수
#: 없으므로 BOOKED 로 남는다 (취소와 환불: 좌석 복원은 환불 성공 확인 후). 그래서
#: seat_status ENUM 에 CANCELLED 가 없다.
ALLOWED: dict[SeatStatus, frozenset[SeatStatus]] = {
    SeatStatus.AVAILABLE: frozenset({SeatStatus.HELD}),
    SeatStatus.HELD: frozenset({SeatStatus.BOOKED, SeatStatus.AVAILABLE}),
    SeatStatus.BOOKED: frozenset({SeatStatus.AVAILABLE}),
}

#: 판매 가능한 상태. 만료된 hold 는 상태가 HELD 여도 선점 대상이므로
#: 이 집합만으로 판정하지 않는다 — is_claimable() 을 쓸 것.
SELLABLE = frozenset({SeatStatus.AVAILABLE})


class IllegalTransition(Exception):
    """전이 표에 없는 전이."""

    def __init__(self, src: SeatStatus, dst: SeatStatus) -> None:
        super().__init__(f"{src} → {dst} 는 허용되지 않는 전이입니다")
        self.src, self.dst = src, dst


def can_transition(src: SeatStatus, dst: SeatStatus) -> bool:
    """전이 표에 있는 전이인가.

    >>> can_transition(SeatStatus.AVAILABLE, SeatStatus.HELD)
    True
    >>> can_transition(SeatStatus.AVAILABLE, SeatStatus.BOOKED)   # hold 를 건너뛸 수 없다
    False
    >>> can_transition(SeatStatus.BOOKED, SeatStatus.HELD)        # 판 좌석을 되잡을 수 없다
    False
    """
    return dst in ALLOWED[src]


def assert_transition(src: SeatStatus, dst: SeatStatus) -> None:
    if not can_transition(src, dst):
        raise IllegalTransition(src, dst)


def is_claimable(status: SeatStatus, hold_expired: bool) -> bool:
    """선점 대상인가. 만료된 hold 는 스윕 워커를 기다리지 않고 선점 쿼리가 즉시 회수한다.

    >>> is_claimable(SeatStatus.AVAILABLE, hold_expired=False)
    True
    >>> is_claimable(SeatStatus.HELD, hold_expired=True)     # TTL 지난 hold
    True
    >>> is_claimable(SeatStatus.HELD, hold_expired=False)    # 남이 잡고 있는 중
    False
    >>> is_claimable(SeatStatus.BOOKED, hold_expired=True)   # 팔린 좌석은 영원히 아님
    False
    """
    if status is SeatStatus.AVAILABLE:
        return True
    return status is SeatStatus.HELD and hold_expired
