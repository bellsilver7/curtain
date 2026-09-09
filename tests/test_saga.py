"""결제 사가 · 멱등성 — 아직 구현되지 않은 동작의 명세 (설계 문서: 결제 사가)

여섯 건 전부 빨강으로 시작한다. 기대하는 표면은 _orders() / _fake_pg() 가
실패 메시지로 설명한다.

이 파일의 전제는 하나다 — **PG 응답은 반드시 유실된다.** 그래서 여기서 진짜로
검증하는 것은 정상 경로가 아니라 다음 셋이다.

  무응답은 실패가 아니다      응답을 못 받았다는 사실만으로 취소하면, 돈은 나갔는데
                              좌석이 없는 사고가 난다. 반드시 PG 사에 되물어야 한다
                              (원칙 "외부 호출은 리컨실러가 받친다").
  확정 경로는 하나여야 한다    동기 응답·웹훅·리컨실러가 각자 확정하면 셋 다 미묘하게
                              다르게 틀린다. 셋이 같은 함수를 부르는지 본다.
  순서가 곧 안전이다          취소는 환불 성공을 확인한 뒤에 좌석을 복원한다.
                              뒤집으면 환불 실패 시 되돌릴 수 없다.

시간을 실제로 흘려보내지 않는다. hold TTL 은 acquire 인자로 짧게 주고, 리컨실러의
"5분 초과" 기준은 호출 인자로 0 을 준다. sleep 으로 5분을 기다리는 테스트는
아무도 돌리지 않게 되고, 돌리지 않는 테스트는 근거가 아니다.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.domain import policy
from app.infra.db.engine import tx
from app.service import hold_service
from tests.conftest import Seeded

pytestmark = pytest.mark.integration


# ─────────────────────────────────────────────────────── 기대하는 표면


#: order_service 가 갖춰야 하는 이름들. 모듈이 스텁으로만 존재하면 import 는
#: 성공하고 AttributeError 만 나므로, 무엇이 없는지 여기서 확인해 알려준다.
_ORDER_SURFACE = ("place", "confirm_paid", "reconcile", "cancel",
                  "PaymentUnknown", "PaymentDeclined", "HoldExpired", "RefundFailed")


def _orders() -> Any:
    """app.service.order_service 를 가져온다. 없으면 기대 표면을 알려준다."""
    try:
        from app.service import order_service

        missing = [n for n in _ORDER_SURFACE if not hasattr(order_service, n)]
        if missing:
            raise ImportError(f"아직 없는 이름: {', '.join(missing)}")
    except ImportError as exc:  # pragma: no cover - 구현 전 경로
        pytest.fail(
            "app/service/order_service.py 가 필요하다.\n"
            "\n"
            "기대하는 표면 (이름만 맞으면 내부는 자유):\n"
            "\n"
            "    async def place(engine, gateway, *, schedule_id, user_id,\n"
            "                    seat_ids, idempotency_key) -> Placed\n"
            "    async def confirm_paid(engine, *, order_id, pg_tid) -> bool\n"
            "    async def reconcile(engine, gateway, *, older_than) -> list[int]\n"
            "    async def cancel(engine, gateway, *, order_id) -> Canceled\n"
            "\n"
            "    class Placed:    order_id, status, snapshot(dict)\n"
            "    class Canceled:  order_id, fee, refunded(bool)\n"
            "\n"
            "  - place() 는 트랜잭션을 여러 개 쓴다. 사가는 정의상 자기 트랜잭션\n"
            "    경계를 갖는다 — hold_service 와 달리 engine 을 받는다\n"
            "  - confirm_paid() 는 동기 응답·웹훅·리컨실러가 공유하는 단 하나의\n"
            "    확정 경로다. 반환값은 '이번 호출이 확정했는가' (두 번째는 False)\n"
            "  - 무응답은 예외로 알린다: PaymentUnknown. 주문은 PENDING 으로 남는다\n"
            "  - 승인 거절은 PaymentDeclined, hold 만료는 HoldExpired\n"
            f"\n원래 오류: {exc}"
        )
    return order_service


def _fake_pg(**kw: Any) -> Any:
    """FakePG 인스턴스. 없으면 기대 표면을 알려준다."""
    try:
        from app.infra.pg.fake import FakePG
    except ImportError as exc:  # pragma: no cover - 구현 전 경로
        pytest.fail(
            "app/infra/pg/fake.py 의 FakePG 가 필요하다.\n"
            "\n"
            "기대하는 표면:\n"
            "\n"
            "    FakePG(approve_outcome=..., lose_response=False,\n"
            "           refund_outcome=...)\n"
            "\n"
            "    async def approve(*, order_id, amount) -> PayAttempt\n"
            "    async def inquire(*, order_id) -> PayAttempt\n"
            "    async def refund(*, pg_tid, amount) -> PayAttempt\n"
            "\n"
            "    class PayAttempt: result(PayResult), pg_tid\n"
            "    class PayResult:  APPROVED · DECLINED · UNKNOWN\n"
            "\n"
            "  - UNKNOWN 이 1급 결과다. 예외가 아니라 반환값이어야 한다 —\n"
            "    예외로 만들면 호출부가 실패와 무응답을 같게 다루기 쉽다\n"
            "  - lose_response=True 는 '승인은 됐고 응답만 유실' 이다.\n"
            "    approve() 는 UNKNOWN 을 주지만 inquire() 는 APPROVED 를 준다.\n"
            "    이 조합이 이 파일에서 가장 중요한 시나리오다\n"
            f"\n원래 오류: {exc}"
        )
    return FakePG(**kw)


def _result() -> Any:
    """PayResult. 도메인 계층에 두는 이유는 계층 규칙 — 승인 결과의 종류는
    Postgres 도 PG 사도 모르는 순수한 도메인 어휘다."""
    try:
        from app.domain.payment import PayResult
    except ImportError as exc:  # pragma: no cover - 구현 전 경로
        pytest.fail(
            "app/domain/payment.py 의 PayResult 가 필요하다.\n"
            "\n"
            "    class PayResult(StrEnum):\n"
            "        APPROVED · DECLINED · UNKNOWN\n"
            "\n"
            "  - UNKNOWN 은 '실패'가 아니라 '모른다'다. 이 셋을 두 개로 줄이면\n"
            "    무응답을 실패로 접게 되고, 그게 이 설계가 막으려는 사고다\n"
            "  - app/domain/ 은 표준 라이브러리만 import 한다 (계층 규칙)\n"
            f"\n원래 오류: {exc}"
        )
    return PayResult


# ─────────────────────────────────────────────────────── 공용 헬퍼


async def _hold(
    engine: Any, seeded: Seeded, *, user_index: int, count: int = 1, ttl_sec: int = 420
) -> list[int]:
    """좌석을 선점하고 seat_id 목록을 돌려준다. 사가 테스트의 출발점."""
    seat_ids = list(seeded.seat_ids[:count])
    async with tx(engine) as conn:
        await hold_service.acquire(
            conn,
            schedule_id=seeded.schedule_id,
            user_id=seeded.user_ids[user_index],
            seat_ids=seat_ids,
            hold_ttl_sec=ttl_sec,
        )
    return seat_ids


async def _rows(engine: Any, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(sql), params)).all()


async def _order_status(engine: Any, order_id: int) -> str:
    rows = await _rows(engine, "SELECT status::text FROM orders WHERE id = :i", i=order_id)
    return str(rows[0][0])


async def _seat_status(engine: Any, schedule_id: int, seat_id: int) -> str:
    rows = await _rows(
        engine,
        "SELECT status::text FROM schedule_seats"
        " WHERE schedule_id = :s AND seat_id = :t",
        s=schedule_id,
        t=seat_id,
    )
    return str(rows[0][0])


async def _payments(engine: Any, order_id: int) -> list[tuple[str, int]]:
    rows = await _rows(
        engine,
        "SELECT status::text, amount FROM payments WHERE order_id = :i ORDER BY id",
        i=order_id,
    )
    return [(str(r[0]), int(r[1])) for r in rows]


async def _item_count(engine: Any, order_id: int) -> int:
    rows = await _rows(
        engine, "SELECT count(*) FROM order_items WHERE order_id = :i", i=order_id
    )
    return int(rows[0][0])


async def _outbox_topics(engine: Any) -> list[str]:
    rows = await _rows(engine, "SELECT topic FROM outbox ORDER BY id")
    return [str(r[0]) for r in rows]


# ─────────────────────────────────────────────────── 무응답 복구 (핵심)


async def test_pg_timeout_but_actually_approved(engine, seeded: Seeded) -> None:
    """PG 무응답 + 실제로는 승인 → 사용자는 실패, 리컨실러가 확정한다.

    이 프로젝트에서 가장 중요한 테스트다. 응답 유실을 실패로 취급하면 돈은
    나갔는데 좌석이 없는 사고가 난다 (원칙 "외부 호출은 리컨실러가 받친다").

    합격 기준: 최종적으로 좌석 1건 BOOKED, 승인된 결제 1건, 주문 PAID.
    그리고 사용자가 받은 즉시 응답은 실패여야 한다 — 거짓말로 성공을 주면
    확정 전에 티켓이 나간다.
    """
    orders = _orders()
    seat_ids = await _hold(engine, seeded, user_index=0)
    gateway = _fake_pg(lose_response=True)

    with pytest.raises(orders.PaymentUnknown) as caught:
        await orders.place(
            engine,
            gateway,
            schedule_id=seeded.schedule_id,
            user_id=seeded.user_ids[0],
            seat_ids=seat_ids,
            idempotency_key="k-timeout-approved",
        )

    order_id = caught.value.order_id
    assert await _order_status(engine, order_id) == "PENDING", (
        "무응답인데 주문 상태를 확정했다. PG 에 되묻기 전에는 아무 결론도 내릴 수 없다"
    )
    assert await _seat_status(engine, seeded.schedule_id, seat_ids[0]) == "HELD", (
        "무응답 응답에서 좌석을 놓아버렸다. 실제로 승인됐다면 그 좌석은 이미 팔린 것이다"
    )

    # ── 리컨실러 tick. 5분 기준은 인자로 0 을 준다 ──
    confirmed = await orders.reconcile(engine, gateway, older_than=timedelta(0))

    assert confirmed == [order_id], f"리컨실러가 이 주문을 집지 않았다: {confirmed}"
    assert await _order_status(engine, order_id) == "PAID"
    assert await _seat_status(engine, seeded.schedule_id, seat_ids[0]) == "BOOKED"
    assert await _item_count(engine, order_id) == 1

    approved = [p for p in await _payments(engine, order_id) if p[0] == "APPROVED"]
    assert len(approved) == 1, (
        f"승인된 결제가 {len(approved)}건이다 (기대 1건). "
        f"전체: {await _payments(engine, order_id)}"
    )
    assert "order.paid" in await _outbox_topics(engine), (
        "확정 트랜잭션에 outbox 가 같이 커밋되지 않았다 — "
        "'좌석은 잡혔는데 알림톡이 안 갔다'가 생길 수 있는 구조다"
    )


async def test_pg_timeout_and_not_approved(engine, seeded: Seeded) -> None:
    """PG 무응답 + 되물어도 미승인 → 주문 CANCELED, 좌석 복원.

    같은 무응답에서 반대 결론이 나오는 경로다. 분기의 근거는 오직
    inquire() 의 답이어야 한다.

    승인된 결제는 0건이지만 payments 행 자체는 남을 수 있다 — 실패한 승인
    시도도 정산 분쟁의 근거이므로 지우지 않는다. 그래서 세는 것은 APPROVED 다.
    """
    orders = _orders()
    seat_ids = await _hold(engine, seeded, user_index=1)
    gateway = _fake_pg(approve_outcome=_result().UNKNOWN)

    with pytest.raises(orders.PaymentUnknown) as caught:
        await orders.place(
            engine,
            gateway,
            schedule_id=seeded.schedule_id,
            user_id=seeded.user_ids[1],
            seat_ids=seat_ids,
            idempotency_key="k-timeout-declined",
        )
    order_id = caught.value.order_id

    await orders.reconcile(engine, gateway, older_than=timedelta(0))

    assert await _order_status(engine, order_id) == "CANCELED"
    assert await _seat_status(engine, seeded.schedule_id, seat_ids[0]) == "AVAILABLE", (
        "미승인으로 결론났는데 좌석이 묶여 있다. 팔 수 있는 좌석이 재고에서 사라진다"
    )
    assert await _item_count(engine, order_id) == 0
    approved = [p for p in await _payments(engine, order_id) if p[0] == "APPROVED"]
    assert approved == [], f"미승인인데 승인된 결제가 남아 있다: {approved}"


# ─────────────────────────────────────────────────────── 멱등성 세 겹


async def test_idempotency_key_replay(engine, seeded: Seeded) -> None:
    """같은 Idempotency-Key 로 동시 5회 → 주문 1건 · 결제 1건 · 응답 5개 동일.

    결제 버튼 연타와 클라이언트 자동 재시도를 막는 첫 겹이다 (멱등성 세 겹).
    응답이 서로 다르면 클라이언트가 어느 것을 믿어야 할지 모른다 — 그래서
    최초 응답을 스냅샷으로 저장해 그대로 재생해야 한다.
    """
    orders = _orders()
    seat_ids = await _hold(engine, seeded, user_index=2, count=2)
    gateway = _fake_pg()
    key = "k-replay"

    async def _place() -> Any:
        return await orders.place(
            engine,
            gateway,
            schedule_id=seeded.schedule_id,
            user_id=seeded.user_ids[2],
            seat_ids=seat_ids,
            idempotency_key=key,
        )

    placed = await asyncio.gather(*(_place() for _ in range(5)))

    order_ids = {p.order_id for p in placed}
    assert len(order_ids) == 1, f"같은 키로 주문이 {len(order_ids)}건 생겼다: {order_ids}"
    order_id = order_ids.pop()

    snapshots = [p.snapshot for p in placed]
    assert all(s == snapshots[0] for s in snapshots), (
        "같은 키의 응답이 서로 다르다. 최초 응답 스냅샷을 재생하지 않고 매번 "
        f"새로 만들고 있다.\n{snapshots}"
    )

    rows = await _rows(engine, "SELECT count(*) FROM orders")
    assert int(rows[0][0]) == 1, f"orders 테이블에 {rows[0][0]}행이다 (기대 1행)"
    assert await _item_count(engine, order_id) == 2
    approved = [p for p in await _payments(engine, order_id) if p[0] == "APPROVED"]
    assert len(approved) == 1, f"결제가 {len(approved)}건 승인됐다 (기대 1건)"
    assert (await _outbox_topics(engine)).count("order.paid") == 1, (
        "outbox 에 order.paid 가 여러 건이다. 알림이 중복 발송된다"
    )


async def test_duplicate_webhook(engine, seeded: Seeded) -> None:
    """같은 승인 콜백 3회(동시 포함) → order_items 1건, 3회 모두 성공.

    웹훅은 재전송이 기본이다. 두 번째 호출이 예외를 던지면 PG 사는 실패로 보고
    계속 재전송하고, 그러면 영원히 끝나지 않는다.

    확정이 조건부 전이(WHERE status = 'PENDING')이면 두 번째 호출은 0행 갱신
    후 조용히 성공한다 — 이게 사가 겹의 핵심 한 줄이다. 반환값으로 "이번 호출이
    확정했는가"를 구분해서, 알림을 한 번만 보낼 수 있게 한다.
    """
    orders = _orders()
    seat_ids = await _hold(engine, seeded, user_index=3)
    gateway = _fake_pg(approve_outcome=_result().UNKNOWN)

    with pytest.raises(orders.PaymentUnknown) as caught:
        await orders.place(
            engine,
            gateway,
            schedule_id=seeded.schedule_id,
            user_id=seeded.user_ids[3],
            seat_ids=seat_ids,
            idempotency_key="k-webhook",
        )
    order_id = caught.value.order_id

    # 웹훅 3회. 두 번은 동시에 온다.
    first = await orders.confirm_paid(engine, order_id=order_id, pg_tid="tid-webhook")
    rest = await asyncio.gather(
        orders.confirm_paid(engine, order_id=order_id, pg_tid="tid-webhook"),
        orders.confirm_paid(engine, order_id=order_id, pg_tid="tid-webhook"),
    )

    assert first is True, "첫 콜백이 확정하지 않았다"
    assert rest == [False, False], (
        f"중복 콜백이 다시 확정했다고 답한다: {rest}. "
        f"두 번째부터는 '이미 처리됨'이어야 알림이 중복 발송되지 않는다"
    )
    assert await _item_count(engine, order_id) == 1, (
        f"order_items 가 {await _item_count(engine, order_id)}행이다 (기대 1행). "
        f"중복 콜백이 좌석을 두 번 확정했다"
    )
    assert (await _outbox_topics(engine)).count("order.paid") == 1
    assert await _order_status(engine, order_id) == "PAID"


# ────────────────────────────────────────────── hold 만료와 취소 순서


async def test_hold_expired_during_approval(engine, seeded: Seeded) -> None:
    """승인 왕복 중 hold 가 만료되면 확정하지 않고 환불한다.

    만료된 hold 로 확정하면 이미 남에게 팔릴 수 있었던 좌석을 뒤늦게 가져가는
    것이 된다. 확정 쿼리가 hold_expires_at > now() 를 조건에 갖고 있으면
    행이 0개가 되고, 그때 롤백 + 환불이 정답이다.

    승인 자체는 성공했으므로 돈이 나가 있다 — 환불하지 않으면 사용자는 좌석도
    없고 돈도 없다.
    """
    orders = _orders()
    # hold 를 1초만 준다. 승인 지연 0.3초 × 4 를 못 버틴다.
    seat_ids = await _hold(engine, seeded, user_index=4, ttl_sec=1)
    gateway = _fake_pg(latency_sec=1.5)

    with pytest.raises(orders.HoldExpired) as caught:
        await orders.place(
            engine,
            gateway,
            schedule_id=seeded.schedule_id,
            user_id=seeded.user_ids[4],
            seat_ids=seat_ids,
            idempotency_key="k-hold-expired",
        )
    order_id = caught.value.order_id

    assert await _item_count(engine, order_id) == 0, "만료된 hold 로 좌석을 확정했다"
    assert await _order_status(engine, order_id) == "FAILED"

    refunded = [p for p in await _payments(engine, order_id) if p[0] == "REFUNDED"]
    assert len(refunded) == 1, (
        f"환불 기록이 {len(refunded)}건이다 (기대 1건). 승인은 됐고 좌석은 못 줬는데 "
        f"환불하지 않으면 사용자는 좌석도 돈도 없다.\n"
        f"전체: {await _payments(engine, order_id)}"
    )


async def test_cancel_restores_seat_only_after_refund(engine, seeded: Seeded) -> None:
    """환불이 실패하면 좌석을 복원하지 않는다 (취소와 환불 순서 규칙).

    순서를 뒤집으면 환불 실패인데 좌석은 이미 남에게 팔려 되돌릴 수 없다.
    사용자는 돈을 못 받고, 우리는 되돌릴 방법이 없다.

    수수료는 policy.cancel_fee() 로 계산하고 결과를 payments 에 스냅샷으로
    남긴다 — 정책이 바뀌어도 과거 취소의 근거가 흔들리지 않게.
    """
    orders = _orders()
    seat_ids = await _hold(engine, seeded, user_index=5)

    placed = await orders.place(
        engine,
        _fake_pg(),
        schedule_id=seeded.schedule_id,
        user_id=seeded.user_ids[5],
        seat_ids=seat_ids,
        idempotency_key="k-cancel",
    )
    assert await _seat_status(engine, seeded.schedule_id, seat_ids[0]) == "BOOKED"

    # ── 환불이 실패하는 취소 ──
    broken = _fake_pg(refund_outcome=_result().DECLINED)
    with pytest.raises(orders.RefundFailed):
        await orders.cancel(engine, broken, order_id=placed.order_id)

    assert await _seat_status(engine, seeded.schedule_id, seat_ids[0]) == "BOOKED", (
        "환불이 실패했는데 좌석을 복원했다. 그 좌석이 남에게 팔리면 되돌릴 수 없다"
    )
    assert await _order_status(engine, placed.order_id) == "PAID", (
        "환불 실패인데 주문을 취소 상태로 바꿨다. 다시 시도할 근거가 사라진다"
    )

    # ── 환불이 되는 취소 ──
    canceled = await orders.cancel(engine, _fake_pg(), order_id=placed.order_id)

    assert canceled.refunded is True
    assert await _order_status(engine, placed.order_id) == "CANCELED"
    assert await _seat_status(engine, seeded.schedule_id, seat_ids[0]) == "AVAILABLE"

    fees = [p for p in await _payments(engine, placed.order_id) if p[0] == "REFUNDED"]
    assert len(fees) == 1, f"환불 기록이 {len(fees)}건이다 (기대 1건)"
    # 회차는 D+21 이라 수수료 구간은 무료다 (정책 상수).
    assert canceled.fee == 0, (
        f"D+21 취소 수수료가 {canceled.fee}원이다 (기대 0원). "
        f"policy.cancel_fee() 를 쓰지 않고 자체 계산하고 있는지 확인할 것"
    )
    assert policy.MAX_SEATS_PER_ORDER >= len(seat_ids)  # 전제 확인
