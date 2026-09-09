# ADR 0002 — 스키마를 SQL 파일 대신 Alembic 으로 관리한다

- 상태: 채택
- 날짜: 2026-09-09
- 관련: 설계 문서 §3, `app/infra/db/models.py`, `migrations/`
- 대체: 초기 커밋의 `migrations/0001_init.sql` (삭제)

## 맥락

초기 커밋은 손으로 쓴 `0001_init.sql` 한 장이었다. 스키마가 한 번도 안 바뀔 거라면
그게 가장 정직하지만, 이 프로젝트는 4주에 걸쳐 §3(재고) → §7(결제) 순서로 테이블을
붙여 나갈 예정이다. 두 번째 변경부터는 "지금 DB가 어느 상태인지"를 사람이 기억해야
하고, 그때부터 SQL 한 장은 부채가 된다.

## 결정

**Alembic 을 쓰고, `app/infra/db/models.py` 의 SQLAlchemy 모델을 스키마의 단일
원천으로 삼는다.** 리비전은 `alembic revision --autogenerate` 로 뽑는다.

모델을 두더라도 **쿼리는 ORM 을 거치지 않는다.** 선점·스윕·확정 SQL은 여전히
`app/infra/db/queries.py` 의 raw SQL이다 (§5.2, §5.4, §7.1). 그 쿼리들의 정확한
형태가 곧 설계이고, ORM 으로 감싸면 `FOR UPDATE SKIP LOCKED` 나 CTE 안의
`guard` 절 같은 것이 흐려진다. 모델의 역할은 두 개다 — autogenerate 의 diff 대상,
그리고 스키마 문서.

## 검증한 것

autogenerate 가 이 스키마의 Postgres 기능을 실제로 잡아내는지 Postgres 16 에
직접 올려서 확인했다.

| 항목 | 결과 |
|---|---|
| 커버링 인덱스 `INCLUDE (seat_id, grade, price, status)` | 정상 생성 |
| 부분 인덱스 `WHERE status = 'HELD'` | 정상 생성 (`ix_hold_expiry`) |
| 부분 인덱스 `WHERE status = 'PENDING'`, `WHERE published_at IS NULL` | 정상 생성 |
| 복합 CHECK (`hold_shape`) | 정상 생성 |
| `timestamptz` | 정상 |
| `alembic check` (모델 ↔ DB 드리프트) | `No new upgrade operations detected` |
| `downgrade base` → `upgrade head` 왕복 | **처음엔 실패** (아래) |

## 수동 보정: ENUM

왕복 테스트가 `type "order_status" already exists` 로 깨졌다. 원인은 autogenerate 가
컬럼에 `sa.Enum(...)` 을 인라인으로 박아놓기 때문이다.

- `upgrade`: `create_table` 이 `CREATE TYPE` 을 checkfirst 없이 실행한다.
  중간에 실패한 마이그레이션을 다시 돌리면 `DuplicateObjectError`.
- `downgrade`: `DROP TYPE` 을 **아예 만들어주지 않는다.** 테이블만 사라지고 타입은 남는다.

그래서 `0001_initial_schema.py` 에서 타입 수명을 명시적으로 관리한다.

```python
SEAT_STATUS = postgresql.ENUM("AVAILABLE", "HELD", "BOOKED",
                              name="seat_status", create_type=False)
# upgrade   : for enum in ENUMS: enum.create(bind, checkfirst=True)
# downgrade : for enum in reversed(ENUMS): enum.drop(bind, checkfirst=True)
```

`create_type=False` 는 "컬럼은 이 타입을 쓰되 생성은 하지 말라"는 뜻이다.
(`sa.Enum` 에는 없는 `postgresql.ENUM` 전용 인자다.) 보정 후 왕복 2회를 돌려
잔여 타입 0개, 드리프트 0건을 확인했다.

## 따라오는 규칙

1. **생성된 리비전은 항상 읽는다.** autogenerate 는 초안이지 결과물이 아니다.
   특히 ENUM 값 추가는 `ALTER TYPE ... ADD VALUE` 로 직접 써야 하고,
   부분 인덱스의 조건절 *변경*은 Alembic 이 감지하지 못한다.
2. **`alembic check` 를 CI 에 건다** (`make drift`). 모델을 고치고 리비전을 안 만든
   상태로 머지되는 것을 막는 유일한 장치다.
3. **접속 문자열은 `alembic.ini` 에 넣지 않는다.** `migrations/env.py` 가
   `DATABASE_URL` 을 읽는다. ini 에 박으면 비밀번호가 저장소에 남는다.
4. **제약 이름은 명명 규칙으로 고정한다** (`models.py` 의 `NAMING_CONVENTION`).
   이름 없는 제약은 DB마다 다른 이름을 갖게 되고, 그때부터 마이그레이션이 손댈 수 없다.
   이 규칙 때문에 CHECK 이름이 설계 초안의 `ck_hold_shape` 에서
   `ck_schedule_seats_hold_shape` 로 바뀌었다.
