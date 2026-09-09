"""요청 의존성 — 설계 문서: 대기열 방어선, API 스펙

인증(Bearer), X-Entry-Token 검증, 레이트리밋을 여기서만 한다.
"""

# TODO(4주차): verify_entry_token(): 대기열을 통과하지 않은 요청은 403
