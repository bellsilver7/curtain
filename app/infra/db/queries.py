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

from app.infra.db.models import (
    Order,
    OrderItem,
    Outbox,
    Payment,
    Schedule,
    ScheduleSeat,
    Seat,
    Venue,
)

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


# ══════════════════════════════════════════════════════════════ 결제 사가

# 주문 상태 리터럴. 좌석 상태와 같은 이유로 리터럴이다 (위 _HELD 주석 참고).
_PENDING = sa.literal_column("'PENDING'")
_CANCELED = sa.literal_column("'CANCELED'")

# 결제 시도 상태.
_REQUESTED = sa.literal_column("'REQUESTED'")
_APPROVED = sa.literal_column("'APPROVED'")
_REFUNDED = sa.literal_column("'REFUNDED'")
_PAID_LITERAL = _PAID
_FAILED = sa.literal_column("'FAILED'")


def held_seats_for_order(
    *, schedule_id: int, user_id: int, seat_ids: Sequence[int]
) -> sa.Select[Any]:
    """hold 현황. **실패를 설명하기 위한 조회다.**

    판정은 open_order() 한 문장이 한다. 이 조회는 그것이 0행을 돌려줬을 때
    "몇 석이 유효했고 잔여가 얼마였는지"를 사용자에게 말해주기 위한 것이므로,
    판정 근거로 쓰면 안 된다 — 읽는 시점과 쓰는 시점이 갈리는 순간 그 사이에
    남이 끼어든다.
    """
    return (
        sa.select(
            ScheduleSeat.id,
            ScheduleSeat.seat_id,
            ScheduleSeat.grade,
            ScheduleSeat.price,
            ScheduleSeat.hold_expires_at,
            # 잔여를 DB 가 계산해서 준다. 애플리케이션이 now() 를 찍어 빼면
            # API·워커·DB 의 시계 차이가 그대로 판정에 섞인다.
            (ScheduleSeat.hold_expires_at - _NOW).label("remaining"),
        )
        .where(
            ScheduleSeat.schedule_id == schedule_id,
            ScheduleSeat.seat_id == sa.any_(_bigint_array(seat_ids)),
            ScheduleSeat.status == _HELD,
            ScheduleSeat.held_by == user_id,
            ScheduleSeat.hold_expires_at > _NOW,
        )
        .order_by(ScheduleSeat.seat_id)
    )


def open_order(
    *,
    schedule_id: int,
    user_id: int,
    seat_ids: Sequence[int],
    idempotency_key: str,
    min_hold_remaining_sec: int,
) -> sa.Insert:
    """PENDING 주문 생성. 사가의 유일한 진입 문장이다.

    선행 조건 전부를 이 한 문장에 넣는다 — 유효한 내 hold 인지, 전량인지,
    승인 왕복을 버틸 잔여가 있는지, 그리고 같은 멱등키가 없는지. 밖에서 확인하고
    나중에 쓰면 그 사이에 남이 끼어든다 (원칙 "모든 상태 전이는 조건부 쓰기").

    장치가 셋이다.

      HAVING count(*) = cardinality(...)   전량 아니면 0행. 부분 주문은 만들어질
                                           수 있는 코드 경로 자체가 없다
      hold_expires_at > now() + 잔여       잔여 검사를 시각 비교로 바꾼다.
                                           애플리케이션 시계가 끼어들지 않는다
      ON CONFLICT DO NOTHING               멱등성 1겹. 같은 키의 동시 요청 중
                                           한 건만 행을 만든다

    **0행의 뜻이 두 가지**라는 것이 이 설계의 값이다. hold 가 없었거나, 같은 키가
    이미 있었거나. 호출부는 그 둘을 멱등키 조회 하나로 가르므로 분기가 하나뿐이고,
    그 하나는 두 경로 모두에서 실행된다 — 테스트가 닿지 않는 방어 코드가 남지 않는다.

    금액도 DB 가 정한다. 클라이언트가 보낸 금액을 믿으면 결제 금액 조작이 된다.
    """
    wanted = _bigint_array(seat_ids)
    source = (
        sa.select(
            sa.literal(schedule_id, sa.BigInteger),
            sa.literal(user_id, sa.BigInteger),
            wanted,
            sa.func.sum(ScheduleSeat.price),
            sa.literal(idempotency_key, sa.Text),
        )
        .where(
            ScheduleSeat.schedule_id == schedule_id,
            ScheduleSeat.seat_id == sa.any_(wanted),
            ScheduleSeat.status == _HELD,
            ScheduleSeat.held_by == user_id,
            ScheduleSeat.hold_expires_at > _NOW + _seconds(min_hold_remaining_sec),
        )
        .having(sa.func.count() == sa.func.cardinality(wanted))
    )
    return (
        pg_insert(Order)
        .from_select(
            ["schedule_id", "user_id", "seat_ids", "total_amount", "idempotency_key"],
            source,
        )
        .on_conflict_do_nothing(constraint="uq_orders_idempotency_key")
        .returning(Order.id, Order.total_amount)
    )


