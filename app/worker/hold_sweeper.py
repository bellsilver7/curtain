"""만료 hold 회수 — 설계 문서 §5.4

1s tick. FOR UPDATE SKIP LOCKED 이므로 여러 대 띄워도 안전하다.
"""

# TODO(2주차): tick(): LIMIT 500, 회수된 좌석은 좌석맵 캐시 무효화
