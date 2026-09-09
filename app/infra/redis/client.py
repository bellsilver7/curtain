"""Redis 클라이언트 · Lua 로딩 — 설계 문서: Redis 좌석 게이트, 대기열

seat_gate.lua 를 SCRIPT LOAD 로 등록해 EVALSHA 로 호출한다.
"""

# TODO(2주차): get_client(), load_scripts(), seat_gate(keys, token, ttl_ms)
