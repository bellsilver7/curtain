"""Redis 클라이언트 — 커넥션 공유와 장애 관측 (설계 문서: Redis 좌석 게이트)

test_seat_gate.py 는 게이트의 겉보기 동작만 보는 블랙박스 명세다. 이 파일은
반대로 클라이언트 모듈의 내부 계약을 본다.

  커넥션은 요청 수에 비례하지 않는다   풀 하나를 게이트와 캐시가 나눠 쓴다.
                                       요청마다 접속하면 오픈런에서 Redis 가
                                       커넥션 수립만 하다 끝난다.
  Redis 장애는 조용히 지나가지 않는다   fail-open 은 요청을 살리지만, 아무 신호도
                                       남기지 않으면 Redis 가 죽은 채로 전 요청이
                                       DB 로 가는 상황을 아무도 모른다.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import redis.asyncio as aioredis

from app.infra.redis import client as redis_client
from app.service import seatmap_service
from tests.conftest import CONCURRENCY, Seeded

pytestmark = pytest.mark.integration

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:16379/0")

#: 아무도 듣지 않는 포트. 진짜 connection refused 를 타게 하려는 것이다.
DEAD_REDIS_URL = "redis://127.0.0.1:1/0"


async def _handshakes(admin: aioredis.Redis) -> int:
    """Redis 가 지금까지 받은 접속 횟수 (단조 증가).

    connected_clients(현재 접속 수)로는 이것을 볼 수 없다. 호출마다 클라이언트를
    새로 만들어도 파이썬 refcount 가 곧바로 소켓을 닫아버리므로 동시 접속 수는
    거의 그대로다 — 실제로 그 지표로 테스트를 썼다가 "호출마다 새 클라이언트"
    사보타주가 초록으로 통과하는 것을 보고 고쳤다. 세야 하는 것은 몇 개가 살아
    있느냐가 아니라 몇 번 접속했느냐다.
    """
    return int((await admin.info("stats"))["total_connections_received"])


async def test_connections_do_not_scale_with_requests(
    engine, seeded: Seeded
) -> None:
    """조회 200회를 순차로 던져도 접속은 한 번이다.

    풀과 클라이언트는 (URL, 이벤트 루프) 당 하나이고, 게이트와 캐시가 그것을
    나눠 쓴다. 계층마다 자기 풀을 만들거나 호출마다 접속하면 이 테스트가 깨진다.

    동시 요청에서는 풀이 자라지만 상한이 있다 — 자라는 폭이 요청 수가 아니라
    _MAX_CONNECTIONS 로 묶여 있다는 것이 요점이다.
    """
    admin = aioredis.from_url(REDIS_URL)
    try:
        # 캐시를 한 번 채워 둔다. 이후 200회는 전부 적중이고, 적중 경로가
        # 요청마다 접속하지 않는다는 것이 이 테스트의 대상이다.
        await seatmap_service.get(engine, schedule_id=seeded.schedule_id)
        base = await _handshakes(admin)

        for _ in range(200):
            await seatmap_service.get(engine, schedule_id=seeded.schedule_id)
        sequential = await _handshakes(admin) - base
        assert sequential <= 2, (
            f"순차 조회 200회에 Redis 접속이 {sequential}번 일어났다 (기대 2번 이하). "
            f"호출마다 접속하거나 계층별로 풀을 따로 만들고 있다."
        )

        await asyncio.gather(
            *(
                seatmap_service.get(engine, schedule_id=seeded.schedule_id)
                for _ in range(CONCURRENCY)
            )
        )
        concurrent = await _handshakes(admin) - base
        assert concurrent <= redis_client._MAX_CONNECTIONS + 2, (
            f"동시 조회 {CONCURRENCY}회에 Redis 접속이 {concurrent}번 일어났다 "
            f"(상한 {redis_client._MAX_CONNECTIONS}). 풀 상한이 안 걸리고 있다."
        )
    finally:
        await admin.aclose()


async def test_cache_outage_is_counted_not_swallowed(
    engine, seeded: Seeded, monkeypatch
) -> None:
    """Redis 가 죽어도 좌석맵은 정확하고, 그 사실이 집계에 남는다.

    fail-open 자체는 test_seatmap.py 가 이미 본다. 여기서 보는 것은 관측이다 —
    이유 없는 degrade 는 오진을 부른다. 게이트에서 "degraded 100건"만 보고
    원인을 타임아웃으로 오진했다가, 실제로는 MaxConnectionsError 였던 적이 있다.
    캐시 경로도 같은 계기를 남겨야 한다.
    """
    monkeypatch.setenv("REDIS_URL", DEAD_REDIS_URL)
    await redis_client.close_all()
    redis_client.reset_stats()

    seatmap = await seatmap_service.get(engine, schedule_id=seeded.schedule_id)

    assert len(seatmap.seats) == 1200, "Redis 없이 좌석맵이 불완전하다"
    assert seatmap.from_cache is False

    reasons = dict(redis_client.degrade_reasons)
    assert any(k.startswith("cache_get:") for k in reasons), (
        f"캐시 읽기 실패가 집계되지 않았다. Redis 가 죽은 것을 아무도 모르는 "
        f"상태로 서비스가 계속된다.\n집계: {reasons or '비어 있음'}"
    )
    assert any(k.startswith("cache_set:") for k in reasons), (
        f"캐시 쓰기 실패가 집계되지 않았다.\n집계: {reasons or '비어 있음'}"
    )


async def test_corrupt_cache_value_is_ignored_and_counted(
    engine, seeded: Seeded
) -> None:
    """깨진 캐시 값은 없는 것으로 보고, 그것도 센다.

    배포 중에 직렬화 형식이 엇갈리면 이 경로를 탄다. 요청은 살아야 하지만
    (원본을 읽으면 된다) 조용히 넘기면 캐시가 영원히 안 맞는 것을 모른다.
    """
    admin = aioredis.from_url(REDIS_URL)
    try:
        await admin.set(
            seatmap_service._cache_key(seeded.schedule_id), "{이건 JSON 이 아니다", px=60_000
        )
        redis_client.reset_stats()

        seatmap = await seatmap_service.get(engine, schedule_id=seeded.schedule_id)

        assert len(seatmap.seats) == 1200, "깨진 캐시 값 때문에 조회가 망가졌다"
        assert redis_client.degrade_reasons["cache_get:corrupt"] >= 1, (
            f"깨진 값을 조용히 넘겼다.\n집계: {dict(redis_client.degrade_reasons)}"
        )
    finally:
        await admin.aclose()
