"""좌석맵 조회 — 설계 문서 §5.5

요청 수가 압도적으로 많은 엔드포인트. 3초 TTL 캐시 + ETag 로 DB를 보호한다.
"""

# TODO(1주차): GET /schedules/{id}/seatmap — Redis 해시 캐시, 변화 없으면 304
