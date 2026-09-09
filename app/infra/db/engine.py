"""엔진 · 트랜잭션 경계 — 설계 문서: 좌석맵 조회 부하

커넥션 풀 크기가 곧 동시 처리량 상한이다. 오픈런에서 좌석맵 조회가 풀을 다 먹으면
선점 트랜잭션이 쓸 커넥션이 남지 않는다 — 좌석맵을 캐시로 뺀 이유가 이것이다.

statement_timeout 은 반드시 건다. 선점은 행 잠금을 기다리는 쿼리이므로,
상한이 없으면 경합이 몰릴 때 커넥션이 무한정 붙잡힌 채 풀이 마른다.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

load_dotenv(override=False)

DEFAULT_URL = "postgresql+asyncpg://curtain:curtain@localhost:15432/curtain"


def database_url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_URL)


def build_engine(url: str | None = None, **overrides: object) -> AsyncEngine:
    """엔진을 새로 만든다. 테스트는 이벤트 루프마다 자기 엔진이 필요하므로 이걸 쓴다."""
    kwargs: dict[str, object] = {
        "pool_size": int(os.getenv("DB_POOL_SIZE", "20")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "10")),
        "pool_pre_ping": True,
        "connect_args": {
            "server_settings": {
                "statement_timeout": os.getenv("DB_STATEMENT_TIMEOUT_MS", "3000"),
                "application_name": "curtain",
            }
        },
    }
    kwargs.update(overrides)
    return create_async_engine(url or database_url(), **kwargs)  # type: ignore[arg-type]


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """앱 전역 엔진. FastAPI 프로세스 하나당 하나."""
    return build_engine()


@asynccontextmanager
async def tx(engine: AsyncEngine | None = None) -> AsyncIterator[AsyncConnection]:
    """트랜잭션 경계.

    원자성이 필요한 모든 작업(좌석 선점, 주문 확정)은 이걸 통과한다.
    서비스 계층이 트랜잭션을 열고, 쿼리는 열지 않는다 — 경계가 두 곳에 있으면
    "이미 트랜잭션 안인가?"를 매번 따져야 한다.
    """
    async with (engine or get_engine()).connect() as conn:
        async with conn.begin():
            yield conn
