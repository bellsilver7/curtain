"""좌석맵 조회와 캐시 (설계 문서: 좌석맵 조회 부하)

요구 두 개가 서로 긴장 관계다.

  조회는 DB 에 가면 안 된다   요청 수가 선점보다 수십 배 많다. 매번 1,200행을
                              읽으면 선점 트랜잭션이 쓸 커넥션이 남지 않는다.
  그래도 TTL 안에 수렴해야 한다  무한정 낡은 값을 주면 좌석맵이 거짓말이 된다.

좌석맵은 힌트이고 진짜 판정은 선점 API 가 한다. 그래서 stale 을 허용할 수 있고,
허용하는 대신 상한(policy.SEATMAP_CACHE_TTL)을 둔다.

무효화 API 는 만들지 않는다. 개별 좌석이 바뀔 때마다 정교하게 무효화하려 들면
무효화 누락 버그가 생기고, 그건 짧은 TTL 이 주는 stale 보다 훨씬 고약하다.
무효화 로직이 없으면 무효화 버그도 없다.

캐시는 최적화이지 정합성이 아니다. Redis 가 죽으면 매 요청이 DB 로 가서
느려질 뿐, 좌석맵 내용은 정확하다 (fail-open). 그 fail-open 은 이 모듈이 아니라
app/infra/redis/client.py 가 갖는다 — 여기서는 키 이름과 페이로드 모양만 정한다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from hashlib import blake2b
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain import policy
from app.infra.db import queries
from app.infra.redis import client as redis_client
from app.service.dto import SeatCell, Seatmap

#: 캐시 키. 뒤의 버전 태그는 직렬화 형식을 바꿀 때 올린다 — 배포 중에 예전
#: 형식이 남아 있어도 새 코드가 그것을 읽으려 하지 않게.
_CACHE_VERSION = "v1"


def _cache_key(schedule_id: int) -> str:
    return f"seatmap:{{{schedule_id}}}:{_CACHE_VERSION}"


def _label(zone: str, row_label: str, col_no: int) -> str:
    return f"{zone} {row_label}열 {col_no}번"


def _etag(schedule_id: int, rows: Sequence[Sequence[Any]]) -> str:
    """내용에서 결정적으로 계산한다.

    schedule_id 를 함께 넣는 이유: 두 회차의 좌석 상태가 우연히 같아도 서로
    다른 리소스이므로 etag 가 같으면 안 된다.
    """
    material = json.dumps([schedule_id, rows], separators=(",", ":"), ensure_ascii=False)
    return blake2b(material.encode("utf-8"), digest_size=12).hexdigest()


def _to_seats(rows: Sequence[Sequence[Any]]) -> tuple[SeatCell, ...]:
    return tuple(
        SeatCell(
            seat_id=int(seat_id),
            label=_label(zone, row_label, int(col_no)),
            grade=grade,
            price=int(price),
            status=str(status),
        )
        for seat_id, zone, row_label, col_no, grade, price, status in rows
    )


# ─────────────────────────────────────────────────────────── 단일 비행

#: (이벤트 루프, 회차) 별 락.
#:
#: 캐시 스탬피드를 막는다. 오픈 직전 모두가 새로고침하는 순간, 캐시가 비어
#: 있으면 나이브한 구현은 요청 수만큼 DB 를 때린다 — 캐시가 없을 때와 똑같아진다.
#:
#: 루프를 키에 넣는 이유는 커넥션 캐시와 같다. asyncio.Lock 은 처음 대기할 때의
#: 루프에 묶이므로, 테스트마다 루프가 바뀌는 환경에서 락을 재사용하면 죽은 루프를
#: 기다리게 된다.
#:
#: 이 락은 프로세스 안에서만 유효하다. 여러 프로세스를 띄우면 프로세스 수만큼
#: DB 조회가 생기는데, 그 정도는 수용한다 — 분산 락을 걸면 캐시 조회 경로에
#: Redis 왕복이 하나 더 붙고, 그게 캐시로 아끼려던 비용과 비슷해진다.
_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _lock_for(schedule_id: int) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), schedule_id)
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


# ─────────────────────────────────────────────────────────── 캐시 입출력


async def _cache_read(schedule_id: int) -> Seatmap | None:
    """캐시 적중이면 Seatmap, 아니면 None.

    왕복과 실패 처리는 redis 클라이언트가 갖는다. 여기 남은 일은 페이로드를
    Seatmap 으로 되돌리는 것뿐이다 — 어떤 계층이 Redis 를 쓰든 장애 처리가
    한 곳에만 있어야, 캐시를 쓰는 곳이 늘어날 때 fail-open 을 빠뜨리지 않는다.

    형식이 예상과 다르면 없는 것으로 본다. 직렬화 형식을 바꿀 때는 캐시 키의
    버전 태그를 올리는 것이 정공법이고, 이 분기는 그때 놓친 값에 대한 보험이다.
    """
    payload = await redis_client.cache_get_json(_cache_key(schedule_id))
    if not isinstance(payload, dict):
        return None
    try:
        return Seatmap(
            schedule_id=schedule_id,
            etag=payload["etag"],
            seats=_to_seats(payload["rows"]),
            from_cache=True,
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _cache_write(schedule_id: int, etag: str, rows: list[list[Any]]) -> None:
    """best-effort. 실패해도 조회는 이미 성공했으므로 그냥 넘어간다."""
    await redis_client.cache_set_json(
        _cache_key(schedule_id),
        {"etag": etag, "rows": rows},
        ttl_sec=policy.SEATMAP_CACHE_TTL.total_seconds(),
    )


async def _read_db(engine: AsyncEngine, schedule_id: int) -> list[list[Any]]:
    """회차 좌석 전량. ix_seatmap 의 INCLUDE 로 힙 접근 없이 인덱스에서 끝난다.

    직렬화하기 쉬운 리스트로 돌려준다 — 캐시에 넣는 형태와 DB 에서 읽은 형태를
    같게 두면, 두 경로가 서로 다른 결과를 주는 종류의 버그가 사라진다.
    """
    async with engine.connect() as conn:
        result = await conn.execute(queries.seatmap(schedule_id=schedule_id))
        return [
            [r.seat_id, r.zone, r.row_label, r.col_no, r.grade, r.price, str(r.status)]
            for r in result.mappings()
        ]


# ─────────────────────────────────────────────────────────── 공개 API


async def get(engine: AsyncEngine, *, schedule_id: int) -> Seatmap:
    """회차 좌석맵. 캐시 적중이면 DB 에 가지 않는다."""
    cached = await _cache_read(schedule_id)
    if cached is not None:
        return cached

    async with _lock_for(schedule_id):
        # 락을 기다리는 동안 앞선 요청이 캐시를 채웠을 수 있다. 이 재확인이
        # 스탬피드를 막는 핵심이다 — 빼면 대기했던 요청들이 차례로 DB 로 간다.
        cached = await _cache_read(schedule_id)
        if cached is not None:
            return cached

        rows = await _read_db(engine, schedule_id)
        etag = _etag(schedule_id, rows)
        await _cache_write(schedule_id, etag, rows)

        return Seatmap(
            schedule_id=schedule_id,
            etag=etag,
            seats=_to_seats(rows),
            from_cache=False,
        )
