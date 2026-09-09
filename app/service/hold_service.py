"""좌석 선점 유스케이스 — 설계 문서 §5.2, §5.3

Redis 게이트 → Postgres 조건부 UPDATE 순서로 시도한다. 게이트는 fail-open (§2.2).
"""

# TODO(2주차): acquire(user_id, schedule_id, seat_ids) / release(hold_id)
