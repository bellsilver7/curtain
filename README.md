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

Python 3.11 이상이 필요하다. macOS 기본 `python3` 은 아직 3.9 인 경우가 많으니
버전을 먼저 확인할 것.

```bash
cp .env.example .env
make install     # .venv 생성 + 의존성 설치. python3 이 3.11 미만이면
                 #   make install PY_BOOTSTRAP=python3.12
make migrate     # postgres/redis 기동 후 스키마 적용
make api         # http://localhost:8000/docs
make worker      # 배치 워커 4종 (별도 터미널)
```

`make` 는 `.venv/bin/` 을 자동으로 찾으므로 activate 하지 않아도 된다.
직접 `pytest` 나 `alembic` 을 칠 때만 `source .venv/bin/activate` 가 필요하다.
`make help` 로 전체 타깃을 볼 수 있다.

Postgres 는 호스트 **15432**, Redis 는 **16379** 로 뜬다. 기본 포트를 비켜둔 이유는
다른 프로젝트의 DB 와 부딪히면 컨테이너가 안 뜨거나, 더 나쁜 경우 엉뚱한 DB 에
마이그레이션이 올라가기 때문이다. 접속 정보는 `.env` 하나만 보면 된다.

## 자주 쓰는 명령

| 명령 | 하는 일 |
|---|---|
| `make migrate` | 최신 리비전까지 스키마 적용 |
| `make revision m="설명"` | 모델 변경 후 리비전 생성 (생성물은 꼭 읽어볼 것) |
| `make drift` | 모델과 DB가 어긋났는지 확인 |
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

- [x] 설계 문서 (`docs/design.md`) · 결정 기록 (`docs/adr/` 3건)
- [x] 스키마 — 모델(`app/infra/db/models.py`) + Alembic 리비전 `0001`
- [x] 정책 상수와 취소 수수료 (`app/domain/policy.py`), 상태 전이 규칙 (`app/domain/seat.py`)
- [x] 좌석 전개 (`app/service/schedule_service.py`) — 1,200석 × 3회차, 멱등
- [x] **좌석 선점** (`app/service/hold_service.py` + `app/infra/db/queries.py`)
      — 전량 아니면 0석, 만료 hold 즉시 회수, 구매 한도
- [x] `hold_sweeper` 회수 로직 (워커 루프는 아직 스텁)
- [x] **동시성 테스트 통과** — 아래 참고
- [x] **Redis 좌석 게이트** — DB 앞단에서 패자를 걸러낸다. Redis 를 날려도 오버부킹 0건
- [ ] 좌석맵 조회 + ETag · 3초 캐시
- [ ] 결제 사가: FakePG · 확정 트랜잭션 · 리컨실러 · 멱등 3겹
- [ ] 대기열: ZSET 큐 · 입장 허용기 · locust

### 지금 통과하는 것

`make test` → 16건. Postgres 16 에 실제 동시 요청을 던져 확인했다.

| 시나리오 | 결과 |
|---|---|
| 같은 좌석 1석에 동시 200 요청 | 성공 1건, 나머지 409, `HELD` 행 1개 |
| `[A,B]` / `[B,A]` 교차 요청 240건 | 데드락 0건 — §5.2 잠금 순서 가정 유효 |
| 4석 중 1석 선점된 상태에서 4석 요청 | 전체 실패, 쓰레기 hold 0건 |
| 한 유저 12개 동시 요청 | 성공 4건 (한도 준수) — [ADR 0003](docs/adr/0003-quota-advisory-lock.md) |
| 만료 hold | 선점 쿼리가 즉시 회수 / `hold_sweeper` 도 회수 |
| 총량 보존 | `AVAILABLE + HELD + BOOKED = 1200 × 3` |
| 게이트: 같은 좌석 200 요청 | DB 잠금 쿼리 도달 **1회** (게이트 없으면 200회) |
| 게이트: 선점 중 `FLUSHALL` | 오버부킹 0건 — 정합성은 Postgres 가 지킨다 |
| 게이트: Redis 없음 | 통과 + degraded 표시. 판매는 계속된다 (fail-open) |

## 다음 한 걸음

정합성(선점·회수·한도)과 그 앞의 부하 방벽(게이트)이 모두 증명됐다. 남은 것:

1. **좌석맵 조회 + 3초 캐시** — 요청 수가 압도적으로 많은 경로다. 캐시 없이
   부하를 주면 선점 트랜잭션이 쓸 커넥션이 남지 않는다.
2. **결제 사가** — FakePG 에 타임아웃을 주입해 리컨실러가 실제로 뒤를 받치는지 본다.
3. **대기열** — ZSET 큐와 입장 허용기. 유량 밸브 값을 부하 테스트로 확정한다.

CI 에 `make drift` 와 `make test` 를 걸어두는 것도 지금이 적기다. 동시성
테스트는 로컬에서만 도는 순간 아무도 안 돌리게 된다.

### 테스트를 의심하는 방법

이 저장소의 테스트 중 상당수는 "구현 전에도 초록"인 함정이다. 그런 테스트는
일부러 깨뜨려 봐야 진짜인지 알 수 있다. 게이트 작업에서 여섯 가지를 망가뜨려
전부 빨강이 되는 것을 확인했고, 그 과정에서 실제로 아무것도 검증하지 않던
테스트 하나를 찾아 고쳤다. 새 함정을 놓을 때는 같은 절차를 밟을 것.
