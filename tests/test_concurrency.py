"""동시성 · 재고 정합성 — 설계 문서: 검증 시나리오

이 파일의 통과 로그가 2주차의 진짜 산출물이다. 기능이 도는 것보다
"동시에 던져도 틀리지 않는다"가 증명되는 게 이 프로젝트의 목적이다.

여기 있는 테스트 이름은 설계 문서의 검증 시나리오 표와 1:1로 대응한다.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest
import sqlalchemy as sa
from asyncpg.exceptions import DeadlockDetectedError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain import policy
from app.infra.db import queries
from app.infra.db.engine import tx
from app.service import hold_service
from app.service.hold_service import HoldRejected, QuotaExceeded
from tests.conftest import CONCURRENCY, Seeded

pytestmark = pytest.mark.integration


# ─────────────────────────────────────────────────────────────── 헬퍼


async def _try_hold(
    engine: AsyncEngine, *, schedule_id: int, user_id: int, seat_ids: list[int], **kw: object
) -> str:
    """선점을 한 번 시도하고 결과를 문자열로 분류한다.

    예외를 삼키지 않고 분류만 하는 이유: 200개 요청 중 몇 개가 어떤 이유로
    실패했는지가 이 테스트의 측정 대상이기 때문이다.
    """
    try:
        async with tx(engine) as conn:
            await hold_service.acquire(
                conn,
                schedule_id=schedule_id,
                user_id=user_id,
                seat_ids=seat_ids,
                **kw,  # type: ignore[arg-type]
            )
        return "ok"
    except QuotaExceeded:
        return "quota"
    except HoldRejected:
        return "taken"
    except DBAPIError as exc:
        if isinstance(exc.orig, DeadlockDetectedError):
            return "deadlock"
        raise


async def _status_counts(engine: AsyncEngine, schedule_id: int) -> dict[str, int]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(queries.SEAT_STATUS_COUNTS, {"schedule_id": schedule_id})
        ).mappings()
        return {r["status"]: r["n"] for r in rows}


# ─────────────────────────────────────────────────────── 검증 시나리오


async def test_single_seat_contention(engine: AsyncEngine, seeded: Seeded) -> None:
    """같은 좌석 1석에 동시 200 요청 → 성공 정확히 1건, 나머지 전부 409.

    합격 기준: DB 에 해당 좌석 HELD 행이 정확히 1개.
    """
    seat = seeded.seat_ids[0]

    results = Counter(
        await asyncio.gather(
            *(
                _try_hold(
                    engine,
                    schedule_id=seeded.schedule_id,
                    user_id=seeded.user_ids[i],
                    seat_ids=[seat],
                )
                for i in range(CONCURRENCY)
            )
        )
    )

    assert results["deadlock"] == 0, f"데드락 발생: {results}"
    assert results["ok"] == 1, f"성공이 1건이 아님: {results}"
    assert results["taken"] == CONCURRENCY - 1, f"거절 수가 안 맞음: {results}"

    counts = await _status_counts(engine, seeded.schedule_id)
    assert counts.get("HELD") == 1, counts
    assert counts.get("AVAILABLE") == 1199, counts


async def test_no_partial_success(engine: AsyncEngine, seeded: Seeded) -> None:
    """4석 요청 중 1석을 다른 유저가 먼저 선점 → 요청 전체 실패.

    합격 기준: 나머지 3석은 AVAILABLE 유지. 쓰레기 hold 0건 (원칙 "부분 성공은 없다").
    """
    a, b, c, d = seeded.seat_ids[:4]

    # 다른 유저가 c 한 석을 먼저 잡는다.
    async with tx(engine) as conn:
        await hold_service.acquire(
            conn, schedule_id=seeded.schedule_id, user_id=seeded.user_ids[0], seat_ids=[c]
        )

    with pytest.raises(HoldRejected) as exc:
        async with tx(engine) as conn:
            await hold_service.acquire(
                conn,
                schedule_id=seeded.schedule_id,
                user_id=seeded.user_ids[1],
                seat_ids=[a, b, c, d],
            )
    assert exc.value.unavailable_seat_ids == [c]

    # a, b, d 는 손대지 않은 상태여야 한다.
    counts = await _status_counts(engine, seeded.schedule_id)
    assert counts.get("HELD") == 1, f"쓰레기 hold 가 남았다: {counts}"


async def test_cross_seat_deadlock(engine: AsyncEngine, seeded: Seeded) -> None:
    """[A,B] 와 [B,A] 를 동시에 → 데드락 0건.

    발생하면 `ORDER BY seat_id FOR UPDATE` 잠금 순서 가정이 깨진 것이고,
    `SELECT ... FOR UPDATE` 를 별도 문장으로 분리해야 한다.
    설계 문서가 "문서를 믿지 말고 직접 확인하라"고 지목한 바로 그 지점이다.
    """
    pairs = 120
    tasks = []
    for i in range(pairs):
        a, b = seeded.seat_ids[2 * i], seeded.seat_ids[2 * i + 1]
        u1, u2 = seeded.user_ids[i % len(seeded.user_ids)], seeded.user_ids[-(i + 1)]
        # 같은 두 좌석을 서로 반대 순서로 요청한다.
        tasks.append(
            _try_hold(engine, schedule_id=seeded.schedule_id, user_id=u1, seat_ids=[a, b])
        )
        tasks.append(
            _try_hold(engine, schedule_id=seeded.schedule_id, user_id=u2, seat_ids=[b, a])
        )

    results = Counter(await asyncio.gather(*tasks))

    assert results["deadlock"] == 0, f"데드락 발생 — 잠금 순서 가정이 깨졌다: {results}"
    # 각 쌍에서 정확히 한쪽만 성공해야 한다.
    assert results["ok"] == pairs, f"쌍마다 1건씩 성공해야 함: {results}"

    counts = await _status_counts(engine, seeded.schedule_id)
    assert counts.get("HELD") == pairs * 2, counts


async def test_expired_hold_is_reclaimed_without_worker(
    engine: AsyncEngine, seeded: Seeded
) -> None:
    """만료된 hold 는 스윕 워커를 기다리지 않고 선점 쿼리가 즉시 회수한다.

    워커는 1s tick 이므로 그 사이에도 좌석은 팔릴 수 있어야 한다.
    """
    seat = seeded.seat_ids[0]

    async with tx(engine) as conn:
        await hold_service.acquire(
            conn,
            schedule_id=seeded.schedule_id,
            user_id=seeded.user_ids[0],
            seat_ids=[seat],
            hold_ttl_sec=1,
        )
    await asyncio.sleep(1.2)

    async with tx(engine) as conn:
        hold = await hold_service.acquire(
            conn, schedule_id=seeded.schedule_id, user_id=seeded.user_ids[1], seat_ids=[seat]
        )
    assert hold.seats[0].seat_id == seat
    assert (await _status_counts(engine, seeded.schedule_id)).get("HELD") == 1


async def test_sweeper_reclaims_expired_holds(engine: AsyncEngine, seeded: Seeded) -> None:
    """hold_sweeper 가 만료분을 AVAILABLE 로 되돌린다.

    클라이언트가 브라우저를 닫아버린 경우의 유일한 회수 수단.
    """
    seats = list(seeded.seat_ids[:3])
    for i, s in enumerate(seats):
        async with tx(engine) as conn:
            await hold_service.acquire(
                conn,
                schedule_id=seeded.schedule_id,
                user_id=seeded.user_ids[i],
                seat_ids=[s],
                hold_ttl_sec=1,
            )
    assert (await _status_counts(engine, seeded.schedule_id)).get("HELD") == 3
    await asyncio.sleep(1.2)

    async with tx(engine) as conn:
        reclaimed = await hold_service.sweep_expired(conn)

    assert sorted(seat for _, seat in reclaimed) == sorted(seats)
    counts = await _status_counts(engine, seeded.schedule_id)
    assert counts.get("AVAILABLE") == 1200, counts
    assert "HELD" not in counts, counts


async def test_quota_is_enforced_under_concurrency(
    engine: AsyncEngine, seeded: Seeded
) -> None:
    """한 유저가 1석씩 동시에 여러 번 요청해도 회차당 4매를 넘지 못한다.

    한도 검사를 애플리케이션에서 하면 "세는 시점과 쓰는 시점" 사이에 끼어들 수
    있으므로 SQL 안에서 판정한다. 이 테스트가 그걸 지킨다.
    """
    user = seeded.user_ids[0]
    attempts = 12
    results = Counter(
        await asyncio.gather(
            *(
                _try_hold(
                    engine,
                    schedule_id=seeded.schedule_id,
                    user_id=user,
                    seat_ids=[seeded.seat_ids[i]],
                )
                for i in range(attempts)
            )
        )
    )

    assert results["deadlock"] == 0, results
    assert results["ok"] == policy.MAX_SEATS_PER_ORDER, f"한도가 안 지켜졌다: {results}"
    counts = await _status_counts(engine, seeded.schedule_id)
    assert counts.get("HELD") == policy.MAX_SEATS_PER_ORDER, counts


async def test_seat_count_invariant(engine: AsyncEngine, seeded: Seeded) -> None:
    """총량 보존: AVAILABLE + HELD + BOOKED = 1200 × 회차수.

    개별 테스트가 다 통과해도 이 합이 안 맞으면 좌석 행이 새고 있다는 뜻이고,
    그건 위의 어떤 실패보다 심각한 신호다.
    """
    # 재고를 한 번 흔들어 놓고 확인한다.
    await asyncio.gather(
        *(
            _try_hold(
                engine,
                schedule_id=seeded.schedule_id,
                user_id=seeded.user_ids[i],
                seat_ids=[seeded.seat_ids[i % 40]],
            )
            for i in range(60)
        )
    )

    async with engine.connect() as conn:
        total = (
            await conn.execute(sa.text("SELECT count(*) FROM schedule_seats"))
        ).scalar_one()
    assert total == 1200 * len(seeded.schedule_ids)

    for sid in seeded.schedule_ids:
        counts = await _status_counts(engine, sid)
        assert sum(counts.values()) == 1200, f"회차 {sid}: {counts}"


async def test_expand_schedule_seats_is_idempotent(
    engine: AsyncEngine, seeded: Seeded
) -> None:
    """회차 재고 전개를 두 번 해도 재고가 늘어나지 않는다.

    uq_schedule_seat + ON CONFLICT DO NOTHING 이 받아낸다. 회차 오픈 처리가
    재시도되는 것은 정상 운영이므로 멱등해야 한다.
    """
    from app.service import schedule_service

    async with engine.begin() as conn:
        again = await schedule_service.expand_schedule_seats(
            conn,
            schedule_id=seeded.schedule_id,
            venue_id=seeded.venue_id,
            layout=seeded.layout,
        )
    assert again == 0, f"두 번째 전개가 {again}행을 만들었다"
    counts = await _status_counts(engine, seeded.schedule_id)
    assert sum(counts.values()) == 1200, counts


async def test_layout_matches_design_assumptions(seeded: Seeded) -> None:
    """공연장 레이아웃이 설계 문서의 가정과 같은지 (VIP 120 / R 380 / S 700)."""
    assert seeded.layout.count_by_grade() == {"VIP": 120, "R": 380, "S": 700}
    assert seeded.layout.total_seats == 1200
