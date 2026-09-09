"""주문 · 결제 · 취소 — 설계 문서 §7

Idempotency-Key 필수. 사가의 시작점이며 판단은 order_service 가 한다.
"""

# TODO(3주차): POST /orders, GET /orders/{id}, POST /orders/{id}/cancel
