"""SQLAlchemy 엔진 · 세션 — 설계 문서 §5.5

커넥션 풀 크기가 곧 동시 처리량 상한이다. statement_timeout 을 반드시 건다.
"""

# TODO(1주차): async engine + session factory + 트랜잭션 헬퍼
