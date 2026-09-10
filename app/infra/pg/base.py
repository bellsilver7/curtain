"""결제사 어댑터 인터페이스 — 설계 문서: 결제 사가

메서드가 셋인 것이 이 인터페이스의 요점이다.

  approve()   승인 요청. 결과는 승인·거절·무응답 셋 중 하나다.
  inquire()   되묻기. 무응답에서 결론을 얻는 유일한 방법이다.
  refund()    환불. 좌석을 돌려놓기 전에 반드시 성공을 확인한다.

inquire() 없이 approve() 만 있는 어댑터는 이 설계에 쓸 수 없다. 무응답을 결론으로
바꿀 방법이 없으면 리컨실러가 존재할 수 없고, 그러면 "돈은 나갔는데 좌석이 없다"를
막을 수단이 사라진다 (원칙 "외부 호출은 리컨실러가 받친다").

무응답은 예외가 아니라 반환값이다. 예외로 만들면 호출부가 `except` 한 덩어리에서
거절과 무응답을 같이 처리하게 되고, 그 순간 무응답이 실패로 접힌다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.payment import PayResult


@dataclass(frozen=True, slots=True)
class PayAttempt:
    """한 번의 시도 결과 — 어댑터의 결과 DTO.

    pg_tid 는 결과가 APPROVED 일 때만 의미가 있다. 그것이 웹훅 중복 판정 키이고
    환불 요청의 대상이다 (멱등성 세 겹).
    """

    result: PayResult
    pg_tid: str | None = None

    @property
    def approved(self) -> bool:
        return self.result is PayResult.APPROVED


class PaymentGateway(Protocol):
    """PG사 어댑터. 실제 구현과 FakePG 가 같은 모양을 갖는다."""

    async def approve(self, *, order_id: int, amount: int) -> PayAttempt:
        """승인 요청. 무응답이면 PayResult.UNKNOWN 을 돌려준다."""
        ...

    async def inquire(self, *, order_id: int) -> PayAttempt:
        """주문번호로 실제 승인 여부를 되묻는다.

        여기서도 UNKNOWN 이 나올 수 있다 (PG사 자체 장애). 그때는 결론을 내리지
        말고 다음 tick 에 다시 묻는다 — 모르는 채로 취소하는 것이 가장 나쁘다.
        """
        ...

    async def refund(self, *, pg_tid: str, amount: int) -> PayAttempt:
        """환불. 성공하면 환불 거래번호를 담아 돌려준다."""
        ...
