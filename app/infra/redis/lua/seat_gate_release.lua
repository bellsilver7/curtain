-- 좌석 게이트 반납 (설계 문서: Redis 좌석 게이트)
--
-- 두 곳에서 쓴다.
--   1. 게이트는 잡았는데 DB 가 거절한 경우 — 반납하지 않으면 게이트 TTL 동안
--      아무도 못 잡는 유령 매진이 된다. 트랜잭션 롤백은 Redis 를 되돌려주지 않는다.
--   2. 사용자가 좌석 선택을 되돌린 경우 (`release()`)
--
-- KEYS : 반납할 좌석 키들
-- ARGV : [1] owner_token
-- 반환 : 실제로 지운 키 개수
--
-- 값을 확인하고 지우는 이유는 seat_gate.lua 와 같다. 내 TTL 이 만료된 뒤
-- 남이 잡은 락을 지우면, 두 사람이 같은 좌석의 게이트를 통과하게 된다.
-- (그래도 DB 가 막아주지만, 게이트가 제 일을 못 하는 것은 그 자체로 버그다.)

local removed = 0

for i = 1, #KEYS do
  if redis.call('GET', KEYS[i]) == ARGV[1] then
    removed = removed + redis.call('DEL', KEYS[i])
  end
end

return removed
