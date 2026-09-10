"""FakePG — 설계 문서: 검증 시나리오

실제 PG사보다 이것이 테스트에 쓸모 있다. 실제 결제사 샌드박스는 대체로 성공만
잘 재현하는데, 이 프로젝트가 검증해야 하는 것은 실패와 무응답이다.

핵심 옵션은 lose_response 다.

    FakePG(lose_response=True)

    approve()  → UNKNOWN   (응답이 유실됐다)
    inquire()  → APPROVED  (실제로는 승인됐다)

"승인은 됐고 응답만 유실"은 이 시스템에서 가장 비싼 상태다. 사용자는 실패 화면을
보고 있고, 돈은 나갔고, 좌석은 아직 아무 것도 아니다. 이 조합을 재현할 수 없으면
리컨실러가 실제로 도는지 증명할 방법이 없다.

FakePG 는 상태를 갖는다 — approve() 로 승인한 주문을 기억하고 inquire() 가 그것을
답한다. 무상태로 만들면 "approve 는 UNKNOWN, inquire 는 APPROVED"를 옵션 조합으로
흉내내야 하고, 그러면 리컨실러가 진짜 되묻는지 알 수 없다.
"""

from __future__ import annotations

import asyncio
import itertools

from app.domain.payment import PayResult
from app.infra.pg.base import PayAttempt


class FakePG:
    """주입 가능한 결제사.

    approve_outcome    승인 요청에 돌려줄 결과
    lose_response      승인은 하고 응답만 유실한다 (approve_outcome 을 덮어쓴다)
    refund_outcome     환불 요청에 돌려줄 결과
    latency_sec        응답 전 지연. hold 만료 창을 재현하는 데 쓴다
    """

    def __init__(
        self,
        *,
        approve_outcome: PayResult = PayResult.APPROVED,
        lose_response: bool = False,
        inquire_outcome: PayResult | None = None,
        refund_outcome: PayResult = PayResult.APPROVED,
        latency_sec: float = 0.0,
    ) -> None:
        self.approve_outcome = approve_outcome
        self.lose_response = lose_response
        #: 되묻기 결과를 강제한다. None 이면 장부를 본다.
        #: UNKNOWN 을 주면 "PG사도 지금은 답을 못 한다"가 되고, 그때 리컨실러가
        #: 결론을 내지 않고 다음 tick 으로 넘기는지 볼 수 있다.
        self.inquire_outcome = inquire_outcome
        self.refund_outcome = refund_outcome
        self.latency_sec = latency_sec

        #: PG사 장부. order_id → 거래번호. inquire() 가 보는 곳이다.
        self.ledger: dict[int, str] = {}
        #: 호출 횟수. 테스트가 "리컨실러가 실제로 되물었는가"를 볼 수 있게 남긴다.
        self.calls: dict[str, int] = {"approve": 0, "inquire": 0, "refund": 0}

        self._serial = itertools.count(1)

    def _tid(self, prefix: str) -> str:
        return f"{prefix}-{next(self._serial):06d}"

    async def _wait(self) -> None:
        if self.latency_sec:
            await asyncio.sleep(self.latency_sec)

    async def approve(self, *, order_id: int, amount: int) -> PayAttempt:
        """승인 요청.

        lose_response 면 장부에는 기록하고 UNKNOWN 을 돌려준다 — 실제 결제사에서
        타임아웃이 났을 때 벌어지는 일이 정확히 이것이다.
        """
        self.calls["approve"] += 1
        await self._wait()

        if self.lose_response:
            self.ledger[order_id] = self._tid("tid")
            return PayAttempt(PayResult.UNKNOWN)

        if self.approve_outcome is PayResult.APPROVED:
            tid = self.ledger.setdefault(order_id, self._tid("tid"))
            return PayAttempt(PayResult.APPROVED, tid)

        return PayAttempt(self.approve_outcome)

    async def inquire(self, *, order_id: int) -> PayAttempt:
        """되묻기. 장부에 있으면 승인, 없으면 거절이다.

        inquire_outcome 을 주면 장부를 무시하고 그것을 돌려준다. UNKNOWN 을 주는
        경로가 특히 중요하다 — PG사 자체 장애로 되물어도 모를 때, 리컨실러가
        결론을 내지 않고 다음 tick 으로 넘기는지 확인할 수 있어야 한다.
        모르는 채로 취소하는 것이 이 도메인에서 가장 나쁘다.
        """
        self.calls["inquire"] += 1
        await self._wait()
        if self.inquire_outcome is not None:
            return PayAttempt(self.inquire_outcome)
        tid = self.ledger.get(order_id)
        if tid is None:
            return PayAttempt(PayResult.DECLINED)
        return PayAttempt(PayResult.APPROVED, tid)

    async def refund(self, *, pg_tid: str, amount: int) -> PayAttempt:
        """환불. 성공하면 환불 거래번호를 따로 발급한다.

        승인 거래번호를 재사용하지 않는 이유는 payments.pg_tid 가 UNIQUE 라서다 —
        같은 번호로 환불 행을 넣으면 제약 위반이 된다. 실제 PG사도 환불에는
        별도 거래번호를 준다.
        """
        self.calls["refund"] += 1
        await self._wait()
        if self.refund_outcome is PayResult.APPROVED:
            return PayAttempt(PayResult.APPROVED, self._tid("rfnd"))
        return PayAttempt(self.refund_outcome)
