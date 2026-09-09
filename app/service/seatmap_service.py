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
느려질 뿐, 좌석맵 내용은 정확하다 (fail-open).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import blake2b
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain import policy
from app.infra.db import queries
from app.infra.redis import client as redis_client

#: 캐시 키. 뒤의 버전 태그는 직렬화 형식을 바꿀 때 올린다 — 배포 중에 예전
#: 형식이 남아 있어도 새 코드가 그것을 읽으려 하지 않게.
_CACHE_VERSION = "v1"


def _cache_key(schedule_id: int) -> str:
    return f"seatmap:{{{schedule_id}}}:{_CACHE_VERSION}"


@dataclass(frozen=True, slots=True)
class SeatView:
    """좌석 하나. 클라이언트가 그대로 그릴 수 있는 형태다."""

    seat_id: int
    #: 사람이 읽는 좌석 이름. 서버가 만든다 — 좌석 번호 규칙이 공연장마다
    #: 다르고, 클라이언트 세 곳에서 각자 조립하면 세 곳이 다르게 틀린다.
    label: str
    grade: str
    price: int
    status: str


@dataclass(frozen=True, slots=True)
class Seatmap:
    schedule_id: int
    #: 같은 내용이면 같은 값. HTTP 계층이 304 를 판정하는 근거다.
    etag: str
    seats: tuple[SeatView, ...]
    #: 캐시에서 답했는가. 정확성과는 무관하고 관측용이다.
    from_cache: bool


def _label(zone: str, row_label: str, col_no: int) -> str:
    return f"{zone} {row_label}열 {col_no}번"


def _etag(schedule_id: int, rows: Sequence[Sequence[Any]]) -> str:
    """내용에서 결정적으로 계산한다.

    schedule_id 를 함께 넣는 이유: 두 회차의 좌석 상태가 우연히 같아도 서로
    다른 리소스이므로 etag 가 같으면 안 된다.
    """
    material = json.dumps([schedule_id, rows], separators=(",", ":"), ensure_ascii=False)
    return blake2b(material.encode("utf-8"), digest_size=12).hexdigest()


def _to_seats(rows: Sequence[Sequence[Any]]) -> tuple[SeatView, ...]:
    return tuple(
        SeatView(
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

    Redis 장애도 None 이다 — 호출자는 "캐시에 없다"와 "Redis 가 없다"를
    구분할 필요가 없고, 구분하게 만들면 장애 처리를 빠뜨리기 쉽다.

    fail-open 을 실제로 담당하는 것은 아래 except 절이다. connection() 은
    Redis 가 죽어 있어도 None 을 주지 않는다 — redis-py 의 커넥션 생성은
    lazy 라서 명령을 보낼 때 처음 터진다. client is None 검사는 잘못된 URL
    같은 경우에만 걸린다. 이 구조를 오해해서 fail-open 을 없앤 줄 알고
    사보타주했다가 테스트가 초록인 것을 보고 두 번 헷갈렸다.
    """
    client = await redis_client.connection()
    if client is None:
        return None
    try:
        raw = await client.get(_cache_key(schedule_id))
    except (RedisError, OSError):
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return Seatmap(
            schedule_id=schedule_id,
            etag=payload["etag"],
            seats=_to_seats(payload["rows"]),
            from_cache=True,
        )
    except (ValueError, KeyError, TypeError):
        # 형식이 깨진 값은 없는 것으로 본다. 곧 TTL 로 사라진다.
        return None


async def _cache_write(schedule_id: int, etag: str, rows: list[list[Any]]) -> None:
    """best-effort. 실패해도 조회는 이미 성공했으므로 조용히 넘어간다."""
    client = await redis_client.connection()
    if client is None:
        return
    payload = json.dumps(
        {"etag": etag, "rows": rows}, separators=(",", ":"), ensure_ascii=False
    )
    try:
        await client.set(
            _cache_key(schedule_id),
            payload,
            px=int(policy.SEATMAP_CACHE_TTL.total_seconds() * 1000),
        )
    except (RedisError, OSError):
        return


async def _read_db(engine: AsyncEngine, schedule_id: int) -> list[list[Any]]:
    """회차 좌석 전량. ix_seatmap 의 INCLUDE 로 힙 접근 없이 인덱스에서 끝난다.

    직렬화하기 쉬운 리스트로 돌려준다 — 캐시에 넣는 형태와 DB 에서 읽은 형태를
    같게 두면, 두 경로가 서로 다른 결과를 주는 종류의 버그가 사라진다.
    """
    async with engine.connect() as conn:
        result = await conn.execute(queries.SEATMAP, {"schedule_id": schedule_id})
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
