# ADR 0001 — 좌석 선점(hold) 상태를 어디에 저장하는가

- 상태: 채택
- 날짜: 2026-09-09
- 관련: 설계 문서 §5.1, §5.6

## 맥락

오픈 순간 인기 좌석 한 석에 수백 개 요청이 동시에 도착한다. 정확히 하나만 성공시키면서
나머지가 DB를 마비시키지 않아야 한다. 선점 상태를 어디에 두느냐가 이 문제의 답을 결정한다.

## 검토한 안

| 안 | 장점 | 대가 |
|---|---|---|
| A. Postgres 행에만 | 진실이 한 곳. 재시작·장애에 무관하게 정확 | 모든 경합이 DB 행 잠금까지 내려온다 |
| B. Redis에만 | 가장 빠르고 TTL이 공짜 | Redis 유실 = 오버부킹 |
| C. Redis 게이트 + Postgres 원본 | DB 경합을 수백 → 1건으로 | 이중 기록. TTL 순서를 틀리면 유령 매진 |

## 결정

**C를 채택한다.** 정합성은 Postgres의 조건부 `UPDATE` 하나로 완결되고, Redis는 그 경로에
도달하는 요청 수를 줄이는 최적화 계층으로만 쓴다.

B를 기각한 이유는 성능이 아니라 장애 모드다. Redis를 정합성 경로에 넣으면 `FLUSHALL` 한 번이
이중 판매가 된다. C에서는 같은 사고가 p99 상승으로만 나타난다.

## 따라오는 제약

1. **`SEAT_GATE_TTL < HOLD_TTL`** 이어야 한다. 캐시가 원본보다 먼저 사라져야 유령 매진이 없다.
   `tests/test_seat_domain.py::test_seat_gate_ttl_is_shorter_than_hold_ttl` 가 이 부등식을 지킨다.
2. **Redis 게이트는 fail-open.** 게이트를 우회해도 Postgres가 막아준다.
   반대로 대기열 Redis는 fail-closed다 (§2.2) — 같은 장애에 대응이 반대인 이유를 혼동하지 말 것.
3. **오버부킹 0건은 Redis를 죽여서 증명한다.**
   `test_concurrency.py::test_overbooking_survives_redis_flush` 가 이 결정의 유일한 검증이다.