def order_by_idempotency_key(*, idempotency_key: str) -> sa.Select[Any]:
    """중복 요청이 재생할 응답을 찾는다."""
    return sa.select(
        Order.id, sa.cast(Order.status, sa.Text).label("status"), Order.response_snapshot
    ).where(Order.idempotency_key == idempotency_key)


def order_for_saga(*, order_id: int) -> sa.Select[Any]:
    """사가가 주문 하나를 다룰 때 필요한 전부.

    schedules 를 조인하는 이유는 취소 수수료가 관람일시 기준이기 때문이다
    (취소와 환불). 주문만 읽고 나중에 회차를 또 조회하면, 그 사이에 회차가
    변경되는 경우를 생각해야 한다.
    """
    return (
        sa.select(
            Order.id,
            Order.user_id,
            Order.schedule_id,
            sa.cast(Order.status, sa.Text).label("status"),
            Order.seat_ids,
            Order.total_amount,
            Order.response_snapshot,
            Schedule.starts_at,
        )
        .join_from(Order, Schedule, Schedule.id == Order.schedule_id)
        .where(Order.id == order_id)
    )


def mark_order_paid(*, order_id: int) -> sa.Update:
    """확정의 첫 문장. 멱등성 2겹 (멱등성 세 겹).

    WHERE status = 'PENDING' 이 전부다. 두 번째 호출은 0행을 갱신하고 조용히
    끝나므로, 웹훅 재전송과 리컨실러와 동기 응답이 서로를 모르면서도 안전하다.
    0행은 에러가 아니라 "이미 다른 경로가 처리했다"는 정상 종료다.

    응답 스냅샷은 set_order_snapshot() 이 같은 트랜잭션에서 쓴다. 확정할 좌석이
    무엇인지는 이 문장 다음에 알게 되므로 한 문장으로는 묶을 수 없다.
    """
    return (
        sa.update(Order)
        .where(Order.id == order_id, Order.status == _PENDING)
        .values(status=_PAID_LITERAL, paid_at=_NOW)
        .returning(Order.id)
    )


#: order_status ENUM 의 값. literal_column 에 문자열을 끼워 넣으므로,
#: 그 문자열이 이 집합 안에 있다는 것을 확인한 뒤에만 쓴다 — 상태값은 내부
#: 어휘이고 사용자 입력이 아니지만, 그 전제가 깨지면 주입이 되므로 검사로 고정한다.
_ORDER_STATUSES = frozenset({"PENDING", "PAID", "CANCELED", "FAILED"})
_PAY_STATUSES = frozenset({"REQUESTED", "APPROVED", "FAILED", "REFUNDED"})


def set_order_snapshot(*, order_id: int, snapshot: dict[str, Any]) -> sa.Update:
    """응답 스냅샷을 쓴다. 확정 트랜잭션 안에서만 호출한다.

    상태 조건이 없는 것은 의도다 — 이 문장은 같은 트랜잭션에서 방금
    PENDING → PAID 를 성공시킨 뒤에만 실행되므로, 조건을 또 붙이면
    (이미 PAID 이므로) 0행이 되어 스냅샷이 영원히 비어 있게 된다.
    """
    return (
        sa.update(Order)
        .where(Order.id == order_id)
        .values(response_snapshot=snapshot)
        .returning(Order.id)
    )


