"""서비스 계층이 주고받는 데이터 모양 — 설계 문서: API 스펙

유스케이스의 입력과 결과가 여기 모여 있다. 표준 라이브러리만 import 한다 —
DB 도 Redis 도 HTTP 도 모르므로, 이 파일은 어느 계층에서든 import 해도
의존성이 한 방향으로만 흐른다. HTTP 계층은 여기서 응답 모양을 가져가고,
서비스는 여기 있는 값을 만들어 돌려준다.

쓰기 모델(app/infra/db/models.py)과 다른 모양인 것이 요점이다. 모델은 스키마를
말하고, 여기 있는 것은 유스케이스가 주고받는 값을 말한다. 서비스가 ORM 모델이나
Row 를 그대로 흘려보내면 스키마 변경이 호출부까지 번진다.

이름 규칙은 CLAUDE.md 에 있다 — 결과는 받는 것을 가리키는 명사, 그 원소는 단수
명사, 입력은 -Spec, -View 는 쓰지 않는다.

여기 오지 않는 것도 규칙이다.

  예외          HoldRejected, PaymentUnknown 같은 것은 데이터가 아니라 제어
                흐름이다. 각 유스케이스 모듈에 남는다
  어댑터 결과   pg/base.PayAttempt 는 infra 의 값이다. 여기로 옮기면 infra 가
                service 를 import 하게 되어 의존 방향이 뒤집힌다
  핸들          redis/client.Gate 는 keep() 이라는 행동이 있고 가변이다.
                DTO 가 아니므로 계층 사이로 넘기지 않는다
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# ══════════════════════════════════════════════════════════════ 입력 (-Spec)


@dataclass(frozen=True, slots=True)
class RowSpec:
    """공연장 열 하나의 정의 — 좌석 전개의 **입력**이다.

    실제 공연장은 열마다 폭이 다르다. 이름이 -Spec 인 것은 입력임을 드러내려는
    것이고, 결과 DTO(Seatmap, SeatCell 등)와 반대 방향의 값이다.
    """

    zone: str
    row_label: str
    width: int
    grade: str
    price: int


@dataclass(frozen=True, slots=True)
class VenueLayout:
    """공연장 하나의 좌석 배치 — 입력. RowSpec 여럿을 묶는다.

    파생값을 계산하는 접근자가 있으므로 엄밀히는 순수한 DTO 가 아니라 값 객체다.
    다만 계산이 자기 필드만 보고, 규칙이 아니라 합계이므로 여기 둔다 — 정책은
    app/domain/policy.py 가 갖는다.
    """

    name: str
    address: str
    rows: tuple[RowSpec, ...]

    @property
    def total_seats(self) -> int:
        return sum(r.width for r in self.rows)

    def count_by_grade(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.grade] = out.get(r.grade, 0) + r.width
        return out


# ══════════════════════════════════════════════════════════════ 좌석 선점 결과


@dataclass(frozen=True, slots=True)
class HeldSeat:
    """선점된 좌석 하나 — 결과 DTO 의 원소."""

    seat_id: int
    grade: str
    price: int


@dataclass(frozen=True, slots=True)
class Hold:
    """선점 결과. hold_service.acquire() 가 돌려주는 값이다."""

    schedule_id: int
    user_id: int
    seats: tuple[HeldSeat, ...]
    expires_at: datetime

    @property
    def total_amount(self) -> int:
        return sum(s.price for s in self.seats)


# ══════════════════════════════════════════════════════════════ 좌석맵 조회 결과


@dataclass(frozen=True, slots=True)
class SeatCell:
    """좌석맵의 한 칸 — 읽기 모델의 원소.

    클라이언트가 그대로 그릴 수 있는 형태다. models.ScheduleSeat(쓰기 모델)와
    다른 모양인 것이 요점이다 — 두 테이블을 조인하고 라벨까지 조립한 값이다.
    """

    seat_id: int
    #: 사람이 읽는 좌석 이름. 서버가 만든다 — 좌석 번호 규칙이 공연장마다
    #: 다르고, 클라이언트 세 곳에서 각자 조립하면 세 곳이 다르게 틀린다.
    label: str
    grade: str
    price: int
    status: str


@dataclass(frozen=True, slots=True)
class Seatmap:
    """좌석맵 조회의 응답 봉투.

    데이터(seats)와 응답 메타(etag, from_cache)를 같이 담는다. 엄밀히는 순수한
    읽기 모델이 아니지만 의도한 것이다 — etag 는 내용이 있는 자리에서 계산해야
    결정적이고, HTTP 계층은 (etag, seats)를 한 번에 받아야 304 를 판정할 수 있다.
    """

    schedule_id: int
    #: 같은 내용이면 같은 값. HTTP 계층이 304 를 판정하는 근거다.
    etag: str
    seats: tuple[SeatCell, ...]
    #: 캐시에서 답했는가. 정확성과는 무관하고 관측용이다.
    from_cache: bool


# ══════════════════════════════════════════════════════════════ 주문 사가 결과


@dataclass(frozen=True, slots=True)
class PlacedOrder:
    """주문 결과. snapshot 이 HTTP 응답 본문이 된다.

    snapshot 을 따로 들고 있는 이유는 멱등성이다 — 같은 키의 재시도에 최초
    응답을 그대로 재생해야 하므로, 그 값이 DB 에 저장된 형태 그대로여야 한다.
    """

    order_id: int
    status: str
    snapshot: dict[str, Any]
    #: 저장된 스냅샷을 재생한 것인가. 정확성과 무관하고 관측용이다.
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class CanceledOrder:
    """취소 결과. order_service.cancel() 이 돌려주는 값이다."""

    order_id: int
    #: 취소 수수료(원). policy.cancel_fee() 의 결과이며 payments 에 스냅샷으로 남는다.
    fee: int
    refunded: bool
