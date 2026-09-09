"""좌석 선점 유스케이스 — 설계 문서: 좌석 선점

정합성은 Postgres 만으로 완결된다. 조건부 UPDATE 하나가 전량 선점과
구매 한도를 판정하고, 유니크 제약이 오버부킹을 구조적으로 막는다. 이 성질을
먼저 테스트로 증명한 뒤에 Redis 게이트를 붙였다 — 순서를 뒤집으면
"게이트 덕분에 맞는 것"과 "DB 덕분에 맞는 것"을 구분할 수 없다.

게이트는 그 위에 얹힌 부하 방벽이다. 같은 좌석에 몰린 수백 요청을 DB 행
잠금까지 내려보내지 않고 걸러낸다. Redis 가 죽으면 게이트를 건너뛰고
(fail-open) 지연만 늘어난다 — 오버부킹은 나지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain import policy
from app.infra.db import queries
from app.infra.redis import client as gate_client


class HoldRejected(Exception):
    """선점 실패. 부분 성공은 없으므로 실패는 항상 전량 실패다 (원칙 "부분 성공은 없다")."""

    code = "SEAT_TAKEN"

    def __init__(self, unavailable_seat_ids: list[int], *, code: str | None = None) -> None:
        self.unavailable_seat_ids = unavailable_seat_ids
        if code:
            self.code = code
        super().__init__(f"{self.code}: 좌석 {unavailable_seat_ids} 선점 실패")


class QuotaExceeded(HoldRejected):
    """회차당 구매 한도 초과 (정책 상수)."""

    code = "QUOTA_EXCEEDED"


@dataclass(frozen=True, slots=True)
class HeldSeat:
    seat_id: int
    grade: str
    price: int


@dataclass(frozen=True, slots=True)
class Hold:
    schedule_id: int
    user_id: int
    seats: tuple[HeldSeat, ...]
    expires_at: datetime

    @property
    def total_amount(self) -> int:
        return sum(s.price for s in self.seats)


async def acquire(
    conn: AsyncConnection,
    *,
    schedule_id: int,
    user_id: int,
    seat_ids: list[int],
    hold_ttl_sec: int | None = None,
    max_seats: int = policy.MAX_SEATS_PER_ORDER,
) -> Hold:
    """좌석 전량 선점. 하나라도 못 잡으면 HoldRejected.

    호출자가 트랜잭션을 열어야 한다 (engine.tx()). 이 함수는 커밋하지 않는다 —
    선점과 주문 생성을 한 트랜잭션에 묶을 수 있어야 하기 때문이다.

    중복 seat_id 는 거부한다. 허용하면 cardinality(:seat_ids) 와 실제 잠근 행 수가
    달라져 guard 가 영원히 거짓이 되고, 원인을 찾기 어려운 100% 실패가 된다.
    """
    if not seat_ids:
        raise ValueError("seat_ids 가 비어 있습니다")
    if len(set(seat_ids)) != len(seat_ids):
        raise ValueError(f"seat_ids 에 중복이 있습니다: {seat_ids}")
    if len(seat_ids) > max_seats:
        raise QuotaExceeded(list(seat_ids))

    ttl_sec = (
        hold_ttl_sec
        if hold_ttl_sec is not None
        else int(policy.HOLD_TTL.total_seconds())
    )

    # 게이트를 DB 앞에 세운다. 실제 hold TTL 을 그대로 넘기는 것이 중요하다 —
    # 게이트가 원본보다 오래 남으면 DB 에서 풀린 좌석이 계속 막히는 유령 매진이 된다.
    #
    # keep() 을 부르지 않고 이 블록을 벗어나면 게이트는 자동 반납된다.
    # DB 가 거절했을 때 반납을 잊는 것이 유령 매진의 주된 원인이므로,
    # 반납이 기본 동작이고 유지가 명시적이다.
    async with gate_client.hold_gate(
        schedule_id=schedule_id,
        user_id=user_id,
        seat_ids=seat_ids,
        hold_ttl_sec=ttl_sec,
    ) as gate:
        if not gate.acquired:
            # 패자 경로. 여기서 DB 를 조회하면 게이트가 아껴준 왕복을 패자 수만큼
            # 되토해낸다 — 막힌 좌석은 게이트가 이미 알고 있으므로 그 값을 쓴다.
            raise HoldRejected(list(gate.blocked_seat_ids))

        # 구매 한도는 "몇 석 갖고 있나"라는 술어이고, 행 잠금으로는 지킬 수 없다.
        # 같은 (회차, 사용자)의 동시 요청만 직렬화한다 — LOCK_USER_QUOTA 주석 참고.
        # 별도 문장인 이유: 한 문장 안의 CTE 실행 순서는 보장되지 않으므로,
        # 락이 owned 절보다 먼저 잡힌다는 것을 SQL 로 표현할 방법이 없다.
        await conn.execute(
            queries.LOCK_USER_QUOTA,
            {"quota_key": f"curtain.hold:{schedule_id}:{user_id}"},
        )

        rows = (
            await conn.execute(
                queries.HOLD_SEATS,
                {
                    "schedule_id": schedule_id,
                    "user_id": user_id,
                    "seat_ids": list(seat_ids),
                    "max_seats": max_seats,
                    "hold_ttl_sec": ttl_sec,
                },
            )
        ).mappings().all()

        if not rows:
            # 게이트는 통과했는데 DB 가 거절한 경우다. 흔하지 않다 — 게이트 TTL 이
            # 먼저 만료됐거나 Redis 장애로 게이트를 건너뛴(degraded) 상황.
            # 패자 경로가 아니므로 여기서 한 번 더 조회해도 부하에 영향이 없다.
            unavailable = [
                r.seat_id
                for r in (
                    await conn.execute(
                        queries.FIND_UNAVAILABLE_SEATS,
                        {"schedule_id": schedule_id, "seat_ids": list(seat_ids)},
                    )
                ).mappings()
            ]
            if unavailable:
                raise HoldRejected(unavailable)
            # 좌석은 다 비어 있는데 0행 = 구매 한도 초과.
            raise QuotaExceeded([])

        # guard 가 통과했으면 전량이다. 그래도 확인한다 — 이 assert 가 깨지면
        # HOLD_SEATS 가 설계와 다르게 동작한다는 뜻이고, 조용히 넘어가면 안 된다.
        if len(rows) != len(seat_ids):
            raise AssertionError(
                f"부분 선점 발생: 요청 {len(seat_ids)}석 중 {len(rows)}석. "
                f"HOLD_SEATS 의 guard 절 확인 필요"
            )

        # 남은 창: 호출자의 트랜잭션이 이 뒤에 롤백되면 DB hold 는 사라지지만
        # 게이트는 남는다. 그 좌석은 게이트 TTL 동안 안 팔린다 — 정합성 문제는
        # 아니고(DB 가 진실이다) 판매 기회 손실이며, 상한은 hold TTL 이다.
        # 주문 생성까지 한 트랜잭션에 묶는 단계에서 커밋 훅으로 옮긴다.
        gate.keep()

        return Hold(
            schedule_id=schedule_id,
            user_id=user_id,
            seats=tuple(
                HeldSeat(seat_id=r.seat_id, grade=r.grade, price=r.price) for r in rows
            ),
            expires_at=rows[0].hold_expires_at,
        )


async def release(
    conn: AsyncConnection, *, schedule_id: int, user_id: int, seat_ids: list[int]
) -> list[int]:
    """본인 hold 해제. 반환값은 실제로 풀린 좌석 — 이미 만료된 것은 포함되지 않는다."""
    result = await conn.execute(
        queries.RELEASE_HOLD,
        {"schedule_id": schedule_id, "user_id": user_id, "seat_ids": list(seat_ids)},
    )
    released = [r.seat_id for r in result.mappings()]

    # 게이트도 지운다. 안 지우면 사용자가 좌석 선택을 되돌렸는데 원래 좌석이
    # 게이트 TTL 동안 막힌다 — 좌석 변경은 정상 흐름이므로 즉시 반영돼야 한다.
    if released:
        await gate_client.release_gate(
            schedule_id=schedule_id, user_id=user_id, seat_ids=released
        )
    return released


async def sweep_expired(conn: AsyncConnection, *, batch: int = 500) -> list[tuple[int, int]]:
    """만료 hold 회수. hold_sweeper 워커가 1s 마다 호출한다.

    반환값 (schedule_id, seat_id) 목록은 좌석맵 캐시 무효화 대상이다.
    """
    result = await conn.execute(queries.SWEEP_EXPIRED_HOLDS, {"batch": batch})
    return [(r.schedule_id, r.seat_id) for r in result.mappings()]
