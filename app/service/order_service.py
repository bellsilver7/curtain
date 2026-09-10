"""주문 사가 — 설계 문서: 결제 사가, 확정 트랜잭션

전제는 하나다. **PG 응답은 반드시 유실된다.** 그 전제에서 나오는 설계가 셋이다.

  무응답은 결론이 아니다      approve() 가 UNKNOWN 이면 주문을 PENDING 으로 두고
                              사용자에게는 실패를 알린다. 취소도 확정도 하지 않는다.
                              결론은 reconcile() 이 PG사에 되물어서만 얻는다.
  확정 경로는 하나다          confirm_paid() 를 동기 응답·웹훅·리컨실러가 공유한다.
                              셋으로 갈라지면 셋 다 미묘하게 다르게 틀린다.
  순서가 곧 안전이다          취소는 환불 성공을 확인한 뒤에 좌석을 복원한다.

이 모듈은 hold_service 와 달리 engine 을 받는다. 사가는 정의상 여러 트랜잭션에
걸쳐 있고 — 주문 생성, PG 왕복, 확정 — 그 경계를 사가 자신이 정해야 한다.
PG 왕복을 트랜잭션 안에 넣으면 수백 ms 짜리 외부 호출이 DB 트랜잭션을 붙잡고,
그게 오픈런에서 커넥션을 말리는 가장 흔한 사고다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain import policy
from app.domain.order import OrderStatus
from app.domain.payment import PayResult
from app.infra.db import queries
from app.infra.db.engine import tx
from app.infra.pg.base import PaymentGateway

#: 중복 요청이 원본의 응답 스냅샷을 기다리는 상한.
#:
#: 같은 키의 동시 요청 중 하나만 사가를 돌리고 나머지는 그 결과를 재생해야 한다.
#: 원본이 아직 PG 왕복 중이면 스냅샷이 없으므로 잠깐 기다린다 — 어드바이저리 락으로
#: 직렬화하는 방법도 있지만, 그러면 PG 왕복 내내 커넥션 하나가 묶인다.
#: 대기가 여기서 끝나면 "아직 진행 중"으로 답하는 것이 정직하다 (HTTP 409).
_REPLAY_WAIT_SEC = 0.05
_REPLAY_ATTEMPTS = 40


class OrderRejected(Exception):
    """주문을 진행할 수 없다. code 는 HTTP 계층이 그대로 쓴다."""

    code = "ORDER_REJECTED"

    def __init__(self, message: str, *, order_id: int | None = None) -> None:
        super().__init__(f"{self.code}: {message}")
        self.order_id = order_id


class HoldExpired(OrderRejected):
    """확정 시점에 hold 가 없거나 만료됐다.

    승인이 이미 났다면 자동 환불까지 끝난 상태로 올라온다 — 사용자는 좌석도
    돈도 잃지 않는다.
    """

    code = "HOLD_EXPIRED"


class PaymentDeclined(OrderRejected):
    """PG사가 승인을 거절했다. 결론이므로 주문은 FAILED 로 닫힌다."""

    code = "PAYMENT_DECLINED"


class PaymentUnknown(OrderRejected):
    """승인 여부를 모른다. 주문은 PENDING 으로 남고 리컨실러가 받는다.

    이것이 실패로 접히면 안 되는 이유: 실제로 승인됐다면 돈은 나갔다.
    """

    code = "PAYMENT_UNKNOWN"


class PaymentInProgress(OrderRejected):
    """같은 키의 원본 요청이 아직 진행 중이다. 재시도하면 스냅샷을 받는다."""

    code = "PAYMENT_IN_PROGRESS"


class RefundFailed(OrderRejected):
    """환불이 실패했다. 좌석은 복원하지 않는다 (취소와 환불 순서 규칙)."""

    code = "REFUND_FAILED"


@dataclass(frozen=True, slots=True)
class Placed:
    """주문 결과. snapshot 이 HTTP 응답 본문이 된다."""

    order_id: int
    status: str
    snapshot: dict[str, Any]
    #: 저장된 스냅샷을 재생한 것인가. 정확성과 무관하고 관측용이다.
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class Canceled:
    order_id: int
    #: 취소 수수료(원). policy.cancel_fee() 의 결과이며 payments 에 스냅샷으로 남는다.
    fee: int
    refunded: bool


# ─────────────────────────────────────────────────────────── 확정 (단일 경로)


async def confirm_paid(engine: AsyncEngine, *, order_id: int, pg_tid: str) -> bool:
    """주문을 확정한다. 동기 응답·웹훅·리컨실러가 **모두 이 함수를 부른다.**

    반환값은 "이번 호출이 확정했는가"다. 이미 확정된 주문에 다시 오면 False 를
    돌려주고 아무것도 바꾸지 않는다 — 예외가 아니다. 웹훅은 재전송이 기본이고,
    두 번째 호출에 500 을 주면 PG사가 영원히 재전송한다.

    확정은 네 문장을 한 트랜잭션에 묶는다 (확정 트랜잭션).

      1. orders → PAID          조건부. 0행이면 이미 처리됐다는 뜻이므로 즉시 종료
      2. order_items INSERT     만료된 hold 는 걸러진다. 모자라면 예외
      3. schedule_seats → BOOKED
      4. outbox INSERT          알림을 좌석과 같이 커밋한다

    2번에서 행이 모자라거나 UNIQUE 위반이 나면 HoldExpired 로 올린다. 롤백되므로
    1번의 PAID 전이도 함께 사라지고, 환불은 호출자가 한다 — 환불은 외부 호출이라
    트랜잭션 안에 둘 수 없다.
    """
    async with tx(engine) as conn:
        order = await _load_order(conn, order_id)

        # 1. 조건부 전이. 멱등성의 핵심 한 줄 (멱등성 세 겹).
        claimed = (
            await conn.execute(queries.mark_order_paid(order_id=order_id))
        ).first()
        if claimed is None:
            return False

        seat_ids = list(order.seat_ids or [])
        if not seat_ids:
            raise HoldExpired("확정할 좌석이 없는 주문입니다", order_id=order_id)

        # 2. 좌석을 이 주문에 붙인다.
        try:
            items = (
                await conn.execute(
                    queries.claim_order_items(
                        order_id=order_id,
                        schedule_id=order.schedule_id,
                        user_id=order.user_id,
                        seat_ids=seat_ids,
                    )
                )
            ).all()
        except IntegrityError as exc:
            # order_items.schedule_seat_id UNIQUE — 멱등성 3겹의 최후 방어선.
            # 여기까지 왔다는 것은 앞의 두 겹이 다 뚫렸다는 뜻이므로 조용히 넘기지 않는다.
            raise HoldExpired(
                f"좌석이 이미 다른 주문에 확정돼 있습니다: {exc.orig}", order_id=order_id
            ) from exc

        if len(items) != len(seat_ids):
            raise HoldExpired(
                f"요청 {len(seat_ids)}석 중 {len(items)}석만 유효합니다 "
                f"(hold 만료 또는 소유자 불일치)",
                order_id=order_id,
            )

        # 3. 좌석 확정.
        booked = (await conn.execute(queries.book_order_seats(order_id=order_id))).all()
        if len(booked) != len(seat_ids):
            raise HoldExpired(
                f"{len(seat_ids)}석 중 {len(booked)}석만 BOOKED 로 전이됐습니다",
                order_id=order_id,
            )

        # 결제 기록을 승인으로 올린다. 0행이면 REQUESTED 시도가 없는 경우
        # (리컨실러가 확정하는 경로) 이므로 새로 남긴다.
        promoted = (
            await conn.execute(
                queries.promote_payment_to_approved(order_id=order_id, pg_tid=pg_tid)
            )
        ).first()
        if promoted is None:
            await conn.execute(
                queries.insert_payment(
                    order_id=order_id,
                    status="APPROVED",
                    amount=order.total_amount,
                    pg_tid=pg_tid,
                )
            )

        # 4. 알림을 같은 트랜잭션에 싣는다.
        await conn.execute(
            queries.insert_outbox(
                topic="order.paid",
                payload={"order_id": order_id, "seat_ids": seat_ids},
            )
        )

        # 스냅샷은 확정과 같은 트랜잭션에서 쓴다. 갈라지면 중복 요청이
        # "PAID 인데 스냅샷 없음"을 보게 되고 그 상태를 다룰 코드가 또 필요해진다.
        snapshot = _snapshot(order_id=order_id, status=OrderStatus.PAID, seats=items)
        await conn.execute(
            queries.set_order_snapshot(order_id=order_id, snapshot=snapshot)
        )
        return True


# ─────────────────────────────────────────────────────────── 주문 (사가 시작)


async def place(
    engine: AsyncEngine,
    gateway: PaymentGateway,
    *,
    schedule_id: int,
    user_id: int,
    seat_ids: list[int],
    idempotency_key: str,
    min_hold_remaining: timedelta = policy.MIN_HOLD_REMAINING_FOR_PAYMENT,
) -> Placed:
    """주문 생성 + 결제 승인. 사가의 시작점 (설계 문서: API 스펙 POST /orders).

    min_hold_remaining 을 인자로 받는 이유는 테스트다. 기본값은 정책 상수이고,
    "승인 왕복 중 hold 만료" 경로를 재현하려면 이 사전 검사를 0 으로 낮춰야 한다 —
    그러지 않으면 짧은 hold 가 PG 를 부르기도 전에 거절된다.
    """
    if not seat_ids:
        raise OrderRejected("좌석이 비어 있습니다")

    # ── 1단계: PENDING 주문. 선행 조건 전부가 이 한 문장 안에 있다 ──
    #
    # 0행의 뜻은 두 가지다 — hold 가 없었거나, 같은 멱등키가 이미 있었거나.
    # 그 둘을 가르는 조회가 하나뿐이고 두 경로가 모두 그 조회를 통과하므로,
    # 테스트가 닿지 않는 방어 분기가 남지 않는다.
    async with tx(engine) as conn:
        opened = (
            await conn.execute(
                queries.open_order(
                    schedule_id=schedule_id,
                    user_id=user_id,
                    seat_ids=sorted(set(seat_ids)),
                    idempotency_key=idempotency_key,
                    min_hold_remaining_sec=int(min_hold_remaining.total_seconds()),
                )
            )
        ).mappings().first()

        if opened is None:
            duplicate_id = await _existing_order_id(conn, idempotency_key)
            if duplicate_id is None:
                raise HoldExpired(await _explain_hold(conn, schedule_id, user_id, seat_ids))
        else:
            duplicate_id = None
            order_id = int(opened["id"])
            total_amount = int(opened["total_amount"])
            await conn.execute(
                queries.insert_payment(
                    order_id=order_id, status="REQUESTED", amount=total_amount
                )
            )

    if duplicate_id is not None:
        return await _placed(engine, order_id=duplicate_id, replayed=True)

    # ── 2단계: PG 왕복. 트랜잭션 밖이다 ─────────────────────────
    attempt = await gateway.approve(order_id=order_id, amount=total_amount)

    # ── 3단계: 결과에 따라 확정하거나 닫거나, 아무것도 하지 않는다 ──
    if attempt.result is PayResult.UNKNOWN:
        # 아무것도 하지 않는 것이 정답이다. 주문은 PENDING 으로 남고
        # reconcile() 이 PG사에 되물어 결론을 낸다.
        raise PaymentUnknown(
            "승인 응답을 받지 못했습니다. 결제 상태 확인 후 처리됩니다",
            order_id=order_id,
        )

    if attempt.result is PayResult.DECLINED:
        await _close_unpaid(engine, order_id=order_id, status=OrderStatus.FAILED)
        raise PaymentDeclined("승인이 거절됐습니다", order_id=order_id)

    assert attempt.pg_tid is not None, "승인인데 거래번호가 없다 — 어댑터 버그"
    try:
        await confirm_paid(engine, order_id=order_id, pg_tid=attempt.pg_tid)
    except HoldExpired as exc:
        # 승인은 났고 좌석은 줄 수 없다. 돈을 돌려주지 않으면 사용자는 둘 다 잃는다.
        await _refund_and_fail(
            engine, gateway, order_id=order_id, pg_tid=attempt.pg_tid, amount=total_amount
        )
        raise HoldExpired(str(exc), order_id=order_id) from exc

    return await _placed(engine, order_id=order_id, replayed=False)


# ─────────────────────────────────────────────────────────── 리컨실러


async def reconcile(
    engine: AsyncEngine,
    gateway: PaymentGateway,
    *,
    older_than: timedelta = policy.PAYMENT_PENDING_LIMIT,
    batch: int = 100,
) -> list[int]:
    """결론 없는 주문을 PG사에 되물어 정리한다 (배치 워커, 30s tick).

    반환값은 이번 tick 에 **확정한** 주문 id 목록이다. 취소한 것은 포함하지 않는다 —
    호출자(워커)가 알림이나 지표로 쓰는 값이므로 "돈이 들어온 건"만 세는 것이 맞다.

    핵심은 분기의 근거다. 응답을 못 받았다는 사실만으로 취소하지 않고, 반드시
    inquire() 의 답을 본다. inquire() 마저 UNKNOWN 이면 아무것도 하지 않고 다음
    tick 에 다시 묻는다 — 모르는 채로 취소하는 것이 가장 나쁘다.
    """
    # 스캔은 트랜잭션 안에서 한다. SKIP LOCKED 는 같은 순간에 스캔하는 워커들이
    # 같은 행을 집지 않게 하는 것까지만 해준다 — 잠금은 이 트랜잭션이 끝나면 풀리고,
    # PG 왕복은 그 밖에서 일어나므로 두 워커가 같은 주문을 되물을 수는 있다.
    # 그 경우의 정확성은 confirm_paid 의 조건부 전이가 지키고, 낭비되는 것은
    # inquire 호출 한 번이다.
    async with engine.begin() as conn:
        candidates = [
            int(r[0])
            for r in (
                await conn.execute(
                    queries.unresolved_orders(
                        older_than_sec=older_than.total_seconds(), batch=batch
                    )
                )
            ).all()
        ]

    confirmed: list[int] = []
    for order_id in candidates:
        answer = await gateway.inquire(order_id=order_id)

        if not answer.result.is_conclusive:
            continue  # 다음 tick 에 다시 묻는다

        if answer.approved:
            assert answer.pg_tid is not None
            try:
                if await confirm_paid(engine, order_id=order_id, pg_tid=answer.pg_tid):
                    confirmed.append(order_id)
            except HoldExpired:
                # 승인은 났는데 좌석을 줄 수 없다. 환불하고 닫는다.
                await _refund_and_fail(
                    engine, gateway, order_id=order_id, pg_tid=answer.pg_tid, amount=None
                )
            continue

        # 미승인이 확인됐다. 좌석을 놓아주고 주문을 닫는다.
        await _close_unpaid(engine, order_id=order_id, status=OrderStatus.CANCELED)

    return confirmed


# ─────────────────────────────────────────────────────────── 취소


async def cancel(
    engine: AsyncEngine,
    gateway: PaymentGateway,
    *,
    order_id: int,
    now: datetime | None = None,
) -> Canceled:
    """취소. **환불 성공을 확인한 뒤에** 좌석을 복원한다 (취소와 환불).

    순서를 뒤집으면 환불이 실패했는데 좌석은 이미 남에게 팔려 되돌릴 수 없다.
    사용자는 돈을 못 받고, 우리에게는 되돌릴 방법이 없다.

    수수료는 policy.cancel_fee() 가 계산하고 결과를 payments.fee_snapshot 에
    남긴다 — 정책이 바뀌어도 과거 취소의 근거가 흔들리지 않게.
    """
    async with engine.connect() as conn:
        order = await _load_order(conn, order_id)
        approved = (
            await conn.execute(queries.approved_payment(order_id=order_id))
        ).mappings().first()

    if order.status != OrderStatus.PAID:
        raise OrderRejected(
            f"확정된 주문만 취소할 수 있습니다 (현재 {order.status})", order_id=order_id
        )
    if approved is None:
        raise OrderRejected("승인된 결제가 없습니다", order_id=order_id)

    fee = policy.cancel_fee(int(order.total_amount), now or _utcnow(), order.starts_at)
    refund_amount = int(order.total_amount) - fee

    attempt = await gateway.refund(pg_tid=str(approved["pg_tid"]), amount=refund_amount)

    if not attempt.approved:
        # 아무것도 바꾸지 않는다. 주문은 PAID 로 남아 다시 시도할 수 있다.
        async with tx(engine) as conn:
            await conn.execute(
                queries.insert_payment(
                    order_id=order_id,
                    status="FAILED",
                    amount=refund_amount,
                    fee_snapshot=_fee_snapshot(order, fee, refund_amount),
                )
            )
        raise RefundFailed(
            f"환불이 실패했습니다 ({attempt.result}). 좌석은 복원하지 않습니다",
            order_id=order_id,
        )

    async with tx(engine) as conn:
        closed = (
            await conn.execute(
                queries.mark_order(order_id=order_id, status="CANCELED", expect="PAID")
            )
        ).first()
        if closed is None:
            # 다른 경로가 먼저 취소했다. 환불은 이미 났으므로 기록만 남긴다.
            return Canceled(order_id=order_id, fee=fee, refunded=True)

        await conn.execute(
            queries.insert_payment(
                order_id=order_id,
                status="REFUNDED",
                amount=refund_amount,
                pg_tid=attempt.pg_tid,
                fee_snapshot=_fee_snapshot(order, fee, refund_amount),
            )
        )
        await conn.execute(queries.restore_order_seats(order_id=order_id))
        await conn.execute(
            queries.insert_outbox(
                topic="order.canceled",
                payload={"order_id": order_id, "fee": fee, "refund": refund_amount},
            )
        )

    return Canceled(order_id=order_id, fee=fee, refunded=True)


# ─────────────────────────────────────────────────────────── 내부 헬퍼


async def _explain_hold(
    conn: AsyncConnection,
    schedule_id: int,
    user_id: int,
    seat_ids: list[int],
) -> str:
    """주문이 열리지 않은 이유를 사람이 읽을 문장으로.

    판정이 아니라 설명이다. 판정은 open_order() 가 이미 끝냈고, 이 조회는
    "몇 석이 유효했고 잔여가 얼마였는지"를 응답에 담기 위한 것이다.
    """
    held = (
        await conn.execute(
            queries.held_seats_for_order(
                schedule_id=schedule_id, user_id=user_id, seat_ids=seat_ids
            )
        )
    ).mappings().all()

    wanted = len(set(seat_ids))
    if len(held) != wanted:
        return f"요청 {wanted}석 중 유효한 hold 는 {len(held)}석입니다"

    remaining = min(r["remaining"] for r in held)
    return (
        f"hold 잔여가 {remaining.total_seconds():.0f}초입니다. "
        f"승인 왕복 중에 만료될 수 있으므로 요청 자체를 거절합니다"
    )


async def _existing_order_id(
    conn: AsyncConnection, idempotency_key: str
) -> int | None:
    """이 키로 만들어진 주문이 이미 있는가.

    READ COMMITTED 이므로 문장마다 스냅샷이 새로 잡힌다 — 같은 트랜잭션 안이어도
    직전에 커밋된 주문을 볼 수 있고, 그래서 이 재확인이 실제로 동작한다.
    """
    row = (
        await conn.execute(
            queries.order_by_idempotency_key(idempotency_key=idempotency_key)
        )
    ).mappings().first()
    return int(row["id"]) if row is not None else None


async def _load_order(conn: AsyncConnection, order_id: int) -> Any:
    row = (
        await conn.execute(queries.order_for_saga(order_id=order_id))
    ).mappings().first()
    if row is None:
        raise OrderRejected(f"주문 {order_id} 이 없습니다", order_id=order_id)
    return _Order(row)


class _Order:
    """주문 한 건의 읽기 전용 뷰. 매핑 키를 속성으로 쓰기 위한 얇은 껍데기."""

    __slots__ = (
        "id",
        "user_id",
        "schedule_id",
        "status",
        "seat_ids",
        "total_amount",
        "response_snapshot",
        "starts_at",
    )

    def __init__(self, row: Any) -> None:
        for name in self.__slots__:
            setattr(self, name, row[name])


async def _placed(engine: AsyncEngine, *, order_id: int, replayed: bool) -> Placed:
    """저장된 응답 스냅샷을 돌려준다. 없으면 잠깐 기다린다.

    원본 요청이 아직 PG 왕복 중이면 스냅샷이 없다. 이때 새 스냅샷을 만들면
    같은 키의 응답이 서로 달라지고, 클라이언트는 어느 것을 믿어야 할지 모른다.
    """
    for _ in range(_REPLAY_ATTEMPTS):
        async with engine.connect() as conn:
            row = (
                await conn.execute(queries.order_for_saga(order_id=order_id))
            ).mappings().one()
        if row["response_snapshot"] is not None:
            return Placed(
                order_id=order_id,
                status=str(row["status"]),
                snapshot=dict(row["response_snapshot"]),
                replayed=replayed,
            )
        if str(row["status"]) != OrderStatus.PENDING:
            # 결론은 났는데 스냅샷이 없다 = 실패로 닫힌 주문이다.
            raise OrderRejected(
                f"주문이 {row['status']} 상태로 종료됐습니다", order_id=order_id
            )
        await asyncio.sleep(_REPLAY_WAIT_SEC)

    raise PaymentInProgress(
        "같은 키의 원본 요청이 아직 진행 중입니다", order_id=order_id
    )


def _snapshot(*, order_id: int, status: str, seats: list[Any]) -> dict[str, Any]:
    """HTTP 응답 본문이 될 값.

    DB 에서 읽은 값만으로 만든다 — 재생될 때와 최초 응답이 반드시 같아야 하므로
    시각이나 난수를 섞으면 안 된다.
    """
    return {
        "order_id": order_id,
        "status": str(status),
        "schedule_seat_ids": sorted(int(r[0]) for r in seats),
    }


def _fee_snapshot(order: Any, fee: int, refund: int) -> dict[str, Any]:
    return {
        "ticket_amount": int(order.total_amount),
        "fee": fee,
        "refund": refund,
        "starts_at": order.starts_at.isoformat(),
    }


async def _close_unpaid(
    engine: AsyncEngine, *, order_id: int, status: str
) -> None:
    """확정되지 않은 주문을 닫고 좌석을 놓아준다.

    좌석 해제는 hold 해제와 같은 쿼리를 쓴다 — 아직 BOOKED 가 아니라 HELD 이므로
    확정 취소가 아니라 선점 해제다.
    """
    async with tx(engine) as conn:
        order = await _load_order(conn, order_id)
        closed = (
            await conn.execute(
                queries.mark_order(order_id=order_id, status=str(status), expect="PENDING")
            )
        ).first()
        if closed is None:
            return  # 다른 경로가 먼저 처리했다
        await conn.execute(queries.fail_pending_payments(order_id=order_id))
        if order.seat_ids:
            await conn.execute(
                queries.release_hold(
                    schedule_id=order.schedule_id,
                    user_id=order.user_id,
                    seat_ids=list(order.seat_ids),
                )
            )


async def _refund_and_fail(
    engine: AsyncEngine,
    gateway: PaymentGateway,
    *,
    order_id: int,
    pg_tid: str,
    amount: int | None,
) -> None:
    """승인은 났는데 좌석을 줄 수 없을 때의 보상.

    환불에 실패하면 주문을 PENDING 으로 남긴다. 실패를 삼키고 FAILED 로 닫으면
    "돈은 나갔는데 아무 기록도 없는" 주문이 되고, 리컨실러가 다시 집을 근거도
    사라진다.
    """
    async with engine.connect() as conn:
        order = await _load_order(conn, order_id)
    total = amount if amount is not None else int(order.total_amount)

    attempt = await gateway.refund(pg_tid=pg_tid, amount=total)

    async with tx(engine) as conn:
        await conn.execute(
            queries.insert_payment(
                order_id=order_id,
                status="REFUNDED" if attempt.approved else "FAILED",
                amount=total,
                pg_tid=attempt.pg_tid,
            )
        )
        await conn.execute(queries.fail_pending_payments(order_id=order_id))
        if attempt.approved:
            await conn.execute(
                queries.mark_order(order_id=order_id, status="FAILED", expect="PENDING")
            )
            if order.seat_ids:
                await conn.execute(
                    queries.release_hold(
                        schedule_id=order.schedule_id,
                        user_id=order.user_id,
                        seat_ids=list(order.seat_ids),
                    )
                )


def _utcnow() -> datetime:
    """취소 수수료 계산의 기준 시각. 날짜 경계로만 쓰이므로 초 단위 오차는 무해하다."""
    return datetime.now(UTC)


__all__ = [
    "Canceled",
    "HoldExpired",
    "OrderRejected",
    "PaymentDeclined",
    "PaymentInProgress",
    "PaymentUnknown",
    "Placed",
    "RefundFailed",
    "cancel",
    "confirm_paid",
    "place",
    "reconcile",
]