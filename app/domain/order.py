"""주문 상태 전이 규칙 — 설계 문서: 결제 사가

seat.py 와 같은 자리에 있는 모듈이다. 표준 라이브러리만 import 하고 (계층 규칙),
규칙을 선언만 한다 — 실제 강제는 SQL 의 조건부 `WHERE status = 기대값` 이다
(원칙 "모든 상태 전이는 조건부 쓰기").

사가에서 이 표가 하는 일은 좌석 쪽보다 하나 더 있다. 전이 표가 곧 **멱등성의
정의**다. 확정이 PENDING → PAID 뿐이라면, 이미 PAID 인 주문에 같은 확정이
다시 와도 0행이 갱신되고 조용히 끝난다. 웹훅 재전송·리컨실러·동기 응답이 서로를
모르면서도 안전한 이유가 이 한 줄이다.
"""

from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    """DB 의 order_status ENUM 과 1:1.

    철자에 주의한다 — DB 값은 CANCELED (L 한 개)다. 설계 문서 산문에는
    CANCELLED 로 적힌 곳이 있지만 ENUM 이 진실이다.
    """

    #: 승인 요청을 보냈고 결론이 없다. 리컨실러가 훑는 대상.
    PENDING = "PENDING"
    #: 확정. 좌석은 BOOKED, order_items 가 존재한다.
    PAID = "PAID"
    #: 사용자 취소. 환불 성공을 확인한 뒤에만 여기로 온다.
    CANCELED = "CANCELED"
    #: 승인 거절, 또는 승인은 됐지만 좌석을 줄 수 없어 환불한 경우.
    FAILED = "FAILED"


#: 허용된 전이.
#:
#: PENDING 에서만 갈라진다. PAID → CANCELED 는 취소이고, 그 밖의 전이는 없다 —
#: 특히 PAID → FAILED 나 CANCELED → 무엇도 없다. 확정된 주문의 결말은 취소뿐이고,
#: 취소된 주문은 종점이다.
ALLOWED: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset(
        {OrderStatus.PAID, OrderStatus.FAILED, OrderStatus.CANCELED}
    ),
    OrderStatus.PAID: frozenset({OrderStatus.CANCELED}),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.FAILED: frozenset(),
}

#: 리컨실러가 되물어야 하는 상태. 하나뿐이지만 집합으로 두는 것은,
#: "결론이 없는 상태"가 늘어날 때 훑는 쪽이 아니라 여기가 바뀌게 하려는 것이다.
UNRESOLVED = frozenset({OrderStatus.PENDING})


class IllegalTransition(Exception):
    """전이 표에 없는 전이."""

    def __init__(self, src: OrderStatus, dst: OrderStatus) -> None:
        super().__init__(f"{src} → {dst} 는 허용되지 않는 주문 전이입니다")
        self.src, self.dst = src, dst


def can_transition(src: OrderStatus, dst: OrderStatus) -> bool:
    """전이 표에 있는 전이인가.

    >>> can_transition(OrderStatus.PENDING, OrderStatus.PAID)
    True
    >>> can_transition(OrderStatus.PAID, OrderStatus.CANCELED)
    True
    >>> can_transition(OrderStatus.CANCELED, OrderStatus.PAID)    # 취소는 종점
    False
    >>> can_transition(OrderStatus.PAID, OrderStatus.FAILED)      # 확정 후 실패는 없다
    False
    """
    return dst in ALLOWED[src]


def assert_transition(src: OrderStatus, dst: OrderStatus) -> None:
    if not can_transition(src, dst):
        raise IllegalTransition(src, dst)


def is_terminal(status: OrderStatus) -> bool:
    """더 갈 곳이 없는 상태인가.

    >>> is_terminal(OrderStatus.CANCELED)
    True
    >>> is_terminal(OrderStatus.PAID)      # 취소가 남아 있다
    False
    """
    return not ALLOWED[status]
