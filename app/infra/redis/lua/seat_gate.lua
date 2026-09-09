-- 좌석 게이트: DB 앞단의 문지기 (설계 문서 §5.3)
--
-- 전량 획득 아니면 전량 반납. 4석 중 일부만 잡히는 상황을 여기서도 막는다.
-- 키는 seat:{schedule_id}:<seat_id> 형태로, SQL과 같은 seat_id 오름차순으로 넘긴다.
-- {schedule_id} 해시 태그 덕분에 클러스터에서도 같은 슬롯에 떨어진다.
--
-- KEYS : 선점하려는 좌석 키들 (정렬된 순서)
-- ARGV : [1] owner_token  [2] ttl_ms  (hold TTL보다 짧게 — §5.3)
-- 반환 : 1 = 전량 획득, 0 = 실패 (DB 왕복 없이 409)

local acquired = {}

for i = 1, #KEYS do
  if redis.call('SET', KEYS[i], ARGV[1], 'NX', 'PX', ARGV[2]) then
    acquired[#acquired + 1] = KEYS[i]
  else
    -- 부분 획득 되돌리기. 내가 쓴 값일 때만 지운다 —
    -- 그냥 DEL 하면 TTL 만료 후 남이 잡은 락을 지울 수 있다.
    for _, k in ipairs(acquired) do
      if redis.call('GET', k) == ARGV[1] then
        redis.call('DEL', k)
      end
    end
    return 0
  end
end

return 1
