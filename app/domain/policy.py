"""정책 상수와 순수 계산 — 설계 문서 §1.1, §7.3

이 모듈은 표준 라이브러리만 import 한다. DB도 Redis도 HTTP도 모른다 (§9 계층 규칙).
정책 숫자를 코드 전역에 흩뿌리지 않기 위한 단일 출처이며,
`tests/test_seat_domain.py` 가 DB 없이 이 파일만 검증한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

# ---------------------------------------------------------------- 좌석 선점

#: 좌석 선점 유효 시간. 국내 예매 서비스 관례(약 7분)를 따랐다.
HOLD_TTL = timedelta(seconds=420)

#: Redis 좌석 게이트 TTL. hold TTL보다 **짧아야** 한다 (§5.3).
#: 게이트가 DB보다 오래 남으면 이미 풀린 좌석이 계속 막히는 유령 매진이 생긴다.
SEAT_GATE_TTL = timedelta(seconds=410)

#: 1인 회차당 구매 한도.
MAX_SEATS_PER_ORDER = 4

# ---------------------------------------------------------------- 대기열

#: 예매 API 실측 처리량에서 역산한 입장 유량. 부하 테스트 결과에 따라 조인다 (§6).
ADMIT_PER_SECOND = 200

#: 입장 토큰 유효 시간.
ENTRY_TOKEN_TTL = timedelta(minutes=10)

#: 클라이언트 순번 폴링 간격. 이보다 잦은 요청은 레이트리밋 대상.
QUEUE_POLL_INTERVAL = timedelta(seconds=2)

#: 폴링이 이 시간 이상 끊긴 대기자는 줄에서 뺀다 (§6 이탈 감지).
QUEUE_HEARTBEAT_TIMEOUT = timedelta(seconds=15)

# ---------------------------------------------------------------- 결제

#: 주문이 PENDING으로 이 시간을 넘기면 리컨실러가 PG사에 되묻는다 (§2.1).
PAYMENT_PENDING_LIMIT = timedelta(minutes=5)

#: hold 잔여가 이보다 적으면 결제 승인 요청 자체를 거절한다.
#: 승인 왕복 중에 hold가 만료되는 창을 좁히기 위한 장치 (§4 참고).
MIN_HOLD_REMAINING_FOR_PAYMENT = timedelta(seconds=60)

# ---------------------------------------------------------------- 취소 수수료

#: (관람일까지 남은 일수 하한, 티켓금액 대비 수수료율).
#: 위에서부터 처음 만족하는 구간을 적용한다.
CANCEL_FEE_TIERS: tuple[tuple[int, Decimal], ...] = (
    (8, Decimal("0.00")),   # D-8 이전       : 무료
    (3, Decimal("0.10")),   # D-7 ~ D-3      : 10%
    (1, Decimal("0.20")),   # D-2 ~ D-1      : 20%
)


class CancelClosed(Exception):
    """관람일 당일 이후로는 취소할 수 없다 (§4 → 422 CANCEL_CLOSED)."""


def days_until_show(now: datetime, starts_at: datetime) -> int:
    """관람일까지 남은 일수. 시각이 아니라 날짜 경계로 센다 —
    같은 날 오전/오후 예매가 다른 수수료를 맞으면 사용자가 납득하지 못한다."""
    return (starts_at.date() - now.date()).days


def cancel_fee(ticket_amount: int, now: datetime, starts_at: datetime) -> int:
    """취소 수수료(원). 결과는 payments.fee_snapshot 에 그대로 남긴다 (§7.3).

    >>> from datetime import datetime, timezone
    >>> d = lambda day: datetime(2026, 9, day, tzinfo=timezone.utc)
    >>> cancel_fee(100_000, d(1), d(30))   # D-29
    0
    >>> cancel_fee(100_000, d(25), d(30))  # D-5
    10000
    >>> cancel_fee(100_000, d(29), d(30))  # D-1
    20000
    """
    remaining = days_until_show(now, starts_at)
    if remaining < 1:
        raise CancelClosed(f"관람일 당일 이후로는 취소할 수 없습니다 (D{remaining:+d})")

    for threshold, rate in CANCEL_FEE_TIERS:
        if remaining >= threshold:
            return int(Decimal(ticket_amount) * rate)

    raise CancelClosed(f"수수료 구간을 찾을 수 없습니다 (D{remaining:+d})")
