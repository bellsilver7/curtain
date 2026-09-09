"""PG 콜백 — 설계 문서 §7.2

서명 검증 → pg_tid 중복 판정 → order_service.confirm_paid() 재사용. 항상 200.
"""

# TODO(3주차): POST /webhooks/pg — 확정 경로를 새로 만들지 말 것
