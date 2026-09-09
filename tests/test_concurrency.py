"""동시성 · 재고 정합성 — 설계 문서 §10

2주차의 진짜 산출물은 기능이 아니라 이 파일의 통과 로그다.
여기 있는 테스트 이름은 §10 표의 시나리오와 1:1로 대응한다.
"""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.skip(reason="TODO(2주차)")]


async def test_single_seat_contention() -> None:
    """같은 좌석 1석에 동시 200 요청 → 201 정확히 1건, 나머지 전부 409.

    합격 기준: DB에 해당 좌석 HELD 행이 정확히 1개.
    """


async def test_no_partial_success() -> None:
    """4석 요청 중 1석을 다른 유저가 0.1초 먼저 선점 → 요청 전체 실패.

    합격 기준: 나머지 3석은 AVAILABLE 유지. 쓰레기 hold 0건 (원칙 02).
    """


async def test_cross_seat_deadlock() -> None:
    """[A,B]와 [B,A]를 동시에 500회 → 데드락 0건.

    발생하면 §5.2의 `ORDER BY seat_id FOR UPDATE` 잠금 순서 가정이 깨진 것이고,
    SELECT ... FOR UPDATE 를 별도 문장으로 분리해야 한다.
    """


async def test_hold_expiry_reclaim() -> None:
    """선점 후 클라이언트 프로세스 강제 종료 → 421초 안에 AVAILABLE 복귀.

    합격 기준: 복귀 후 다른 유저가 같은 좌석 구매 성공.
    """


async def test_overbooking_survives_redis_flush() -> None:
    """선점 진행 중 redis-cli FLUSHALL → 오버부킹 0건.

    원칙 01("정합성의 단일 진실은 PostgreSQL")의 유일한 증명.
    p99 상승은 허용, 이중 판매는 불허.
    """


async def test_seat_count_invariant() -> None:
    """총량 보존: AVAILABLE + HELD + BOOKED = 1200 × 회차수.

    전체 스위트 뒤에 항상 붙이는 불변식. 개별 테스트가 다 통과해도
    이 합이 안 맞으면 좌석 행이 새고 있다는 뜻이다.
    """
