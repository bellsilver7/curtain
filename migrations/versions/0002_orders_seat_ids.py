"""orders.seat_ids — 주문이 사려는 좌석을 기억한다

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09

autogenerate 가 뽑은 것은 add_column 한 줄이었다. 그대로 두면 행이 있는 DB 에서
NOT NULL 추가가 실패하므로, 세 단계로 나누고 기존 행은 order_items 에서 되채웠다
(결정 기록: 마이그레이션 전략 — 생성물은 반드시 읽고 손본다).

되채움이 '{}' 가 되는 행은 확정된 좌석이 없는 주문, 즉 PENDING 이거나 실패한
주문뿐이다. 그 주문들은 확정할 대상이 없으므로 리컨실러가 되물어 취소한다 —
빈 배열이 곧 "이 주문은 아무 좌석도 못 받았다"라는 정확한 진술이다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 확정된 좌석에서 의도를 되채운다. seat_id 오름차순은 선점 쿼리의 잠금 순서와
#: 같게 맞춰 둔 것이다 — 확정도 같은 순서로 잠근다.
_BACKFILL = """
UPDATE orders o
   SET seat_ids = COALESCE(
       (SELECT array_agg(ss.seat_id ORDER BY ss.seat_id)
          FROM order_items oi
          JOIN schedule_seats ss ON ss.id = oi.schedule_seat_id
         WHERE oi.order_id = o.id),
       '{}'::bigint[])
 WHERE o.seat_ids IS NULL
"""


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("seat_ids", postgresql.ARRAY(sa.BigInteger()), nullable=True),
    )
    op.execute(_BACKFILL)
    op.alter_column("orders", "seat_ids", nullable=False)


def downgrade() -> None:
    op.drop_column("orders", "seat_ids")
