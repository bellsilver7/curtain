"""공연장 좌석과 회차 재고 전개 — 설계 문서: 재고 모델

재고는 "남은 수량"이 아니라 "특정 회차의 특정 좌석"이다. 회차를 열 때
schedules × seats 를 미리 전개해 두면, 이후 모든 경합이 카운터 감산이 아니라
행 잠금이 되고 오버부킹은 유니크 제약 위반이 된다.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from app.infra.db import queries
from app.service.dto import RowSpec, VenueLayout


def _rows(zone: str, labels: str, width: int, grade: str, price: int) -> list[RowSpec]:
    return [RowSpec(zone, ch, width, grade, price) for ch in labels]


#: 설계 문서가 가정하는 1,200석 공연장: VIP 120 / R 380 / S 700.
#: 열 폭이 균일하지 않은 것은 의도적이다 — 실제 공연장이 그렇고, 균일한 격자는
#: 좌석 번호 계산 버그를 숨긴다.
DEMO_HALL = VenueLayout(
    name="커튼홀",
    address="서울시 어딘가",
    rows=(
        *_rows("1층", "ABC", 40, "VIP", 170_000),          # 3 × 40 = 120
        *_rows("1층", "DEFGHIJKLM", 38, "R", 140_000),     # 10 × 38 = 380
        *_rows("2층", "ABCDEFGHIJKLMNOPQRST", 35, "S", 110_000),  # 20 × 35 = 700
    ),
)


async def create_venue(conn: AsyncConnection, layout: VenueLayout) -> int:
    """공연장과 물리 좌석을 만든다. 좌석은 회차와 무관하게 한 번만 만들어진다."""
    venue_id: int = (
        await conn.execute(
            queries.insert_venue(name=layout.name, address=layout.address)
        )
    ).scalar_one()

    await conn.execute(
        queries.create_venue_seats(
            venue_id=venue_id,
            zones=[r.zone for r in layout.rows],
            row_labels=[r.row_label for r in layout.rows],
            widths=[r.width for r in layout.rows],
        )
    )
    return venue_id


async def expand_schedule_seats(
    conn: AsyncConnection, *, schedule_id: int, venue_id: int, layout: VenueLayout
) -> int:
    """회차 오픈 시 재고 행을 전개한다. 반환값은 새로 만들어진 행 수.

    멱등하다 — 두 번 호출해도 ON CONFLICT DO NOTHING 이 받아내므로 두 번째는 0 을
    돌려준다. 회차 오픈 처리가 재시도되어도 재고가 늘어나지 않는다.
    """
    result = await conn.execute(
        queries.expand_schedule_seats(
            schedule_id=schedule_id,
            venue_id=venue_id,
            zones=[r.zone for r in layout.rows],
            row_labels=[r.row_label for r in layout.rows],
            grades=[r.grade for r in layout.rows],
            prices=[r.price for r in layout.rows],
        )
    )
    return len(result.fetchall())
