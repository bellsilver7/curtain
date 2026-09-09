"""Alembic 실행 환경 — 설계 문서 §3

접속 정보는 DATABASE_URL 환경변수에서만 읽는다. alembic.ini 에 박으면
비밀번호가 저장소에 남는다.

autogenerate 를 쓰지만 무조건 믿지는 않는다. 이 스키마는 Alembic 이 diff 를
잘 못 뜨는 Postgres 기능(부분 인덱스 · 커버링 인덱스 · CHECK · ENUM) 위에
서 있어서, 생성된 리비전은 항상 사람이 읽고 손본다.
자세한 주의사항은 docs/adr/0002-migrations.md 참고.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.infra.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

DEFAULT_URL = "postgresql+asyncpg://curtain:curtain@localhost:15432/curtain"


def _url() -> str:
    url = os.getenv("DATABASE_URL", DEFAULT_URL)
    # .env 는 앱용 asyncpg URL 을 쓴다. alembic 도 async 로 돌리므로 그대로 사용.
    return url


def _configure(connection: Connection | None = None, **extra: object) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # 컬럼 타입 변경을 감지한다. 끄면 integer→bigint 같은 변경이 조용히 누락된다.
        compare_type=True,
        # server_default 변경도 본다. 오탐이 있으면 리비전에서 지우면 된다.
        compare_server_default=True,
        # ENUM 등 Postgres 네이티브 타입을 리비전에 그대로 렌더한다.
        include_schemas=False,
        **extra,
    )


def run_migrations_offline() -> None:
    """`alembic upgrade --sql` — DB 접속 없이 SQL만 출력."""
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
