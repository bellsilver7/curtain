# Curtain

좌석 지정 공연 티켓 예매 시스템. 토이 프로젝트.

설계의 무게는 전부 **같은 좌석을 동시에 노리는 수천 명을 어떻게 한 명으로 줄이는가**에
실려 있다. "돌아가는 예매"가 아니라 *틀린 예매가 구조적으로 불가능한* 예매를 목표로 한다.

FastAPI · PostgreSQL 16 · Redis 7

> 전체 설계는 **[docs/design.md](docs/design.md)** 에 있다. 코드를 건드리기 전에 §1(원칙)과
> §5(동시성)를 먼저 읽는 것을 권한다. 아래는 그중 실행에 필요한 부분만 옮긴 것이다.

## 네 가지 원칙

1. **정합성의 단일 진실은 PostgreSQL이다.** Redis는 전부 최적화 계층 — 통째로 날려도
   오버부킹은 발생하지 않아야 한다.
2. **부분 성공은 없다.** 4석 요청은 4석 전부이거나 0석이다.
3. **모든 상태 전이는 조건부 쓰기로만 한다.** `WHERE status = 기대값` 없는 `UPDATE`는 없다.
4. **외부 호출은 리컨실러가 뒤를 받친다.** 결제가 "실패한 것"과 "응답을 못 받은 것"은
   완전히 다른 사건이다.

## 시작하기

```bash
cp .env.example .env
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

make up          # postgres + redis 기동
make migrate     # 스키마 적용
make api         # http://localhost:8000/docs
make worker      # 배치 워커 4종 (별도 터미널)
```

## 자주 쓰는 명령

| 명령 | 하는 일 |
|---|---|
| `make test-unit` | DB 없이 도는 도메인 테스트만. 지금 통과한다 |
| `make test` | 전체. `docs/design.md` §10 표를 그대로 실행한다 |
| `make test-concurrency` | 2주차의 진짜 산출물 — 좌석 경합·데드락·총량 보존 |
| `make load` | 오픈런 부하 (locust) |
| `make lint` | ruff + mypy |

## 구조

```
app/
├─ api/        HTTP 경계. 검증·직렬화만. 비즈니스 판단 금지
├─ domain/     순수 파이썬. import 가능한 것은 표준 라이브러리뿐
├─ service/    유스케이스 조립. 트랜잭션 경계가 여기 있다
├─ infra/      db · redis · pg 어댑터 (FakePG 포함)
└─ worker/     hold_sweeper · queue_admitter · order_reconciler · outbox_publisher
```

계층 규칙은 하나 — **`app/domain/`은 DB도 Redis도 HTTP도 모른다.** 그래야 상태 전이 규칙을
순수 함수로 테스트할 수 있다. 이 규칙이 깨지면 `make lint`의 mypy strict 가 먼저 알려준다.

## 현재 상태

초기 커밋 시점의 스캐폴드다. 실제로 동작하는 것은 아래뿐이다.

- [x] 설계 문서 (`docs/design.md`) · 결정 기록 (`docs/adr/`)
- [x] 스키마 (`migrations/0001_init.sql`)
- [x] 정책 상수와 취소 수수료 계산 (`app/domain/policy.py`) — `make test-unit` 통과
- [x] Redis 좌석 게이트 Lua (`app/infra/redis/lua/seat_gate.lua`)
- [ ] 1주차 — 재고 골격: 좌석 전개 · 좌석맵 조회 + ETag 캐시
- [ ] 2주차 — 선점과 회수: 조건부 UPDATE · 게이트 연결 · `hold_sweeper`
- [ ] 3주차 — 결제 사가: FakePG · 확정 트랜잭션 · 리컨실러 · 멱등 3겹
- [ ] 4주차 — 대기열과 부하: ZSET 큐 · 입장 허용기 · locust

## 다음 한 걸음

`tests/test_concurrency.py`부터 채우는 것을 권한다. 테스트가 먼저 있으면 §5.2의 SQL이
맞는지 하루 안에 판정 나고, 틀렸을 때 무엇을 바꿔야 하는지도 명확해진다. 반대로 API부터
만들면 "일단 동작하는데 진짜 맞는지 모르는" 코드가 4주 내내 남는다.

특히 §5.2의 CTE 안 `ORDER BY seat_id FOR UPDATE`가 실제로 그 순서대로 잠근다는 보장은
실행 계획에 달려 있다. 문서를 믿지 말고 교차 좌석 조합(`[A,B]`와 `[B,A]` 동시)으로
데드락 발생 여부를 직접 확인할 것.
