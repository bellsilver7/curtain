"""SQLAlchemy 모델 — 스키마의 단일 원천 (설계 문서 §3)

이 파일은 **스키마 정의 전용**이다. 쿼리가 이 모델을 통해 나가지는 않는다 —
선점·스윕·확정 SQL은 `app/infra/db/queries.py` 의 raw SQL이고, 그 쿼리들의
정확한 형태가 곧 설계다 (§5.2, §5.4, §7.1). 모델은 두 가지 역할만 한다.

  1. Alembic autogenerate 가 diff를 뜨는 대상
  2. 컬럼·제약·인덱스가 한눈에 보이는 문서

Postgres 전용 기능을 SQLAlchemy 로 표현하는 방법이 여기 다 들어 있다.

  네이티브 ENUM    sa.Enum(..., name=...)
  부분 인덱스      Index(..., postgresql_where=...)
  커버링 인덱스    Index(..., postgresql_include=[...])
  복합 CHECK       CheckConstraint(...)

autogenerate 는 이것들을 **최초 생성**은 제대로 해주지만 **이후 변경 감지**는
불완전하다. 주의사항은 docs/adr/0002-migrations.md 에 정리해 두었다.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 이름 없는 제약은 DB마다 다른 이름을 갖게 되고, 그때부터 마이그레이션이
# 손댈 수 없어진다. 규칙을 고정해 두는 것이 Alembic 사용의 전제 조건이다.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


# ---------------------------------------------------------------- ENUM 타입
# 모델에서는 평범한 sa.Enum 으로 선언하고, 타입의 수명(CREATE TYPE / DROP TYPE)은
# 마이그레이션에서 명시적으로 관리한다. autogenerate 가 ENUM 값 추가를
# ALTER TYPE 으로 잡아주지 못하므로, 어차피 손으로 볼 지점이다.
# (create_type= 은 sa.Enum 이 아니라 postgresql.ENUM 전용 인자다.)

SeatStatus = sa.Enum("AVAILABLE", "HELD", "BOOKED", name="seat_status")
OrderStatus = sa.Enum("PENDING", "PAID", "CANCELED", "FAILED", name="order_status")
PayStatus = sa.Enum("REQUESTED", "APPROVED", "FAILED", "REFUNDED", name="pay_status")


def _ts() -> sa.DateTime:
    """timestamptz. 예매는 관람일시가 전부인 도메인이라 naive datetime은 금지."""
    return sa.DateTime(timezone=True)


# ---------------------------------------------------------------- 사용자 · 장소


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=sa.func.now())


class Venue(Base):
    __tablename__ = "venues"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    address: Mapped[str] = mapped_column(sa.Text, nullable=False)


class Seat(Base):
    """물리 좌석. 공연장에 고정이며 회차와 무관하다 (§11: 다중 공연장은 미룸)."""

    __tablename__ = "seats"
    __table_args__ = (
        sa.UniqueConstraint("venue_id", "zone", "row_label", "col_no", name="uq_seat_position"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    venue_id: Mapped[int] = mapped_column(sa.ForeignKey("venues.id"), nullable=False)
    zone: Mapped[str] = mapped_column(sa.Text, nullable=False)       # 1층 / 2층 / 발코니
    row_label: Mapped[str] = mapped_column(sa.Text, nullable=False)  # A, B, C …
    col_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)


# ---------------------------------------------------------------- 공연 · 회차


class Performance(Base):
    __tablename__ = "performances"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    venue_id: Mapped[int] = mapped_column(sa.ForeignKey("venues.id"), nullable=False)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    running_time_min: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    age_limit: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")


class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (
        sa.UniqueConstraint("performance_id", "starts_at", name="uq_schedule"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    performance_id: Mapped[int] = mapped_column(sa.ForeignKey("performances.id"), nullable=False)
    #: 관람일시. 취소 수수료 계산의 기준 (§7.3)
    starts_at: Mapped[datetime] = mapped_column(_ts(), nullable=False)
    #: 이 시각 전 요청은 425 Too Early (§6.1)
    sale_opens_at: Mapped[datetime] = mapped_column(_ts(), nullable=False)


# ---------------------------------------------------------------- 주문


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        # 리컨실러가 훑는 대상은 PENDING 주문뿐이다 (§2.1).
        sa.Index(
            "ix_orders_pending",
            "created_at",
            postgresql_where=sa.text("status = 'PENDING'"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), nullable=False)
    schedule_id: Mapped[int] = mapped_column(sa.ForeignKey("schedules.id"), nullable=False)
    status: Mapped[str] = mapped_column(OrderStatus, nullable=False, server_default="PENDING")
    total_amount: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    booking_fee: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    #: 멱등성 1겹 (§7.2). 같은 키로 몇 번 눌러도 주문은 하나다.
    idempotency_key: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    #: 중복 요청에 되돌려줄 최초 응답 스냅샷
    response_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=sa.func.now())
    paid_at: Mapped[datetime | None] = mapped_column(_ts(), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(_ts(), nullable=True)


# ---------------------------------------------------------------- 재고 (핵심)


class ScheduleSeat(Base):
    """회차별 좌석 재고. 이 테이블이 설계 전체의 중심이다 (§3).

    재고가 카운터가 아니라 행이므로 감산 경쟁이 없고, 오버부킹은 "숫자를 잘못 뺀
    버그"가 아니라 유니크 제약 위반이 되어 DB가 거절한다.
    """

    __tablename__ = "schedule_seats"
    __table_args__ = (
        # 오버부킹의 1차 방어선: 같은 회차에 같은 좌석은 한 행만 존재한다.
        sa.UniqueConstraint("schedule_id", "seat_id", name="uq_schedule_seat"),
        # HELD 상태와 hold 메타데이터가 어긋난 행은 DB가 애초에 받지 않는다.
        sa.CheckConstraint(
            "(status = 'HELD' AND held_by IS NOT NULL AND hold_expires_at IS NOT NULL)"
            " OR (status <> 'HELD' AND held_by IS NULL AND hold_expires_at IS NULL)",
            name="hold_shape",
        ),
        # 좌석맵은 회차 단위 1,200행 전량 조회다. INCLUDE로 힙 접근을 없앤다 (§5.5).
        sa.Index(
            "ix_seatmap",
            "schedule_id",
            postgresql_include=["seat_id", "grade", "price", "status"],
        ),
        # 만료 스윕이 훑는 대상은 선점분뿐이다. 부분 인덱스로 스캔량을 재고 전체에서
        # 동시 선점 좌석 수(수백 행)로 줄인다 (§5.4).
        sa.Index(
            "ix_hold_expiry",
            "hold_expires_at",
            postgresql_where=sa.text("status = 'HELD'"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    schedule_id: Mapped[int] = mapped_column(sa.ForeignKey("schedules.id"), nullable=False)
    seat_id: Mapped[int] = mapped_column(sa.ForeignKey("seats.id"), nullable=False)
    grade: Mapped[str] = mapped_column(sa.Text, nullable=False)  # VIP / R / S
    price: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[str] = mapped_column(SeatStatus, nullable=False, server_default="AVAILABLE")
    held_by: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"), nullable=True)
    hold_expires_at: Mapped[datetime | None] = mapped_column(_ts(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=sa.func.now())


class OrderItem(Base):
    """이중 판매의 최종 방어선 (§7.2 DB 겹).

    schedule_seat_id 의 UNIQUE 가 핵심이다. 한 재고 행은 평생 한 주문 항목에만
    붙으므로, 앞의 두 겹(HTTP 멱등키·사가 조건부 전이)이 다 뚫려도 여기서 23505 가 난다.
    """

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(sa.ForeignKey("orders.id"), nullable=False, index=True)
    schedule_seat_id: Mapped[int] = mapped_column(
        sa.ForeignKey("schedule_seats.id"), nullable=False, unique=True
    )
    price: Mapped[int] = mapped_column(sa.Integer, nullable=False)


# ---------------------------------------------------------------- 결제


class Payment(Base):
    """주문당 여러 행 가능. 실패한 승인 시도도 남긴다 — 정산 분쟁의 유일한 근거."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(sa.ForeignKey("orders.id"), nullable=False, index=True)
    #: PG 거래번호. 웹훅 중복 판정 키 (§7.2)
    pg_tid: Mapped[str | None] = mapped_column(sa.Text, nullable=True, unique=True)
    status: Mapped[str] = mapped_column(PayStatus, nullable=False)
    amount: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    #: 취소 시점의 수수료 계산 결과 스냅샷.
    #: 정책이 바뀌어도 과거 취소의 근거는 안 흔들린다 (§7.3).
    fee_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=sa.func.now())
    approved_at: Mapped[datetime | None] = mapped_column(_ts(), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(_ts(), nullable=True)


# ---------------------------------------------------------------- outbox


class Outbox(Base):
    """확정 트랜잭션과 같이 커밋된다 (§7.1).

    "좌석은 잡혔는데 알림톡이 안 갔다"가 원천적으로 생기지 않는다.
    """

    __tablename__ = "outbox"
    __table_args__ = (
        sa.Index(
            "ix_outbox_unpublished",
            "created_at",
            postgresql_where=sa.text("published_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_ts(), nullable=False, server_default=sa.func.now())
    published_at: Mapped[datetime | None] = mapped_column(_ts(), nullable=True)
