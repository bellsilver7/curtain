"""Redis 클라이언트 · Lua 로딩 — 설계 문서 §5.3, §6

seat_gate.lua 를 SCRIPT LOAD 로 등록해 EVALSHA 로 호출한다.
"""

# TODO(2주차): get_client(), load_scripts(), seat_gate(keys, token, ttl_ms)
