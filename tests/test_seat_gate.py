"""Redis 좌석 게이트 (설계 문서: Redis 좌석 게이트)

이 파일은 블랙박스 명세다. hold_service.acquire() / release() 의 겉보기
동작만 검증하고, Redis 클라이언트 모듈이나 Lua 스크립트를 직접 import 하지
않는다. 구현 방식(키 이름, 스크립트 개수, 클라이언트 구조)을 테스트가 미리
못박으면 리팩터링이 테스트를 깨게 되고, 그러면 테스트가 설계를 방해한다.

게이트가 증명해야 하는 것은 하나다 — 게이트는 정합성이 아니라 부하를 위한
것이다. Redis 를 통째로 날려도 오버부킹은 0건이어야 하고, 대신 Redis 가
살아 있을 때는 패자들이 DB 에 도달하지 않아야 한다.

다섯 건 중 test_gate_sheds_load_before_db 만이 게이트 없이는 통과하지 못하는
드라이버다. 나머지 넷은 Postgres 만으로도 초록이었고, 게이트를 붙이면서
망가뜨릴 수 있는 지점에 놓아둔 함정이다. 초록으로 시작해 초록으로 끝난 테스트는
아무것도 증명하지 않으므로, 구현 후 일부러 깨뜨려 실제로 빨강이 되는지 확인했다.
그 과정에서 이 파일의 초기 버전이 (2) 경로를 전혀 검증하지 않는 것을 발견했다 —
test_gate_released_when_db_rejects 의 주석 참고.
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter

import pytest
import redis.asyncio as aioredis

from app.domain import policy
from app.infra.db.engine import tx
from app.service import hold_service
from app.service.hold_service import HoldRejected, QuotaExceeded
from tests.conftest import CONCURRENCY, Seeded, SqlSpy, status_counts, try_hold

pytestmark = pytest.mark.integration

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:16379/0")

#: 아무도 듣지 않는 포트. 진짜 connection refused 를 타게 하려는 것이다.
#: 플래그로 게이트를 우회하면 "장애 경로"가 아니라 "분기 하나"를 테스트하게 된다.
DEAD_REDIS_URL = "redis://127.0.0.1:1/0"


# ─────────────────────────────────────────────────────────────── 드라이버


async def test_gate_sheds_load_before_db(
    engine, seeded: Seeded, sql_spy: SqlSpy
) -> None:
    """같은 좌석 200요청 중 DB 의 잠금 쿼리에 도달하는 것은 1건이어야 한다.

    이게 게이트의 존재 이유 전체다. 결과(성공 1건)는 게이트가 없어도 맞지만,
    그 결과에 도달하기까지 DB 가 견뎌야 하는 경합량이 200배 다르다.

    구현 힌트가 아니라 제약: 패자에게 unavailable_seat_ids 를 주기 위해
    DB 를 다시 조회하면 이 테스트는 통과하지 못한다. 어느 좌석이 막혔는지는
    게이트가 이미 알고 있으므로, 그 정보를 게이트에서 받아 와야 한다.
    """
    seat = seeded.seat_ids[0]
    sql_spy.reset()

    results = Counter(
        await asyncio.gather(
            *(
                try_hold(
                    engine,
                    schedule_id=seeded.schedule_id,
                    user_id=seeded.user_ids[i],
                    seat_ids=[seat],
                )
                for i in range(CONCURRENCY)
            )
        )
    )

    # 결과는 게이트가 있든 없든 같아야 한다.
    assert results["ok"] == 1, results
    assert results["deadlock"] == 0, results

    # 달라지는 것은 DB 가 본 부하다.
    locking = sql_spy.count("FOR UPDATE")
    touching = sql_spy.touching("schedule_seats")
    assert locking == 1, (
        f"잠금 쿼리가 {locking}회 실행됐다 (기대 1회). "
        f"패자들이 게이트에서 걸러지지 않고 DB 행 잠금까지 내려왔다는 뜻이다.\n"
        f"{sql_spy.dump()}"
    )
    assert touching <= 3, (
        f"schedule_seats 를 건드린 문장이 {touching}개다 (기대 3개 이하). "
        f"승자 한 명분만 남아야 한다 — 패자에게 응답을 만들려고 DB 를 다시 "
        f"조회하고 있는지 확인할 것.\n"
        f"{sql_spy.dump()}"
    )


# ─────────────────────────────────────────────────────────────── 함정


async def test_overbooking_survives_redis_flush(engine, seeded: Seeded) -> None:
    """선점 진행 중 FLUSHALL → 오버부킹 0건.

    원칙 "정합성의 단일 진실은 PostgreSQL"의 유일한 증명이다. 게이트 키가
    중간에 다 사라지면 여러 요청이 게이트를 통과하지만, 그래도 DB 행 잠금이
    한 명만 통과시킨다. p99 는 올라가도 이중 판매는 없어야 한다.

    이 테스트는 구현 전에도 통과한다(게이트가 없으니 지울 것도 없다).
    구현 후에도 통과해야 한다는 것이 요점이다.
    """
    seat = seeded.seat_ids[0]
    client = aioredis.from_url(REDIS_URL)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"Redis({REDIS_URL}) 에 붙을 수 없다. make up 으로 컨테이너를 띄울 것: {exc}"
        ) from exc

    async def _flush_repeatedly() -> None:
        # 선점이 도는 동안 게이트 키를 반복해서 지운다.
        for _ in range(20):
            await asyncio.sleep(0.01)
            await client.flushall()

    try:
        results, _ = await asyncio.gather(
            asyncio.gather(
                *(
                    try_hold(
                        engine,
                        schedule_id=seeded.schedule_id,
                        user_id=seeded.user_ids[i],
                        seat_ids=[seat],
                    )
                    for i in range(CONCURRENCY)
                )
            ),
            _flush_repeatedly(),
        )
    finally:
        await client.aclose()

    counted = Counter(results)
    assert counted["deadlock"] == 0, counted
    counts = await status_counts(engine, seeded.schedule_id)
    assert counts.get("HELD") == 1, (
        f"FLUSHALL 중에 좌석이 {counts.get('HELD')}명에게 잡혔다. "
        f"게이트가 정합성 경로에 들어가 있다는 뜻이다 — Redis 는 최적화 계층이어야 한다."
    )
    assert counted["ok"] == 1, counted


async def test_works_without_redis(engine, seeded: Seeded, monkeypatch) -> None:
    """Redis 가 아예 없어도 선점은 정확해야 한다 (fail-open).

    게이트를 우회하는 플래그가 아니라 죽은 포트를 가리켜서, 실제
    connection refused 경로를 타게 한다. 그래서 이 테스트가 통과하려면
    Redis 클라이언트가 호출 시점에 URL 을 읽거나 주입받는 구조여야 한다 —
    모듈 로드 시점에 커넥션을 만들어 두면 여기서 걸린다.

    대기열은 반대로 fail-closed 다. 같은 Redis 장애에 대응이 반대인 이유는
    하나가 정합성 밖에 있고 하나가 부하 방벽이기 때문이다.
    """
    monkeypatch.setenv("REDIS_URL", DEAD_REDIS_URL)
    seat = seeded.seat_ids[0]
    n = 30

    results = Counter(
        await asyncio.gather(
            *(
                try_hold(
                    engine,
                    schedule_id=seeded.schedule_id,
                    user_id=seeded.user_ids[i],
                    seat_ids=[seat],
                )
                for i in range(n)
            )
        )
    )

    assert results["deadlock"] == 0, results
    assert results["ok"] == 1, (
        f"Redis 없이 성공이 {results['ok']}건이다 (기대 1건). "
        f"게이트 실패를 선점 실패로 취급하고 있거나(fail-closed), "
        f"예외가 그대로 올라오고 있다."
    )
    assert (await status_counts(engine, seeded.schedule_id)).get("HELD") == 1


async def test_gate_released_when_db_rejects(engine, seeded: Seeded) -> None:
    """실패한 요청이 건드린 좌석은 즉시 다시 팔려야 한다 — 유령 매진 방지.

    반납이 필요한 경로가 두 가지이고, 서로 다른 장치가 책임진다. 둘 다 본다.

      (1) 게이트가 거절 — Lua 스크립트가 부분 획득을 스스로 되돌린다
      (2) 게이트는 통과했는데 DB 가 거절 — 서비스가 반납해야 한다.
          트랜잭션 롤백은 Redis 를 되돌려주지 않는다

    (2)를 빠뜨리기 쉽다. (1)만 검증하면 Lua 의 되돌리기를 시험하는 것이지
    서비스의 반납을 시험하는 것이 아니다 — 실제로 이 테스트의 첫 버전이 그랬고,
    반납 코드를 지워도 초록이었다.
    """
    a, b, c, d = seeded.seat_ids[:4]

    # ── (1) 게이트가 거절하는 경로 ──────────────────────────────
    async with tx(engine) as conn:
        await hold_service.acquire(
            conn, schedule_id=seeded.schedule_id, user_id=seeded.user_ids[0], seat_ids=[c]
        )

    with pytest.raises(HoldRejected):
        async with tx(engine) as conn:
            await hold_service.acquire(
                conn,
                schedule_id=seeded.schedule_id,
                user_id=seeded.user_ids[1],
                seat_ids=[a, b, c, d],
            )

    async with tx(engine) as conn:
        hold = await hold_service.acquire(
            conn,
            schedule_id=seeded.schedule_id,
            user_id=seeded.user_ids[2],
            seat_ids=[a, b, d],
        )
    assert {s.seat_id for s in hold.seats} == {a, b, d}, "게이트 거절 후 좌석이 막혀 있다"

    # ── (2) 게이트는 통과, DB 가 거절하는 경로 ────────────────────
    # 구매 한도를 채운 유저가 빈 좌석 하나를 더 요청한다. 그 좌석은 비어 있으므로
    # 게이트는 통과하고, DB 가 한도로 거절한다. 서비스가 반납하지 않으면 그
    # 좌석은 게이트 TTL 동안 아무도 못 잡는다.
    hoarder = seeded.user_ids[3]
    quota_seats = list(seeded.seat_ids[10 : 10 + policy.MAX_SEATS_PER_ORDER])
    async with tx(engine) as conn:
        await hold_service.acquire(
            conn,
            schedule_id=seeded.schedule_id,
            user_id=hoarder,
            seat_ids=quota_seats,
        )

    free_seat = seeded.seat_ids[20]
    with pytest.raises(QuotaExceeded):
        async with tx(engine) as conn:
            await hold_service.acquire(
                conn,
                schedule_id=seeded.schedule_id,
                user_id=hoarder,
                seat_ids=[free_seat],
            )

    async with tx(engine) as conn:
        hold = await hold_service.acquire(
            conn,
            schedule_id=seeded.schedule_id,
            user_id=seeded.user_ids[4],
            seat_ids=[free_seat],
        )
    assert hold.seats[0].seat_id == free_seat, (
        "한도 초과로 거절된 좌석이 게이트에 갇혀 있다 — "
        "DB 가 거절했을 때 게이트를 반납하지 않는다는 뜻이다"
    )


async def test_release_clears_gate(engine, seeded: Seeded) -> None:
    """release() 는 게이트도 지워야 한다.

    안 지우면 사용자가 좌석을 바꿨는데 원래 좌석이 게이트 TTL 동안 막힌다.
    좌석 선택을 되돌리는 것은 정상 흐름이므로 즉시 반영돼야 한다.
    """
    seat = seeded.seat_ids[0]

    async with tx(engine) as conn:
        await hold_service.acquire(
            conn, schedule_id=seeded.schedule_id, user_id=seeded.user_ids[0], seat_ids=[seat]
        )
    async with tx(engine) as conn:
        released = await hold_service.release(
            conn, schedule_id=seeded.schedule_id, user_id=seeded.user_ids[0], seat_ids=[seat]
        )
    assert released == [seat]

    async with tx(engine) as conn:
        hold = await hold_service.acquire(
            conn, schedule_id=seeded.schedule_id, user_id=seeded.user_ids[1], seat_ids=[seat]
        )
    assert hold.seats[0].seat_id == seat