def mark_order(*, order_id: int, status: str, expect: str) -> sa.Update:
    """조건부 상태 전이 하나. 전이 표는 app/domain/order.py 가 갖는다.

    expect 를 인자로 받는 것이 요점이다. 기대 상태 없는 UPDATE 는 리뷰에서
    거절한다 (원칙 "모든 상태 전이는 조건부 쓰기").
    """
    if status not in _ORDER_STATUSES or expect not in _ORDER_STATUSES:
        raise ValueError(f"order_status 값이 아니다: {status!r} / {expect!r}")
    values: dict[str, Any] = {"status": sa.literal_column(f"'{status}'")}
    if status == "CANCELED":
        values["canceled_at"] = _NOW
    return (
        sa.update(Order)
        .where(Order.id == order_id, Order.status == sa.literal_column(f"'{expect}'"))
        .values(**values)
        .returning(Order.id)
    )


def claim_order_items(
    *, order_id: int, schedule_id: int, user_id: int, seat_ids: Sequence[int]
) -> sa.Insert:
    """확정의 두 번째 문장 — 좌석을 이 주문에 붙인다 (확정 트랜잭션).

    조건이 만료된 hold 를 걸러낸다. 승인 왕복 중에 hold 가 만료됐다면 여기서
    행이 모자라고, 그때는 커밋하지 않고 환불해야 한다 — 만료된 hold 로 확정하면
    이미 남에게 팔릴 수 있었던 좌석을 뒤늦게 가져가는 것이 된다.

    ON CONFLICT 를 쓰지 않는다. order_items.schedule_seat_id 의 UNIQUE 위반은
    멱등성 3겹의 최후 방어선이고, 그 예외가 올라와야 롤백 후 환불로 이어진다.
    조용히 무시하면 이중 판매가 조용히 성공한다.
    """
    source = (
        sa.select(
            sa.literal(order_id, sa.BigInteger), ScheduleSeat.id, ScheduleSeat.price
        )
        .where(
            ScheduleSeat.schedule_id == schedule_id,
            ScheduleSeat.seat_id == sa.any_(_bigint_array(seat_ids)),
            ScheduleSeat.status == _HELD,
            ScheduleSeat.held_by == user_id,
            ScheduleSeat.hold_expires_at > _NOW,
        )
        .order_by(ScheduleSeat.seat_id)
    )
    return (
        sa.insert(OrderItem)
        .from_select(["order_id", "schedule_seat_id", "price"], source)
        .returning(OrderItem.schedule_seat_id)
    )


def book_order_seats(*, order_id: int) -> sa.Update:
    """확정의 세 번째 문장 — 좌석을 BOOKED 로.

    hold 메타데이터를 반드시 비운다. ck_schedule_seats_hold_shape 가
    "HELD 가 아니면 held_by 와 hold_expires_at 은 NULL" 을 요구하므로,
    빠뜨리면 DB 가 행을 거부한다 — 제약이 이 실수를 대신 잡아준다.
    """
    mine = sa.select(OrderItem.schedule_seat_id).where(OrderItem.order_id == order_id)
    return (
        sa.update(ScheduleSeat)
        .where(ScheduleSeat.id.in_(mine), ScheduleSeat.status == _HELD)
        .values(
            status=_BOOKED, held_by=None, hold_expires_at=None, updated_at=_NOW
        )
        .returning(ScheduleSeat.seat_id)
    )


def restore_order_seats(*, order_id: int) -> sa.Update:
    """취소 확정 후 좌석 복원 (취소와 환불).

    환불 성공을 확인한 뒤에만 부른다. 순서를 뒤집으면 환불이 실패했는데 좌석은
    이미 남에게 팔려 되돌릴 수 없다.
    """
    mine = sa.select(OrderItem.schedule_seat_id).where(OrderItem.order_id == order_id)
    return (
        sa.update(ScheduleSeat)
        .where(ScheduleSeat.id.in_(mine), ScheduleSeat.status == _BOOKED)
        .values(status=_AVAILABLE, updated_at=_NOW)
        .returning(ScheduleSeat.seat_id)
    )


