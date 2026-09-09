"""테스트 픽스처 — 설계 문서 §10

integration 마크가 붙은 테스트는 docker-compose 의 postgres/redis 를 요구한다.
`make test-unit` 은 그것들을 제외하고 도메인 테스트만 돌린다.
"""

import pytest

# TODO(1주차): db_engine / redis_client / clean_schedule 픽스처
# TODO(2주차): seed_schedule(seats=1200) — 동시성 테스트용 회차 준비
# TODO(3주차): fake_pg(latency_ms=..., timeout_rate=...) 픽스처


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
