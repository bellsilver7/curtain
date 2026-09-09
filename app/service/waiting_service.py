"""대기열 유스케이스 — 설계 문서: 대기열

ZSET 순번 조회와 입장 토큰 발급. Redis 장애 시 fail-closed (장애 대응 방향).
"""

# TODO(4주차): enqueue(), position(), admit_batch(n=ADMIT_PER_SECOND)