def insert_outbox(*, topic: str, payload: dict[str, Any]) -> sa.Insert:
    """확정 트랜잭션과 같이 커밋된다 (확정 트랜잭션).

    "좌석은 잡혔는데 알림톡이 안 갔다"가 원천적으로 생기지 않는다. 발행은
    별도 워커가 published_at 이 NULL 인 행을 훑어서 한다.
    """
    return sa.insert(Outbox).values(topic=topic, payload=payload).returning(Outbox.id)


def insert_payment(
    *,
    order_id: int,
    status: str,
    amount: int,
    pg_tid: str | None = None,
    fee_snapshot: dict[str, Any] | None = None,
) -> sa.Insert:
    """결제 시도 기록. 실패한 시도도 남긴다 — 정산 분쟁의 유일한 근거다.

    status 는 pay_status ENUM 값이다: REQUESTED · APPROVED · FAILED · REFUNDED.
    """
    if status not in _PAY_STATUSES:
        raise ValueError(f"pay_status 값이 아니다: {status!r}")
    values: dict[str, Any] = {
        "order_id": order_id,
        "status": sa.literal_column(f"'{status}'"),
        "amount": amount,
        "pg_tid": pg_tid,
        "fee_snapshot": fee_snapshot,
    }
    if status == "APPROVED":
        values["approved_at"] = _NOW
    elif status == "REFUNDED":
        values["refunded_at"] = _NOW
    return sa.insert(Payment).values(**values).returning(Payment.id)


def approved_payment(*, order_id: int) -> sa.Select[Any]:
    """이 주문의 승인된 결제. 환불 요청의 대상(pg_tid)을 여기서 얻는다."""
    return (
        sa.select(Payment.id, Payment.pg_tid, Payment.amount)
        .where(Payment.order_id == order_id, Payment.status == _APPROVED)
        .order_by(Payment.id.desc())
        .limit(1)
    )


def promote_payment_to_approved(*, order_id: int, pg_tid: str) -> sa.Update:
    """REQUESTED 시도를 승인으로 올린다.

    조건부 전이다. 웹훅과 리컨실러가 같은 주문을 동시에 확정하려 하면 한쪽만
    0행을 받는다 — 그쪽은 결제 행을 새로 만들지 않고 넘어가야 한다.
    같은 pg_tid 로 두 행을 만들면 payments.pg_tid UNIQUE 가 거부한다.
    """
    return (
        sa.update(Payment)
        .where(Payment.order_id == order_id, Payment.status == _REQUESTED)
        .values(status=_APPROVED, pg_tid=pg_tid, approved_at=_NOW)
        .returning(Payment.id)
    )


def fail_pending_payments(*, order_id: int) -> sa.Update:
    """결론이 난 주문의 남은 REQUESTED 시도를 실패로 닫는다.

    남겨두면 리컨실러 입장에서 "승인 요청은 있는데 결론이 없는" 행이 영원히
    남는다. 지우지 않고 FAILED 로 닫는 것은 시도 이력을 정산 근거로 쓰기 때문이다.
    """
    return (
        sa.update(Payment)
        .where(Payment.order_id == order_id, Payment.status == _REQUESTED)
        .values(status=_FAILED)
        .returning(Payment.id)
    )


def unresolved_orders(*, older_than_sec: float, batch: int) -> sa.Select[Any]:
    """리컨실러가 되물을 주문 (배치 워커).

    ix_orders_pending 부분 인덱스를 탄다 — status 를 리터럴로 박은 이유가 그것이다.

    FOR UPDATE SKIP LOCKED 로 워커 여러 대가 같은 주문을 잡지 않게 한다.
    같은 주문을 둘이 확정하려 해도 조건부 전이 때문에 결과는 맞지만, PG사에
    두 번 되묻는 것은 그냥 낭비다.
    """
    return (
        sa.select(Order.id)
        .where(
            Order.status == _PENDING,
            Order.created_at < _NOW - _seconds(int(older_than_sec)),
        )
        .order_by(Order.created_at)
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
