"""쿼리 — 설계 문서: 재고 모델, 좌석 선점, 만료 스윕, 좌석맵 조회 부하

SQLAlchemy Core 표현식으로 쓴다. 문자열 SQL 이 아니라 models.py 의 컬럼과 제약
이름을 직접 참조하므로, 스키마가 바뀌면 이 모듈이 import 되는 순간 깨진다 —
문자열 SQL 은 그 쿼리를 실제로 실행할 때까지 조용하다
(결정 기록: 쿼리 작성 방식).

ORM 세션은 쓰지 않는다. 모델은 컬럼과 제약의 이름표로만 쓰고 실행은 전부 Core 다.
identity map, flush 순서, lazy load 가 끼어들 자리는 이 도메인에 없다.
트랜잭션 경계는 app/service/ 가 갖는다.

각 쿼리는 상수가 아니라 함수다. UPDATE 문에서 바인드 파라미터 이름이 대상 테이블의
컬럼 이름과 겹치면 SQLAlchemy 가 그것을 SET 절 값으로 해석해서 컴파일이 실패한다
(schedule_seats 에는 schedule_id 컬럼이 있다). 값을 인자로 받아 그 자리에서
바인드하면 이름은 SQLAlchemy 가 유일하게 붙이므로 그 문제가 원천적으로 없고,
호출부가 dict 대신 타입이 붙은 키워드 인자를 쓰게 되는 이득이 따라온다.

표현식으로 옮길 때 반드시 지켜야 하는 것이 둘 있다.

  상태값은 리터럴로 박는다   status 를 바인드 파라미터로 넘기면 부분 인덱스
                             ix_hold_expiry (WHERE status = 'HELD') 가 매칭되지
                             않고 Seq Scan 이 된다. 실측했다 — _HELD 주석 참고.
  잠금 절은 테스트로 고정한다  ORDER BY ... FOR UPDATE, SKIP LOCKED, guard 절은
                             이 파일을 읽어서 지키는 것이 아니라
                             tests/test_queries.py 의 단정으로 지킨다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from app.infra.db.models import Order, OrderItem, ScheduleSeat, Seat, Venue

# ══════════════════════════════════════════════════════════════ 리터럴 · 헬퍼

# 좌석 상태 리터럴.
#
# 바인드 파라미터가 아니라 리터럴이어야 한다. status 를 파라미터로 넘기면
# Postgres 가 부분 인덱스의 조건절(status = 'HELD')이 쿼리 조건에서 따라 나온다는
# 것을 증명하지 못하고, 스윕 쿼리가 ix_hold_expiry 대신 Seq Scan 으로 간다.
# 같은 쿼리를 파라미터 버전과 리터럴 버전으로 EXPLAIN 해서 확인했고, 그 단정을
# tests/test_queries.py 에 남겼다.
#
# 상태값은 닫힌 집합이고 사용자 입력이 아니므로 리터럴로 박아도 주입 위험이 없다.
_AVAILABLE = sa.literal_column("'AVAILABLE'")
_HELD = sa.literal_column("'HELD'")
_BOOKED = sa.literal_column("'BOOKED'")
_PAID = sa.literal_column("'PAID'")

_NOW = sa.func.now()


def _bigint_array(values: Sequence[int]) -> sa.BindParameter[Any]:
    """bigint[] 파라미터. asyncpg 에 파이썬 리스트를 넘기려면 타입을 명시해야 한다."""
    return sa.literal(list(values), ARRAY(sa.BigInteger))


def _text_array(values: Sequence[str]) -> sa.BindParameter[Any]:
    return sa.literal(list(values), ARRAY(sa.Text))


def _int_array(values: Sequence[int]) -> sa.BindParameter[Any]:
    return sa.literal(list(values), ARRAY(sa.Integer))


def _seconds(sec: int) -> Any:
    """초 단위 interval. make_interval 의 인자는 (년,월,주,일,시,분,초) 순서다.

    Postgres 의 이름 붙인 인자 문법(secs => :n)은 표현식으로 표현할 수 없어서
    위치 인자로 쓴다. 앞의 0 여섯 개가 그 순서 때문이다.
    """
    return sa.func.make_interval(0, 0, 0, 0, 0, 0, sa.literal(sec, sa.Integer))


def _claimable() -> sa.ColumnElement[bool]:
    """지금 잡을 수 있는 좌석의 조건.

    만료된 hold 를 포함하는 것이 핵심이다. 스윕 워커는 1s tick 이므로 만료와
    회수 사이에 창이 있고, 그 창에서도 좌석은 팔려야 한다.
    """
    return sa.or_(
        ScheduleSeat.status == _AVAILABLE,
        sa.and_(
            ScheduleSeat.status == _HELD,
            ScheduleSeat.hold_expires_at < _NOW,
        ).self_group(),
    )


# ══════════════════════════════════════════════════════════════ 좌석 전개 (재고 모델)


def insert_venue(*, name: str, address: str) -> sa.Insert:
    return sa.insert(Venue).values(name=name, address=address).returning(Venue.id)


def create_venue_seats(
    *,
    venue_id: int,
    zones: Sequence[str],
    row_labels: Sequence[str],
    widths: Sequence[int],
) -> sa.Insert:
    """공연장 물리 좌석 생성. 열 정의를 배열로 받아 generate_series 로 전개한다.

    1,200번의 개별 INSERT 는 그 자체가 나중의 병목 재현을 방해한다 —
    한 문장으로 끝낸다.
    """
    layout = (
        sa.func.unnest(_text_array(zones), _text_array(row_labels), _int_array(widths))
        .table_valued("zone", "row_label", "width")
        .render_derived(name="layout")
    )
    numbers = (
        sa.func.generate_series(1, layout.c.width)
        .table_valued("col_no")
        .render_derived(name="g")
        .lateral()
    )
    source = (
        sa.select(
            sa.literal(venue_id, sa.BigInteger),
            layout.c.zone,
            layout.c.row_label,
            numbers.c.col_no,
        )
        .select_from(layout)
        .join(numbers, sa.true())
    )
    return (
        pg_insert(Seat)
        .from_select(["venue_id", "zone", "row_label", "col_no"], source)
        # 좌석 위치 유니크 제약이 멱등성을 준다. 제약을 이름으로 가리키므로
        # models.py 에서 이름이 바뀌면 여기가 먼저 깨진다.
        .on_conflict_do_nothing(constraint="uq_seat_position")
        .returning(Seat.id)
    )


def expand_schedule_seats(
    *,
    schedule_id: int,
    venue_id: int,
    zones: Sequence[str],
    row_labels: Sequence[str],
    grades: Sequence[str],
    prices: Sequence[int],
) -> sa.Insert:
    """회차 오픈 시 재고 행을 전개한다 (재고 모델). schedules × seats → schedule_seats.

    ON CONFLICT DO NOTHING 이 이 문장을 멱등하게 만든다. 회차 오픈 처리가 두 번
    실행되거나 재시도되어도 재고가 늘어나지 않는다 — uq_schedule_seat 가 받아낸다.
    """
    grade_price = (
        sa.func.unnest(
            _text_array(zones),
            _text_array(row_labels),
            _text_array(grades),
            _int_array(prices),
        )
        .table_valued("zone", "row_label", "grade", "price")
        .render_derived(name="gp")
    )
    source = (
        sa.select(
            sa.literal(schedule_id, sa.BigInteger),
            Seat.id,
            grade_price.c.grade,
            grade_price.c.price,
        )
        .select_from(Seat)
        .join(
            grade_price,
            sa.and_(
                grade_price.c.zone == Seat.zone,
                grade_price.c.row_label == Seat.row_label,
            ),
        )
        .where(Seat.venue_id == venue_id)
    )
    return (
        pg_insert(ScheduleSeat)
        .from_select(["schedule_id", "seat_id", "grade", "price"], source)
        .on_conflict_do_nothing(constraint="uq_schedule_seat")
        .returning(ScheduleSeat.id)
    )


# ══════════════════════════════════════════════════════════════ 좌석 선점


def lock_user_quota(*, quota_key: str) -> sa.Select[Any]:
    """(회차, 사용자) 단위 어드바이저리 락. hold_seats 직전에 같은 트랜잭션에서 잡는다.

    왜 필요한가: hold_seats 의 owned 절은 "이 유저가 이 회차에 몇 석 갖고 있나"를
    세는 술어(predicate) 다. 행 잠금은 target 좌석에만 걸리므로, 서로 다른
    좌석을 노리는 동시 요청들은 아무것도 공유하지 않고 각자 owned=0 을 읽는다.
    READ COMMITTED 에서 전형적인 write skew 이고, 실제로 4매 한도가 7매로 새는 것을
    테스트로 재현했다 (test_quota_is_enforced_under_concurrency).

    셀 행이 아예 없을 수도 있는 술어는 SELECT ... FOR UPDATE 로 잠글 수 없다(phantom).
    SERIALIZABLE 로 올리면 정확하지만 경합 경로 전체에 재시도 루프가 붙는다.
    어드바이저리 락은 (회차, 사용자) 하나만 직렬화하므로 좌석 경합 경로에는 영향이 없다.
    커밋/롤백 시 자동 해제되고, 해시 충돌은 불필요한 직렬화일 뿐 오답이 아니다.
    """
    return sa.select(
        sa.func.pg_advisory_xact_lock(
            sa.cast(sa.func.hashtext(sa.literal(quota_key, sa.Text)), sa.BigInteger)
        )
    )


def hold_seats(
    *,
    schedule_id: int,
    user_id: int,
    seat_ids: Sequence[int],
    max_seats: int,
    hold_ttl_sec: int,
) -> sa.Update:
    """좌석 선점. 요청 좌석 전량을 잡거나 0석이다 (원칙 "부분 성공은 없다").

    애플리케이션에 분기가 없다는 게 핵심이다. "3석은 됐고 1석은 안 됨" 이라는
    중간 상태가 만들어질 수 있는 코드 경로 자체가 존재하지 않는다.
    """
    wanted = _bigint_array(seat_ids)
    requested = sa.func.cardinality(wanted)

    # 회차당 보유 매수 (정책 상수 MAX_SEATS_PER_ORDER).
    # 유효한 hold 와 이미 결제된 좌석을 함께 센다. 만료된 hold 는 세지 않는다.
    owned = (
        sa.select(sa.func.count().label("n"))
        .select_from(ScheduleSeat)
        .outerjoin(OrderItem, OrderItem.schedule_seat_id == ScheduleSeat.id)
        .outerjoin(
            Order, sa.and_(Order.id == OrderItem.order_id, Order.status == _PAID)
        )
        .where(
            ScheduleSeat.schedule_id == schedule_id,
            sa.or_(
                sa.and_(
                    ScheduleSeat.status == _HELD,
                    ScheduleSeat.held_by == user_id,
                    ScheduleSeat.hold_expires_at > _NOW,
                ).self_group(),
                sa.and_(
                    ScheduleSeat.status == _BOOKED, Order.user_id == user_id
                ).self_group(),
            ),
        )
        .cte("owned")
    )

    # 잡을 대상. 여기서 잠금 순서가 정해진다 —
    # ORDER BY seat_id 로 고정해 교차 요청 간 데드락을 피한다.
    target = (
        sa.select(ScheduleSeat.id)
        .where(
            ScheduleSeat.schedule_id == schedule_id,
            ScheduleSeat.seat_id == sa.any_(wanted),
            _claimable(),
        )
        .order_by(ScheduleSeat.seat_id)
        .with_for_update()
        .cte("target")
    )

    # 전량 확보 여부와 구매 한도를 SQL 안에서 판정한다. 밖에서 세면 세는 시점과
    # 쓰는 시점 사이에 남이 끼어든다 (원칙 "모든 상태 전이는 조건부 쓰기").
    guard = sa.select(
        sa.and_(
            sa.select(sa.func.count()).select_from(target).scalar_subquery()
            == requested,
            sa.select(owned.c.n).scalar_subquery() + requested <= max_seats,
        ).label("ok")
    ).cte("guard")

    return (
        sa.update(ScheduleSeat)
        .where(
            ScheduleSeat.id.in_(sa.select(target.c.id)),
            # 하나라도 모자라거나 한도를 넘으면 0행.
            #
            # 스칼라 서브쿼리로 읽는다. guard 를 UPDATE 의 FROM 에 올리면 결과는
            # 같지만 1행 CTE 와의 교차 조인이 되고, SQLAlchemy 의 카테시안 곱
            # 경고가 매 실행마다 뜬다 — 의도한 교차 조인과 실수를 구분할 수
            # 없게 되는 쪽이 손해다.
            sa.select(guard.c.ok).scalar_subquery(),
        )
        .values(
            status=_HELD,
            held_by=user_id,
            hold_expires_at=_NOW + _seconds(hold_ttl_sec),
            updated_at=_NOW,
        )
        .returning(
            ScheduleSeat.seat_id,
            ScheduleSeat.grade,
            ScheduleSeat.price,
            ScheduleSeat.hold_expires_at,
        )
    )


def find_unavailable_seats(
    *, schedule_id: int, seat_ids: Sequence[int]
) -> sa.Select[Any]:
    """실패 응답의 unavailable_seat_ids 를 채우기 위한 조회 (API 스펙).

    클라이언트가 좌석맵 전체를 다시 받지 않고 그 좌석만 회색으로 칠할 수 있게 한다.
    """
    return sa.select(ScheduleSeat.seat_id).where(
        ScheduleSeat.schedule_id == schedule_id,
        ScheduleSeat.seat_id == sa.any_(_bigint_array(seat_ids)),
        sa.not_(_claimable()),
    )


def release_hold(
    *, schedule_id: int, user_id: int, seat_ids: Sequence[int]
) -> sa.Update:
    """본인 hold 해제 (좌석 변경 시).

    남의 hold 는 건드릴 수 없다 — held_by 조건이 그것이다.
    """
    return (
        sa.update(ScheduleSeat)
        .where(
            ScheduleSeat.schedule_id == schedule_id,
            ScheduleSeat.seat_id == sa.any_(_bigint_array(seat_ids)),
            ScheduleSeat.status == _HELD,
            ScheduleSeat.held_by == user_id,
        )
        .values(status=_AVAILABLE, held_by=None, hold_expires_at=None, updated_at=_NOW)
        .returning(ScheduleSeat.seat_id)
    )


# ══════════════════════════════════════════════════════════════ 만료 스윕


def sweep_expired_holds(*, batch: int) -> sa.Update:
    """1s tick. 클라이언트가 브라우저를 닫아버린 경우의 유일한 회수 수단이다."""
    expired = (
        sa.select(ScheduleSeat.id)
        .where(ScheduleSeat.status == _HELD, ScheduleSeat.hold_expires_at < _NOW)
        .order_by(ScheduleSeat.hold_expires_at)
        # 한 tick 처리 상한. 긴 트랜잭션 방지.
        .limit(batch)
        # 워커 N대가 서로 기다리지 않는다.
        .with_for_update(skip_locked=True)
    )
    return (
        sa.update(ScheduleSeat)
        .where(ScheduleSeat.id.in_(expired))
        .values(status=_AVAILABLE, held_by=None, hold_expires_at=None, updated_at=_NOW)
        # 좌석맵 캐시 무효화 대상.
        .returning(ScheduleSeat.schedule_id, ScheduleSeat.seat_id)
    )


# ══════════════════════════════════════════════════════════════ 좌석맵 · 검증

# 별칭을 ss / s 로 준 것은 두 테이블이 같은 이름의 컬럼을 여러 개 갖기 때문이다.
# 별칭이 없으면 어느 쪽 seat_id 인지 읽는 사람이 매번 되짚어야 한다.
_SS = aliased(ScheduleSeat, name="ss")
_S = aliased(Seat, name="s")


def seatmap(*, schedule_id: int) -> sa.Select[Any]:
    """회차 단위 전량 조회 (좌석맵 조회 부하).

    ix_seatmap 의 INCLUDE 로 힙 접근 없이 인덱스에서 끝난다.
    """
    return (
        sa.select(
            _SS.seat_id,
            _S.zone,
            _S.row_label,
            _S.col_no,
            _SS.grade,
            _SS.price,
            _SS.status,
        )
        .join_from(_SS, _S, _S.id == _SS.seat_id)
        .where(_SS.schedule_id == schedule_id)
        .order_by(_S.zone, _S.row_label, _S.col_no)
    )


def seat_status_counts(*, schedule_id: int) -> sa.Select[Any]:
    """총량 보존 불변식 (검증 시나리오 마지막 줄). 전체 테스트 뒤에 항상 붙인다."""
    return (
        sa.select(
            sa.cast(ScheduleSeat.status, sa.Text).label("status"),
            sa.func.count().label("n"),
        )
        .where(ScheduleSeat.schedule_id == schedule_id)
        .group_by(ScheduleSeat.status)
    )
