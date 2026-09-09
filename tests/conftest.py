"""테스트 픽스처 — 설계 문서 §10

`integration` 마크가 붙은 테스트는 docker-compose 의 postgres 를 요구한다.
`make test-unit` 은 그것들을 제외하고 도메인 테스트만 돌린다.

동시성 테스트는 커넥션을 많이 쓴다. 풀이 좁으면 "락 경합"이 아니라 "풀 대기"를
측정하게 되고, §5.2 의 검증이 무의미해진다 — 그래서 테스트 엔진은 풀을 넓게 잡는다.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.infra.db.engine import build_engine
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


@pytest_asyncio.fixture
async def clean_db(engine: AsyncEngine) -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(f"TRUNCATE {', '.join(_ALL_TABLES)} RESTART IDENTITY CASCADE")
        )
    yield


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
async def seeded(engine: AsyncEngine, clean_db: None) -> Seeded:
    """1,200석 공연장 · 회차 3개 · 재고 3,600행 · 사용자 CONCURRENCY+10 명.

    설계 문서 §1 의 "가정"을 그대로 재현한다. 총량 보존 불변식(§10)이
    `1200 × 3 = 3600` 을 기대하므로 회차 셋 다 전개한다.
    """
    layout = DEMO_HALL
    assert layout.total_seats == 1200, "레이아웃이 §1 가정(1,200석)과 다릅니다"

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

        # 관람일시는 D+21 로 둔다. 취소 수수료 구간(§7.3)에서 "무료" 쪽에 들어가야
        # 취소 테스트가 수수료 계산과 얽히지 않는다.
        base = datetime.now(timezone.utc) + timedelta(days=21)
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
                        "o": datetime.now(timezone.utc) - timedelta(minutes=1),
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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
