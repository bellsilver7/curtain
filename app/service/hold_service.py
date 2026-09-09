"""좌석 선점 유스케이스 — 설계 문서 §5.2

이 단계에서는 **Postgres 만으로** 정확해야 한다. Redis 좌석 게이트(§5.3)는
아직 붙이지 않는다 — 원칙 01("정합성의 단일 진실은 PostgreSQL")이 참인지 먼저
증명해야, 나중에 붙는 게이트가 순수한 최적화임이 증명된다.
게이트를 먼저 붙이면 "게이트 덕분에 맞는 것"과 "DB 덕분에 맞는 것"을 구분할 수 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain import policy
from app.infra.db import queries


class HoldRejected(Exception):
    """선점 실패. 부분 성공은 없으므로 실패는 항상 전량 실패다 (원칙 02)."""

    code = "SEAT_TAKEN"

    def __init__(self, unavailable_seat_ids: list[int], *, code: str | None = None) -> None:
        self.unavailable_seat_ids = unavailable_seat_ids
        if code:
            self.code = code
        super().__init__(f"{self.code}: 좌석 {unavailable_seat_ids} 선점 실패")


class QuotaExceeded(HoldRejected):
    """회차당 구매 한도 초과 (§1.1)."""

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
    """좌석 전량 선점. 하나라도 못 잡으면 `HoldRejected`.

    호출자가 트랜잭션을 열어야 한다 (`engine.tx()`). 이 함수는 커밋하지 않는다 —
    선점과 주문 생성을 한 트랜잭션에 묶을 수 있어야 하기 때문이다.

    중복 seat_id 는 거부한다. 허용하면 `cardinality(:seat_ids)` 와 실제 잠근 행 수가
    달라져 guard 가 영원히 거짓이 되고, 원인을 찾기 어려운 100% 실패가 된다.
    """
    if not seat_ids:
        raise ValueError("seat_ids 가 비어 있습니다")
    if len(set(seat_ids)) != len(seat_ids):
        raise ValueError(f"seat_ids 에 중복이 있습니다: {seat_ids}")
    if len(seat_ids) > max_seats:
        raise QuotaExceeded(list(seat_ids))

    # 구매 한도는 "몇 석 갖고 있나"라는 술어이고, 행 잠금으로는 지킬 수 없다.
    # 같은 (회차, 사용자)의 동시 요청만 직렬화한다 — queries.LOCK_USER_QUOTA 의 주석 참고.
    # 별도 문장인 이유: 한 문장 안의 CTE 실행 순서는 보장되지 않으므로,
    # 락이 owned 절보다 먼저 잡힌다는 것을 SQL 로 표현할 방법이 없다.
    await conn.execute(
        queries.LOCK_USER_QUOTA, {"quota_key": f"curtain.hold:{schedule_id}:{user_id}"}
    )

    params = {
        "schedule_id": schedule_id,
        "user_id": user_id,
        "seat_ids": list(seat_ids),
        "max_seats": max_seats,
        "hold_ttl_sec": hold_ttl_sec
        if hold_ttl_sec is not None
        else int(policy.HOLD_TTL.total_seconds()),
    }
    rows = (await conn.execute(queries.HOLD_SEATS, params)).mappings().all()

    if not rows:
        # 0행의 원인은 둘 중 하나다 — 좌석을 못 잡았거나, 한도를 넘었거나.
        # 사용자에게 주는 메시지가 달라야 하므로 여기서 구분한다.
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
        raise QuotaExceeded([])

    # guard 가 통과했으면 전량이다. 그래도 확인한다 — 이 assert 가 깨지면
    # §5.2 의 SQL 이 설계와 다르게 동작한다는 뜻이고, 조용히 넘어가면 안 된다.
    if len(rows) != len(seat_ids):
        raise AssertionError(
            f"부분 선점 발생: 요청 {len(seat_ids)}석 중 {len(rows)}석. HOLD_SEATS 의 guard 절 확인 필요"
        )

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
    return [r.seat_id for r in result.mappings()]


async def sweep_expired(conn: AsyncConnection, *, batch: int = 500) -> list[tuple[int, int]]:
    """만료 hold 회수 (§5.4). `hold_sweeper` 워커가 1s 마다 호출한다.

    반환값 `(schedule_id, seat_id)` 목록은 좌석맵 캐시 무효화 대상이다.
    """
    result = await conn.execute(queries.SWEEP_EXPIRED_HOLDS, {"batch": batch})
    return [(r.schedule_id, r.seat_id) for r in result.mappings()]
