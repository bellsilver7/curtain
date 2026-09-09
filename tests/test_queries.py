"""쿼리 표현식 검증 — 설계 문서: 좌석 선점, 만료 스윕, 좌석맵 조회 부하

raw SQL 을 SQLAlchemy Core 표현식으로 옮기면 잃는 것이 하나 있다. 문장을 눈으로
읽어 "FOR UPDATE 가 여기 있다"를 확인할 수 없다는 것이다. 그 확인을 여기서
기계가 대신한다 (결정 기록: 쿼리 작성 방식).

컴파일 결과 문자열을 단정하는 테스트는 보통 깨지기 쉬워 피하지만, 여기서
단정하는 것은 포맷이 아니라 잠금 의미론이다. 이 문구가 사라지는 변경은 곧
설계 변경이고, 조용히 통과해서는 안 된다.

앞쪽 절반은 DB 없이 돈다 (make test-unit 에 포함된다). 계획을 확인하는
뒤쪽 절반만 실제 Postgres 를 쓴다.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine

from app.infra.db import queries
from tests.conftest import Seeded

_PG = postgresql.dialect()


def _sql(stmt: Any) -> str:
    """읽기 위한 문장. 파라미터 자리에 값을 펼쳐 넣어 사람이 눈으로 볼 수 있게 한다.

    잠금 절 단정에만 쓴다. 계획을 볼 때 이것을 쓰면 안 된다 — 값을 펼치는 순간
    바인드 파라미터였는지 리터럴이었는지가 구분되지 않아서, 파라미터화 때문에
    인덱스를 놓치는 회귀를 이 문장으로는 절대 잡을 수 없다. 실제로 그 함정에
    한 번 빠졌고(사보타주가 초록으로 돌아왔다), 그래서 _explain 을 따로 뒀다.
    """
    return str(stmt.compile(dialect=_PG, compile_kwargs={"literal_binds": True}))


async def _explain(conn: Any, stmt: Any) -> str:
    """실제 드라이버 경로의 실행 계획.

    파라미터를 펼치지 않고 asyncpg 에 그대로 넘긴다. 애플리케이션이 실행할 때와
    같은 문장, 같은 파라미터여야 계획도 같다.
    """
    compiled = stmt.compile(dialect=conn.engine.dialect)
    params = compiled.construct_params()
    positional = tuple(params[key] for key in compiled.positiontup)
    rows = (await conn.exec_driver_sql("EXPLAIN " + str(compiled), positional)).all()
    return "\n".join(row[0] for row in rows)


def _hold() -> Any:
    return queries.hold_seats(
        schedule_id=1, user_id=2, seat_ids=[11, 12, 13], max_seats=4, hold_ttl_sec=420
    )


#: 모든 쿼리를 한 번씩. 새 쿼리를 추가하면 여기에도 넣어야 스키마 검증을 받는다.
def _every_statement() -> list[tuple[str, Any]]:
    return [
        ("insert_venue", queries.insert_venue(name="커튼홀", address="서울")),
        (
            "create_venue_seats",
            queries.create_venue_seats(
                venue_id=1, zones=["1층"], row_labels=["A"], widths=[3]
            ),
        ),
        (
            "expand_schedule_seats",
            queries.expand_schedule_seats(
                schedule_id=1,
                venue_id=1,
                zones=["1층"],
                row_labels=["A"],
                grades=["VIP"],
                prices=[170_000],
            ),
        ),
        ("lock_user_quota", queries.lock_user_quota(quota_key="curtain.hold:1:2")),
        ("hold_seats", _hold()),
        (
            "find_unavailable_seats",
            queries.find_unavailable_seats(schedule_id=1, seat_ids=[11, 12]),
        ),
        (
            "release_hold",
            queries.release_hold(schedule_id=1, user_id=2, seat_ids=[11, 12]),
        ),
        ("sweep_expired_holds", queries.sweep_expired_holds(batch=500)),
        ("seatmap", queries.seatmap(schedule_id=1)),
        ("seat_status_counts", queries.seat_status_counts(schedule_id=1)),
    ]


# ─────────────────────────────────────────────────── 잠금 의미론 (DB 불필요)


def test_hold_locks_rows_in_seat_id_order() -> None:
    """선점은 seat_id 오름차순으로 잠근다.

    잠금 순서가 요청마다 다르면 A→B 와 B→A 가 서로를 기다려 데드락이 난다.
    좌석 1,2 와 2,1 을 동시에 던지는 test_cross_seat_deadlock 이 그것을 재현하는데,
    거기서는 데드락 0건이 "우연히 안 겹쳤다"로도 설명된다 — 순서를 고정했다는
    사실 자체는 여기서 단정한다.
    """
    sql = _sql(_hold())
    assert "ORDER BY schedule_seats.seat_id FOR UPDATE" in sql, (
        f"선점 쿼리에서 잠금 순서가 사라졌다. 데드락 회피의 근거가 이것뿐이다.\n{sql}"
    )


def test_hold_waits_instead_of_skipping_locked_rows() -> None:
    """선점은 SKIP LOCKED 를 쓰지 않는다 — 기다린다.

    남이 잠근 행을 건너뛰면 target 이 요청보다 적어지고, guard 가 거짓이 되어
    0행을 돌려준다. 즉 SKIP LOCKED 는 "경합 시 실패"를 "경합 시 조용한 전량 실패"로
    바꾼다. 스윕은 반대로 건너뛰어야 하므로, 두 쿼리가 서로의 옵션을 물려받지
    않았는지 확인한다.
    """
    assert "SKIP LOCKED" not in _sql(_hold())


def test_hold_decides_all_or_nothing_inside_sql() -> None:
    """전량 판정과 구매 한도가 SQL 안에 있다 (원칙 "부분 성공은 없다").

    이 판정이 애플리케이션으로 나오는 순간, 세는 시점과 쓰는 시점 사이에 남이
    끼어들 창이 생긴다.
    """
    sql = _sql(_hold())
    assert "guard.ok" in sql, f"guard 절이 사라졌다.\n{sql}"
    assert "cardinality(" in sql, f"요청 매수와 확보 매수를 비교하지 않는다.\n{sql}"


def test_sweep_workers_do_not_queue_behind_each_other() -> None:
    """스윕은 SKIP LOCKED 로 훑는다 — 워커 N대가 같은 행을 기다리지 않는다."""
    sql = _sql(queries.sweep_expired_holds(batch=500))
    assert "FOR UPDATE SKIP LOCKED" in sql, (
        f"스윕에서 SKIP LOCKED 가 사라졌다. 워커를 늘리면 서로 막힌다.\n{sql}"
    )
    assert "LIMIT 500" in sql, f"한 tick 처리 상한이 사라졌다.\n{sql}"


def test_release_cannot_touch_someone_elses_hold() -> None:
    """해제는 본인 hold 만. held_by 조건이 그 유일한 방어선이다."""
    sql = _sql(queries.release_hold(schedule_id=7, user_id=42, seat_ids=[11]))
    assert "schedule_seats.held_by = 42" in sql, (
        f"소유자 조건이 사라졌다. 남의 좌석을 풀 수 있게 된다.\n{sql}"
    )
    assert "schedule_seats.status = 'HELD'" in sql, (
        f"상태 조건이 사라졌다 (원칙 \"모든 상태 전이는 조건부 쓰기\").\n{sql}"
    )


def test_seat_status_is_inlined_not_parameterized() -> None:
    """상태값은 리터럴이어야 한다.

    바인드 파라미터로 넘기면 Postgres 가 부분 인덱스 ix_hold_expiry 의 조건절
    (WHERE status = 'HELD')이 쿼리 조건에서 따라 나온다는 것을 증명하지 못하고
    Seq Scan 으로 간다. 계획까지 확인하는 테스트는 아래 integration 쪽에 있고,
    여기서는 문장 자체를 본다 — DB 없이도 회귀를 잡을 수 있게.
    """
    sql = str(queries.sweep_expired_holds(batch=500).compile(dialect=_PG))
    assert "status = 'HELD'" in sql, (
        f"상태값이 파라미터로 빠졌다. 부분 인덱스가 매칭되지 않는다.\n{sql}"
    )


# ─────────────────────────────────────────────── 실제 스키마·계획 (Postgres 필요)

@pytest.mark.integration
async def test_every_statement_matches_the_live_schema(
    engine: AsyncEngine, seeded: Seeded
) -> None:
    """모든 쿼리를 실제 스키마에 대고 EXPLAIN 한다.

    표현식은 컬럼 이름을 모델에서 가져오므로 오타는 import 시점에 잡힌다. 잡히지
    않는 것은 모델과 DB 가 어긋난 경우다 — make drift 가 그것을 보지만, 쿼리가
    실제로 계획까지 서는지는 별개다. ENUM 비교나 배열 캐스팅처럼 컴파일은 되고
    실행이 안 되는 종류를 여기서 걸러낸다.

    EXPLAIN 은 실행하지 않으므로 UPDATE·INSERT 도 안전하게 통과시킬 수 있다.
    """
    for name, stmt in _every_statement():
        # 문장마다 커넥션을 새로 연다. 하나가 실패하면 트랜잭션이 abort 되어
        # 뒤의 문장들이 전부 "current transaction is aborted" 로 뭉개진다 —
        # 그러면 어느 쿼리가 진짜 범인인지 알 수 없다.
        async with engine.connect() as conn:
            try:
                await _explain(conn, stmt)
            except Exception as exc:  # noqa: BLE001 - 어느 쿼리인지 알려주는 게 목적
                pytest.fail(f"{name} 이 실제 스키마에서 계획을 세우지 못한다: {exc}")


@pytest.mark.integration
async def test_sweep_uses_the_partial_index(
    engine: AsyncEngine, seeded: Seeded
) -> None:
    """스윕이 ix_hold_expiry 를 탄다.

    부분 인덱스가 스캔량을 재고 전체(3,600행)에서 동시 선점 좌석 수(수백 행)로
    줄인다. 상태값을 리터럴로 박은 이유가 이것이고, 실제로 파라미터 버전은
    같은 조건에서 Seq Scan 이 됐다.
    """
    async with engine.connect() as conn:
        plan = await _explain(conn, queries.sweep_expired_holds(batch=500))
    assert "ix_hold_expiry" in plan, (
        f"스윕이 부분 인덱스를 타지 않는다. 만료 좌석을 찾으려고 재고 전체를 "
        f"훑고 있다.\n{plan}"
    )
