"""좌석맵 조회와 캐시 — 아직 구현되지 않은 동작의 명세

이번에는 일곱 건 전부 빨강으로 시작한다. 게이트 작업 때는 hold_service.acquire()
가 이미 있어서 함정 테스트가 초록일 수 있었지만, 좌석맵 서비스는 아예 없다.
그래서 "구현 전 초록"인 함정은 없고, 대신 각 테스트가 무엇을 요구하는지
실패 메시지로 말해준다.

기대하는 표면은 아래 _service() 가 설명한다. 최소한으로 잡았다 — 캐시 키 모양,
직렬화 형식, Redis 명령 선택은 전부 구현이 정하면 된다.

핵심 요구 두 개가 서로 긴장 관계다.

  조회는 DB 에 가면 안 된다   요청 수가 선점보다 압도적으로 많다. 매번 DB 를
                              때리면 선점 트랜잭션이 쓸 커넥션이 남지 않는다.
  그래도 수렴해야 한다        3초의 stale 은 의도적으로 수용하지만, 무한정
                              낡은 값을 주면 좌석맵이 거짓말이 된다.

좌석맵은 힌트이고 진짜 판정은 선점 API 가 한다. 그래서 stale 을 허용할 수 있고,
허용하는 대신 상한을 테스트로 고정한다.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import pytest

from app.domain import policy
from app.infra.db.engine import tx
from app.service import hold_service
from tests.conftest import Seeded, SqlSpy

pytestmark = pytest.mark.integration

DEAD_REDIS_URL = "redis://127.0.0.1:1/0"

#: SEATMAP 쿼리를 SqlSpy 에서 식별하는 조각. 다른 쿼리에는 없다.
SEATMAP_SQL = "JOIN seats AS s ON s.id = ss.seat_id"


def _service() -> Any:
    """app.service.seatmap_service 를 가져온다. 없으면 기대 표면을 알려준다."""
    try:
        from app.service import seatmap_service
    except ImportError as exc:  # pragma: no cover - 구현 전 경로
        pytest.fail(
            "app/service/seatmap_service.py 가 필요하다.\n"
            "\n"
            "기대하는 표면 (이름만 맞으면 내부는 자유):\n"
            "\n"
            "    async def get(engine, *, schedule_id: int) -> Seatmap\n"
            "\n"
            "    class SeatCell:   seat_id, label, grade, price, status\n"
            "    class Seatmap:    schedule_id, etag, seats, from_cache\n"
            "    (결과 DTO 는 app/service/dto.py 에 둔다)\n"
            "\n"
            "  - label 은 '1층 C열 12번' 형식 (zone/row_label/col_no 조합)\n"
            "  - from_cache 는 관측용 불리언. 캐시에서 answered 되었는가\n"
            "  - etag 는 같은 내용이면 같은 값이어야 한다\n"
            "  - 캐시 TTL 은 policy.SEATMAP_CACHE_TTL 을 쓴다\n"
            "  - 무효화 API 는 만들지 않는다. staleness 는 TTL 로만 수렴한다\n"
            f"\n원래 오류: {exc}"
        )
    return seatmap_service


async def _get(engine: Any, schedule_id: int) -> Any:
    return await _service().get(engine, schedule_id=schedule_id)


# ─────────────────────────────────────────────── 내용이 맞는가


async def test_seatmap_returns_every_seat_with_label(engine, seeded: Seeded) -> None:
    """1,200석 전부, 사람이 읽을 라벨과 등급·가격·상태를 담아 돌려준다.

    라벨을 서버가 만드는 이유: 좌석 번호 규칙이 공연장마다 다르고, 클라이언트
    세 곳에서 각자 조립하면 세 곳이 다르게 틀린다.
    """
    seatmap = await _get(engine, seeded.schedule_id)

    assert seatmap.schedule_id == seeded.schedule_id
    assert len(seatmap.seats) == 1200, f"{len(seatmap.seats)}석이 돌아왔다"

    by_grade = Counter(s.grade for s in seatmap.seats)
    assert dict(by_grade) == {"VIP": 120, "R": 380, "S": 700}, dict(by_grade)

    prices = {s.grade: s.price for s in seatmap.seats}
    assert prices == {"VIP": 170_000, "R": 140_000, "S": 110_000}, prices

    assert {s.status for s in seatmap.seats} == {"AVAILABLE"}

    first = min(seatmap.seats, key=lambda s: s.seat_id)
    assert first.label == "1층 A열 1번", f"라벨 형식이 다르다: {first.label!r}"


async def test_seatmap_is_keyed_per_schedule(engine, seeded: Seeded) -> None:
    """회차마다 다른 좌석맵을 돌려준다.

    캐시 키에 회차가 빠지면 두 번째 회차가 첫 번째의 좌석맵을 받는다.
    조용히 틀리는 종류의 버그라 명시적으로 본다.
    """
    a, b = seeded.schedule_ids[0], seeded.schedule_ids[1]

    async with tx(engine) as conn:
        await hold_service.acquire(
            conn, schedule_id=a, user_id=seeded.user_ids[0], seat_ids=[seeded.seat_ids[0]]
        )

    map_a = await _get(engine, a)
    map_b = await _get(engine, b)

    assert map_a.schedule_id == a and map_b.schedule_id == b
    assert Counter(s.status for s in map_a.seats)["HELD"] == 1, "회차 A 의 선점이 안 보인다"
    assert Counter(s.status for s in map_b.seats)["HELD"] == 0, (
        "회차 B 가 회차 A 의 좌석맵을 받았다 — 캐시 키에 회차가 빠졌다"
    )
    assert map_a.etag != map_b.etag


# ─────────────────────────────────────────────── 부하를 줄이는가 (드라이버)


async def test_second_read_does_not_touch_db(
    engine, seeded: Seeded, sql_spy: SqlSpy
) -> None:
    """두 번째 조회는 DB 에 가지 않는다.

    이게 캐시의 존재 이유 전체다. 좌석맵은 선점보다 요청이 수십 배 많으므로,
    매번 1,200행을 읽으면 선점 트랜잭션이 쓸 커넥션이 남지 않는다.
    """
    sql_spy.reset()

    first = await _get(engine, seeded.schedule_id)
    second = await _get(engine, seeded.schedule_id)

    assert first.from_cache is False, "첫 조회가 캐시에서 나왔다고 표시된다"
    assert second.from_cache is True, "두 번째 조회가 캐시를 쓰지 않았다"
    assert [s.seat_id for s in first.seats] == [s.seat_id for s in second.seats]
    assert first.etag == second.etag

    reads = sql_spy.count(SEATMAP_SQL)
    assert reads == 1, (
        f"좌석맵 쿼리가 {reads}회 실행됐다 (기대 1회). 두 번째 조회가 캐시를 "
        f"거치지 않았다는 뜻이다.\n{sql_spy.dump()}"
    )


async def test_concurrent_cold_reads_hit_db_once(
    engine, seeded: Seeded, sql_spy: SqlSpy
) -> None:
    """캐시가 비어 있을 때 200 동시 조회 → DB 조회는 소수여야 한다.

    캐시 스탬피드. 오픈 직전에 모두가 새로고침하는 순간이 정확히 이 상황이고,
    나이브한 캐시는 여기서 200번 DB 를 때린다. 캐시가 없을 때와 똑같아진다.

    상한을 1 이 아니라 여유 있게 두는 것은, 완벽한 단일 비행(single flight)까지
    요구하지 않기 때문이다. 다만 200 요청이 200 조회가 되면 캐시가 이 경로에서
    아무 일도 하지 않는 것이다.
    """
    sql_spy.reset()
    n = 200

    maps = await asyncio.gather(*(_get(engine, seeded.schedule_id) for _ in range(n)))

    assert all(len(m.seats) == 1200 for m in maps), "일부 응답이 불완전하다"
    assert len({m.etag for m in maps}) == 1, "같은 시점 조회인데 etag 가 갈렸다"

    reads = sql_spy.count(SEATMAP_SQL)
    assert reads <= 5, (
        f"콜드 캐시에 200 동시 조회를 던졌더니 좌석맵 쿼리가 {reads}회 실행됐다 "
        f"(기대 5회 이하). 캐시 스탬피드다 — 첫 조회가 끝나기 전에 도착한 요청들이 "
        f"각자 DB 로 갔다.\n{sql_spy.dump()}"
    )


# ─────────────────────────────────────────────── 그래도 수렴하는가


async def test_stale_window_is_bounded_by_ttl(engine, seeded: Seeded) -> None:
    """캐시된 좌석맵은 TTL 안에 새 상태로 수렴한다.

    3초의 stale 은 의도적으로 수용한다 — 좌석맵은 힌트고 진짜 판정은 선점
    API 가 한다. 수용하는 대신 상한이 있어야 하고, 이 테스트가 그 상한이다.

    수용한다는 것은 "선점 직후 조회가 아직 AVAILABLE 로 보여도 실패가 아니다"
    라는 뜻이다. 그래서 직후 상태는 단정하지 않고 수렴만 본다.
    """
    ttl = policy.SEATMAP_CACHE_TTL.total_seconds()
    assert ttl <= 5, f"이 테스트는 짧은 TTL 을 전제한다 (현재 {ttl}s)"

    await _get(engine, seeded.schedule_id)  # 캐시를 채운다

    async with tx(engine) as conn:
        await hold_service.acquire(
            conn,
            schedule_id=seeded.schedule_id,
            user_id=seeded.user_ids[0],
            seat_ids=[seeded.seat_ids[0]],
        )

    await asyncio.sleep(ttl + 0.5)
    after = await _get(engine, seeded.schedule_id)

    held = [s for s in after.seats if s.status == "HELD"]
    assert len(held) == 1, (
        f"TTL({ttl}s) + 0.5s 가 지났는데도 좌석맵이 낡은 값을 준다 "
        f"(HELD {len(held)}석). 캐시가 만료되지 않는다는 뜻이다."
    )
    assert held[0].seat_id == seeded.seat_ids[0]


async def test_etag_changes_when_map_changes(engine, seeded: Seeded) -> None:
    """내용이 같으면 같은 etag, 달라지면 다른 etag.

    etag 의 값어치는 전부 이 성질에 있다. 매번 달라지면 304 가 안 나가고,
    안 달라지면 클라이언트가 낡은 좌석맵에 갇힌다.
    """
    before = await _get(engine, seeded.schedule_id)
    again = await _get(engine, seeded.schedule_id)
    assert before.etag == again.etag, "내용이 같은데 etag 가 달라졌다"

    async with tx(engine) as conn:
        await hold_service.acquire(
            conn,
            schedule_id=seeded.schedule_id,
            user_id=seeded.user_ids[0],
            seat_ids=[seeded.seat_ids[0]],
        )
    await asyncio.sleep(policy.SEATMAP_CACHE_TTL.total_seconds() + 0.5)

    after = await _get(engine, seeded.schedule_id)
    assert after.etag != before.etag, "좌석 상태가 바뀌었는데 etag 가 같다"


# ─────────────────────────────────────────────── Redis 가 없을 때


async def test_serves_correctly_without_redis(
    engine, seeded: Seeded, monkeypatch, sql_spy: SqlSpy
) -> None:
    """Redis 가 없어도 좌석맵은 정확히 나온다 (fail-open).

    캐시는 최적화이지 정합성이 아니다. 게이트와 같은 원칙이다 — Redis 장애가
    조회 중단이 되면 안 된다. 대신 매 요청이 DB 로 가므로 느려진다.

    죽은 포트를 가리켜 실제 connection refused 경로를 탄다.
    """
    monkeypatch.setenv("REDIS_URL", DEAD_REDIS_URL)
    sql_spy.reset()

    first = await _get(engine, seeded.schedule_id)
    second = await _get(engine, seeded.schedule_id)

    assert len(first.seats) == 1200
    assert [s.seat_id for s in first.seats] == [s.seat_id for s in second.seats]
    assert first.etag == second.etag, "같은 내용인데 etag 가 갈렸다"
    assert second.from_cache is False, "Redis 가 없는데 캐시에서 나왔다고 한다"

    reads = sql_spy.count(SEATMAP_SQL)
    assert reads == 2, (
        f"Redis 없이 두 번 조회했는데 좌석맵 쿼리가 {reads}회다 (기대 2회). "
        f"캐시 실패를 조회 실패로 바꾸고 있거나, 예외를 삼키고 빈 결과를 주고 있다."
    )
