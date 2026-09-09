"""결제 사가 · 멱등성 — 설계 문서: 결제 사가, 검증 시나리오

FakePG 에 지연과 타임아웃을 주입해서, 보상 경로(⑦–⑨)가 실제로 도는지 확인한다.
"""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.skip(reason="TODO(3주차)")]


async def test_pg_timeout_but_actually_approved() -> None:
    """FakePG 30초 지연 + 실제로는 승인 → 사용자는 실패 응답.

    합격 기준: 리컨실러가 승인을 발견해 확정. 최종적으로 좌석 1건, 결제 1건.
    응답 유실을 실패로 취급하면 돈은 나갔는데 좌석이 없는 사고가 난다 (원칙 "외부 호출은 리컨실러가 받친다").
    """


async def test_pg_timeout_and_not_approved() -> None:
    """FakePG 타임아웃 후 미승인 유지 → 주문 CANCELED, 좌석 AVAILABLE, 결제 0건."""


async def test_idempotency_key_replay() -> None:
    """동일 Idempotency-Key 로 POST /orders 5회 동시 → 주문 1건 · 결제 1건.

    합격 기준: 응답 5개의 body 가 모두 동일 (저장된 스냅샷 재생).
    """


async def test_duplicate_webhook() -> None:
    """같은 승인 콜백 3회(동시 1회 포함) → order_items 1건, 3회 모두 200."""


async def test_hold_expired_during_approval() -> None:
    """승인 왕복 중 hold 만료 → 409 HOLD_EXPIRED + 자동 환불 (좌석 상태 전이 note)."""


async def test_cancel_restores_seat_only_after_refund() -> None:
    """환불 실패 시 좌석이 복원되지 않아야 한다 (취소와 환불 순서 규칙)."""
