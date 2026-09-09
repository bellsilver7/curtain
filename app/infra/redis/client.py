"""Redis 좌석 게이트 클라이언트 (설계 문서: Redis 좌석 게이트)

게이트는 정합성이 아니라 부하를 위한 것이다. 같은 좌석에 몰린 수백 요청을
DB 행 잠금까지 내려보내지 않고 여기서 걸러낸다. Redis 를 통째로 날려도
오버부킹은 발생하지 않아야 하고, 그래서 이 모듈의 모든 실패는 fail-open 이다.

대기열은 반대로 fail-closed 다. 같은 Redis 장애에 대응이 반대인 이유는 하나가
정합성 밖에 있고 하나가 부하 방벽이기 때문이다.

API 는 컨텍스트 매니저 하나다.

    async with hold_gate(schedule_id=.., user_id=.., seat_ids=[..],
                         hold_ttl_sec=420) as gate:
        if not gate.acquired:
            raise HoldRejected(list(gate.blocked_seat_ids))
        ...DB 작업...
        gate.keep()          # 성공했을 때만 유지

keep() 을 부르지 않고 블록을 벗어나면 게이트는 자동으로 반납된다. 함수 세 개로
쪼개 두면 "DB 가 거절했을 때 반납"을 잊기 쉽고, 잊으면 유령 매진이 된다 —
잊을 수 없는 모양으로 만드는 것이 이 설계의 요점이다.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import redis.asyncio as aioredis
from redis.asyncio.client import Redis
from redis.exceptions import RedisError

from app.domain import policy

DEFAULT_URL = "redis://localhost:16379/0"

_LUA_DIR = Path(__file__).parent / "lua"

#: 게이트 TTL / hold TTL 비율. 게이트는 원본(DB)보다 먼저 사라져야 한다 —
#: 게이트가 더 오래 남으면 DB 에서 이미 풀린 좌석이 Redis 때문에 계속 막히는,
#: 진단하기 고약한 유령 매진이 생긴다.
#:
#: policy.SEAT_GATE_TTL 을 그대로 쓰지 않고 비율로 환산하는 이유: 테스트나 운영에서
#: hold TTL 을 짧게 주면(예: 1초) 고정 상수 410초가 훨씬 오래 남아 같은 문제가 난다.
#: TTL 관계는 절대값이 아니라 비례 관계로 지켜야 한다.
_GATE_TTL_RATIO = policy.SEAT_GATE_TTL / policy.HOLD_TTL

#: 타임아웃 세 개. 게이트는 "빠르게 답하거나 빠진다"가 원칙이지만, 너무 조이면
#: 부하가 몰릴 때 정상 동작이 장애로 오인되어 fail-open 이 남발된다.
#: 아래 값은 상한이지 목표가 아니다 — 정상 응답은 1ms 미만이다.
_CONNECT_TIMEOUT_SEC = float(os.getenv("REDIS_CONNECT_TIMEOUT_SEC", "1.0"))
_CMD_TIMEOUT_SEC = float(os.getenv("REDIS_CMD_TIMEOUT_SEC", "0.5"))
#: 풀에서 커넥션을 기다리는 상한. 명령이 1ms 미만이므로 대기는 거의 없다.
_POOL_WAIT_SEC = float(os.getenv("REDIS_POOL_WAIT_SEC", "1.0"))


def redis_url() -> str:
    """호출 시점에 환경변수를 읽는다.

    모듈 로드 시점에 읽어 두면 테스트가 URL 을 바꿔치기할 수 없고, 무엇보다
    "Redis 가 없는 환경"을 재현할 수 없다.
    """
    return os.getenv("REDIS_URL", DEFAULT_URL)


@lru_cache(maxsize=4)
def _lua(name: str) -> str:
    return (_LUA_DIR / name).read_text(encoding="utf-8")


@dataclass(slots=True)
class _Bundle:
    """클라이언트와 등록된 스크립트."""

    client: Redis
    acquire: object  # redis.asyncio.client.AsyncScript
    release: object


#: (URL, 이벤트 루프) 별 캐시.
#:
#: URL 을 키에 넣는 이유: 테스트가 REDIS_URL 을 바꿔치기하면 새 클라이언트를 써야 한다.
#: 루프를 키에 넣는 이유: asyncio 커넥션은 만들어진 루프에 묶인다. pytest-asyncio 는
#: 테스트마다 루프를 새로 만들므로, 루프를 무시하고 캐시하면 두 번째 테스트가
#: "attached to a different loop" 로 죽는다.
_bundles: dict[tuple[str, int], _Bundle] = {}


async def _bundle() -> _Bundle | None:
    """연결된 번들, 또는 Redis 에 닿지 못하면 None (fail-open).

    None 을 돌려주는 것이 이 모듈의 장애 표현이다. 예외를 던지면 호출자마다
    try/except 를 붙여야 하고, 하나만 빠뜨려도 Redis 장애가 판매 중단이 된다.
    """
    url = redis_url()
    key = (url, id(asyncio.get_running_loop()))
    bundle = _bundles.get(key)
    if bundle is not None:
        return bundle

    try:
        # BlockingConnectionPool 을 반드시 쓴다. 이것이 이 파일에서 가장
        # 중요한 한 줄이다.
        #
        # 기본 ConnectionPool 은 풀이 마르면 기다리지 않고 즉시
        # MaxConnectionsError 를 던진다. 그 예외는 아래에서 fail-open 으로
        # 처리되므로, 게이트가 필요한 바로 그 순간에 게이트가 사라진다.
        # (실측: 좌석 1석에 200 동시 요청 → 100건이 MaxConnectionsError 로
        #  degraded. redis-py 8 의 기본 상한이 100 이다.)
        #
        # 게이트 명령은 1ms 미만이므로, 잠깐 기다려 커넥션을 재사용하는 쪽이
        # 압도적으로 낫다. Blocking 으로 바꾸면 같은 부하에서 degraded 0 건이다.
        #
        # 타임아웃과는 무관한 문제였다. 처음에는 접속 폭풍이 타임아웃을 유발한
        # 것으로 오진했지만, 타임아웃을 0.25s ↔ 1.0s 로 바꿔도 결과는 같고
        # 풀 종류만이 갈랐다. 아래 타임아웃 값은 별개의 안전장치다.
        pool = aioredis.BlockingConnectionPool.from_url(
            url,
            decode_responses=True,
            max_connections=int(os.getenv("REDIS_MAX_CONNECTIONS", "64")),
            timeout=_POOL_WAIT_SEC,
            socket_timeout=_CMD_TIMEOUT_SEC,
            socket_connect_timeout=_CONNECT_TIMEOUT_SEC,
            health_check_interval=30,
        )
        client: Redis = aioredis.Redis(connection_pool=pool)
        # register_script 는 EVALSHA 를 쓰고 NOSCRIPT 면 알아서 EVAL 로 재시도한다.
        # SHA 를 직접 들고 다니거나 Redis 에 저장하면 SCRIPT FLUSH · 서버 재시작 ·
        # 새 인스턴스 투입 때 NOSCRIPT 로 깨진다.
        bundle = _Bundle(
            client=client,
            acquire=client.register_script(_lua("seat_gate.lua")),
            release=client.register_script(_lua("seat_gate_release.lua")),
        )
    except (RedisError, OSError, ValueError):
        return None

    _bundles[key] = bundle
    return bundle


def _keys(schedule_id: int, seat_ids: Sequence[int]) -> list[str]:
    """게이트 키. {schedule_id} 해시 태그로 클러스터 슬롯을 고정한다."""
    return [f"seat:{{{schedule_id}}}:{seat_id}" for seat_id in seat_ids]


def _token(user_id: int) -> str:
    """게이트 소유자 표시.

    난수가 아니라 user_id 에서 파생시킨다. release() 는 선점과 다른 요청이므로
    난수 토큰을 알 방법이 없고, 그러면 자기 게이트를 반납할 수 없다.
    남의 게이트를 지우지 못하게 하는 성질은 그대로 유지된다 — 다른 사용자는
    다른 토큰이다.
    """
    return f"u:{user_id}"


@dataclass(slots=True)
class Gate:
    """게이트 획득 결과. 블록을 벗어날 때 keep() 안 했으면 반납된다."""

    acquired: bool
    #: 게이트가 알려준 막힌 좌석. DB 를 다시 조회하지 않고 이 값을 응답에 쓴다.
    blocked_seat_ids: tuple[int, ...] = ()
    #: Redis 에 닿지 못해 게이트를 우회했는가. 정확성에는 영향이 없고, 관측용이다.
    degraded: bool = False
    _kept: bool = field(default=False, repr=False)

    def keep(self) -> None:
        """DB 확정까지 성공했을 때만 부른다. 게이트를 hold TTL 동안 유지한다."""
        self._kept = True


@asynccontextmanager
async def hold_gate(
    *,
    schedule_id: int,
    user_id: int,
    seat_ids: Sequence[int],
    hold_ttl_sec: float,
) -> AsyncIterator[Gate]:
    """좌석 게이트를 잡고, keep() 하지 않으면 반납한다.

    seat_ids 는 오름차순으로 정렬해서 넘긴다 — SQL 의 ORDER BY seat_id
    FOR UPDATE 와 잠금 순서를 맞춰, 교차 요청이 게이트 단계에서 엇갈리지 않게.

    Redis 에 닿지 못하면 acquired=True, degraded=True 로 통과시킨다.
    정합성은 DB 가 지키므로 게이트를 건너뛰어도 오버부킹은 나지 않는다.
    """
    ordered = sorted(set(seat_ids))
    bundle = await _bundle()

    if bundle is None:
        yield Gate(acquired=True, degraded=True)
        return

    keys = _keys(schedule_id, ordered)
    token = _token(user_id)
    ttl_ms = max(1, int(hold_ttl_sec * _GATE_TTL_RATIO * 1000))

    try:
        blocked_index = int(await bundle.acquire(keys=keys, args=[token, ttl_ms]))  # type: ignore[operator]
    except (RedisError, OSError):
        # 획득 도중 장애 — 게이트 없이 진행한다. 남은 키는 TTL 이 알아서 지운다.
        yield Gate(acquired=True, degraded=True)
        return

    if blocked_index:
        # Lua 가 1-based 인덱스를 돌려준다. 실패 시 이미 스크립트가 부분 획득을
        # 되돌렸으므로 여기서 추가로 반납할 것이 없다.
        yield Gate(acquired=False, blocked_seat_ids=(ordered[blocked_index - 1],))
        return

    gate = Gate(acquired=True)
    try:
        yield gate
    finally:
        if not gate._kept:
            await _release(bundle, keys, token)


async def release_gate(
    *, schedule_id: int, user_id: int, seat_ids: Sequence[int]
) -> int:
    """게이트를 명시적으로 반납한다 (hold_service.release() 용).

    반환값은 실제로 지운 키 수. Redis 가 없으면 0 이고, 그래도 좌석은 DB 에서
    풀렸으므로 판매에는 문제가 없다 — 게이트 TTL 이 지나면 자연히 열린다.
    """
    bundle = await _bundle()
    if bundle is None:
        return 0
    return await _release(
        bundle, _keys(schedule_id, sorted(set(seat_ids))), _token(user_id)
    )


async def _release(bundle: _Bundle, keys: list[str], token: str) -> int:
    """반납은 best-effort 다.

    여기서 예외가 올라가면, DB 는 이미 커밋됐는데 응답이 실패로 바뀔 수 있다.
    게이트를 못 지운 대가는 그 좌석이 TTL 동안 안 팔리는 것뿐이므로,
    실패를 삼키는 쪽이 낫다.
    """
    if not keys:
        return 0
    try:
        return int(await bundle.release(keys=keys, args=[token]))  # type: ignore[operator]
    except (RedisError, OSError):
        return 0


async def close_all() -> None:
    """열린 커넥션을 닫는다. 앱 종료 훅과 테스트 정리에서 호출한다."""
    for bundle in list(_bundles.values()):
        try:
            await bundle.client.aclose()
        except (RedisError, OSError):
            pass
    _bundles.clear()
