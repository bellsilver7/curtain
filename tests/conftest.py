"""테스트 픽스처 — 설계 문서: 검증 시나리오

integration 마크가 붙은 테스트는 docker-compose 의 postgres 를 요구한다.
make test-unit 은 그것들을 제외하고 도메인 테스트만 돌린다.

동시성 테스트는 커넥션을 많이 쓴다. 풀이 좁으면 "락 경합"이 아니라 "풀 대기"를
측정하게 되고, 좌석 선점 검증이 무의미해진다 — 그래서 테스트 엔진은 풀을 넓게 잡는다.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
import sqlalchemy as sa
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.infra.db.engine import build_engine
from app.infra.redis import client as gate_client
from app.service import schedule_service
from app.service.schedule_service import DEMO_HALL, VenueLayout

#: 동시성 테스트의 병렬도. 풀 크기는 이것보다 넉넉해야 한다.
CONCURRENCY = int(os.getenv("TEST_CONCURRENCY", "200"))

#: TRUNCATE 대상. FK 순서는 CASCADE 가 처리하지만, 목록에서 빠진 테이블이
#: 다음 테스트로 데이터를 흘리는 것은 CASCADE 가 막아주지 않는다.
_ALL_TABLES = (
    "order_items",
    "payments",
    "outbox",
    "orders",
    "schedule_seats",
    "schedules",
    "performances",
    "seats",
    "venues",
    "users",
)


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """테스트마다 새 엔진. pytest-asyncio 가 테스트별로 이벤트 루프를 새로 만들므로
    엔진을 재사용하면 다른 루프에 붙은 커넥션을 쓰게 된다."""
    eng = build_engine(
        pool_size=CONCURRENCY + 20,
        max_overflow=40,
        # 락 경합 테스트에서 statement_timeout 이 먼저 터지면 원인을 오해한다.
        connect_args={
            "server_settings": {
                "statement_timeout": "15000",
                "application_name": "curtain-tests",
            }
        },
    )
    try:
        yield eng
    finally:
        await eng.dispose()


#: SQL 주석(-- 부터 줄 끝까지)을 걷어낸다.
#: 이 프로젝트의 쿼리는 주석이 두껍고, 그 주석 안에 FOR UPDATE 같은 문구가
#: 설명으로 등장한다. 주석을 남겨두면 스파이가 SQL 이 아니라 산문을 센다.
_SQL_COMMENT = re.compile(r"--[^\n]*")


@dataclass(slots=True)
class SqlSpy:
    """실행된 SQL 문장을 기록한다. 게이트가 DB 부하를 실제로 덜어내는지 재는 도구.

    "게이트를 붙였다"는 주석은 근거가 아니다. 패자 199건이 DB 에 도달하지
    않는다는 것을 숫자로 보여야 한다.

    문자열 리터럴 안의 -- 는 구분하지 않는다. 지금 쿼리에는 그런 리터럴이
    없고, 생기면 이 스파이부터 고쳐야 한다.
    """

    statements: list[str]

    def reset(self) -> None:
        self.statements.clear()

    def record(self, statement: str) -> None:
        self.statements.append(_SQL_COMMENT.sub("", statement))

    def count(self, needle: str) -> int:
        """needle 을 포함한 문장 수. 주석은 이미 제거된 상태로 비교한다."""
        return sum(1 for s in self.statements if needle in s)

    def touching(self, table: str) -> int:
        return self.count(table)

    def dump(self, limit: int = 200) -> str:
        """실패 메시지에 붙일 요약. 무엇이 DB 로 갔는지 눈으로 보게 한다."""
        return "\n".join(
            f"  {' '.join(s.split())[:limit]}" for s in self.statements[:12]
        )


@pytest_asyncio.fixture
async def sql_spy(engine: AsyncEngine) -> AsyncIterator[SqlSpy]:
    """DB 왕복을 세는 스파이.

    시드 단계의 문장까지 세지 않으려면 측정 직전에 spy.reset() 을 호출한다.
    명시적으로 리셋하게 둔 것은, 무엇을 세는 구간인지 테스트를 읽는 사람이
    바로 알 수 있게 하려는 것이다.
    """
    spy = SqlSpy([])

    def _record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        spy.record(statement)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield spy
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _record)


@pytest_asyncio.fixture
async def clean_stores(engine: AsyncEngine) -> AsyncIterator[None]:
    """Postgres 와 Redis 를 둘 다 비운다.

    Postgres 만 비우면 게이트 키가 다음 테스트로 샌다. 게이트 TTL 은 수백 초라
    TRUNCATE 로 좌석이 비워져도 Redis 는 여전히 그 좌석을 막고 있고, 그러면
    다음 테스트가 첫 선점부터 실패한다 — 진단하기 나쁜 실패다.

    게이트 클라이언트 캐시도 비운다. 캐시는 (URL, 이벤트 루프)로 키를 잡는데,
    pytest-asyncio 가 테스트마다 루프를 새로 만들므로 남겨두면 죽은 루프에
    묶인 커넥션을 물려받는다.
    """
    await gate_client.close_all()

    async with engine.begin() as conn:
        await conn.execute(
            sa.text(f"TRUNCATE {', '.join(_ALL_TABLES)} RESTART IDENTITY CASCADE")
        )

    redis = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:16379/0"))
    try:
        await redis.flushall()
    except RedisError:
        # Redis 없이 도는 것도 정상이다 (fail-open 검증). 비울 것도 없다.
        pass
    finally:
        await redis.aclose()

    try:
        yield
    finally:
        await gate_client.close_all()


@dataclass(frozen=True, slots=True)
class Seeded:
    """테스트가 쓰는 회차 하나와 그 재고."""

    venue_id: int
    performance_id: int
    schedule_ids: tuple[int, ...]
    layout: VenueLayout
    user_ids: tuple[int, ...]
    #: 첫 회차의 좌석 id (seat_id 오름차순)
    seat_ids: tuple[int, ...]

    @property
    def schedule_id(self) -> int:
        return self.schedule_ids[0]


@pytest_asyncio.fixture
async def seeded(engine: AsyncEngine, clean_stores: None) -> Seeded:
    """1,200석 공연장 · 회차 3개 · 재고 3,600행 · 사용자 CONCURRENCY+10 명.

    설계 문서가 가정하는 규모를 그대로 재현한다. 총량 보존 불변식이
    1200 × 3 = 3600 을 기대하므로 회차 셋 다 전개한다.
    """
    layout = DEMO_HALL
    assert layout.total_seats == 1200, "레이아웃이 설계 문서의 가정(1,200석)과 다릅니다"

    async with engine.begin() as conn:
        venue_id = await schedule_service.create_venue(conn, layout)

        performance_id: int = (
            await conn.execute(
                sa.text(
                    "INSERT INTO performances (venue_id, title, running_time_min, age_limit)"
                    " VALUES (:v, :t, 150, 8) RETURNING id"
                ),
                {"v": venue_id, "t": "한여름 밤의 꿈"},
            )
        ).scalar_one()

        # 관람일시는 D+21 로 둔다. 취소 수수료 구간에서 "무료" 쪽에 들어가야
        # 취소 테스트가 수수료 계산과 얽히지 않는다.
        base = datetime.now(UTC) + timedelta(days=21)
        schedule_ids: list[int] = []
        for offset_h in (0, 24, 29):
            sid: int = (
                await conn.execute(
                    sa.text(
                        "INSERT INTO schedules (performance_id, starts_at, sale_opens_at)"
                        " VALUES (:p, :s, :o) RETURNING id"
                    ),
                    {
                        "p": performance_id,
                        "s": base + timedelta(hours=offset_h),
                        "o": datetime.now(UTC) - timedelta(minutes=1),
                    },
                )
            ).scalar_one()
            schedule_ids.append(sid)
            created = await schedule_service.expand_schedule_seats(
                conn, schedule_id=sid, venue_id=venue_id, layout=layout
            )
            assert created == 1200, f"회차 {sid} 재고 전개가 {created}행"

        user_ids = [
            r[0]
            for r in (
                await conn.execute(
                    sa.text(
                        "INSERT INTO users (email, name)"
                        " SELECT 'u'||g||'@test.local', 'user'||g"
                        "   FROM generate_series(1, :n) AS g"
                        " RETURNING id"
                    ),
                    {"n": CONCURRENCY + 10},
                )
            ).all()
        ]

        seat_ids = [
            r[0]
            for r in (
                await conn.execute(
                    sa.text(
                        "SELECT seat_id FROM schedule_seats"
                        " WHERE schedule_id = :s ORDER BY seat_id"
                    ),
                    {"s": schedule_ids[0]},
                )
            ).all()
        ]

    return Seeded(
        venue_id=venue_id,
        performance_id=performance_id,
        schedule_ids=tuple(schedule_ids),
        layout=layout,
        user_ids=tuple(user_ids),
        seat_ids=tuple(seat_ids),
    )


# ─────────────────────────────────────────────────────── 공용 헬퍼


async def try_hold(
    engine: AsyncEngine,
    *,
    schedule_id: int,
    user_id: int,
    seat_ids: list[int],
    **kw: object,
) -> str:
    """선점을 한 번 시도하고 결과를 문자열로 분류한다.

    예외를 삼키지 않고 분류만 하는 이유: 200개 요청 중 몇 개가 어떤 이유로
    실패했는지가 동시성 테스트의 측정 대상이기 때문이다. 데드락은 반드시
    별도 분류로 남긴다 — 그냥 실패로 묶으면 잠금 순서가 깨진 것을 놓친다.
    """
    from asyncpg.exceptions import DeadlockDetectedError
    from sqlalchemy.exc import DBAPIError

    from app.infra.db.engine import tx
    from app.service import hold_service
    from app.service.hold_service import HoldRejected, QuotaExceeded

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


async def status_counts(engine: AsyncEngine, schedule_id: int) -> dict[str, int]:
    """회차의 좌석 상태별 개수. 총량 보존 불변식의 재료."""
    from app.infra.db import queries

    async with engine.connect() as conn:
        rows = (
            await conn.execute(queries.seat_status_counts(schedule_id=schedule_id))
        ).mappings()
        return {r["status"]: r["n"] for r in rows}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
