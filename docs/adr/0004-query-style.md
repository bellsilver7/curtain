# ADR 0004 — 쿼리를 문자열 SQL 대신 SQLAlchemy Core 표현식으로 쓴다

- 상태: 채택
- 날짜: 2026-09-09
- 관련: 결정 기록: 마이그레이션 전략, `app/infra/db/queries.py`, `tests/test_queries.py`
- 수정: 결정 기록: 마이그레이션 전략 의 "쿼리는 ORM 을 거치지 않는다" 항목

## 맥락

결정 기록: 마이그레이션 전략 에서 모델을 스키마의 단일 원천으로 삼되 **쿼리는
문자열 SQL 로 둔다**고 정했다. 이유는 하나였다 — 선점 쿼리의 정확한 형태가 곧
설계이고, `FOR UPDATE`, `SKIP LOCKED`, CTE 의 `guard` 절이 흐려지면 리뷰에서
보이지 않는다는 것.

두 번 겪고 나서 그 판단의 값을 다시 계산했다.

첫째, 문자열 SQL 은 스키마와 어긋나도 조용하다. 컬럼 이름을 바꾸면 모델은
autogenerate 가 잡고 `make drift` 가 확인해 주지만, `queries.py` 의 문자열은
그 쿼리를 실제로 실행하는 코드 경로가 테스트에 있을 때까지 아무 말도 하지 않는다.

둘째, 호출부가 타입 없는 dict 다. `{"schedule_id": ..., "seat_ids": ...}` 의 키는
문자열 SQL 안의 `:이름` 과 손으로 맞춘 것이고, 오타는 실행 시점의
`missing bind parameter` 다.

그리고 "SQL 을 눈으로 읽어 잠금 절을 확인한다"는 것 자체가 근거로 약하다. 이
프로젝트는 동시성 주장에 테스트를 붙이기로 이미 정해 두었다 — 잠금 절도 같은
기준을 받아야 한다.

## 결정

**모든 쿼리를 SQLAlchemy Core 표현식으로 쓴다.** 모델의 컬럼과 제약 이름을 직접
참조하므로 스키마와 어긋나면 `queries.py` 를 import 하는 순간 깨진다.

**쿼리는 상수가 아니라 함수다.** 값을 키워드 인자로 받아 그 자리에서 바인드한다.
dict 대신 타입이 붙은 시그니처가 되고, UPDATE 문에서 바인드 파라미터 이름이 대상
테이블의 컬럼 이름과 겹치는 문제(아래)도 원천적으로 사라진다.

**ORM 세션은 쓰지 않는다.** 모델은 컬럼과 제약의 이름표이고 실행은 전부 Core 다.
identity map, flush 순서, lazy load 는 이 도메인에 끼어들 자리가 없고, 트랜잭션
경계는 계속 `app/service/` 가 갖는다.

**잠금 의미론은 `tests/test_queries.py` 가 지킨다.** 컴파일된 문장을 단정한다 —
선점의 `ORDER BY … FOR UPDATE`, 선점에 `SKIP LOCKED` 가 **없다는** 것, 스윕의
`FOR UPDATE SKIP LOCKED` 와 배치 상한, 해제의 `held_by` 조건, `guard` 절.
여덟 가지로 사보타주해서 전부 빨개지는 것을 확인했다.

## 옮기면서 실측으로 알게 된 것

### 상태값을 바인드 파라미터로 넘기면 부분 인덱스가 죽는다

표현식으로 옮기면 `status == "HELD"` 는 기본적으로 바인드 파라미터가 된다. 그러면
`ix_hold_expiry (hold_expires_at) WHERE status = 'HELD'` 가 매칭되지 않는다.
Postgres 16 에서 같은 쿼리를 두 형태로 EXPLAIN 한 결과다.

```
-- 파라미터
Limit -> Sort -> Seq Scan on schedule_seats
                   Filter: ((hold_expires_at < now()) AND (status = ('HELD'::cstring)::seat_status))

-- 리터럴
Limit -> Index Scan using ix_hold_expiry on schedule_seats
           Index Cond: (hold_expires_at < now())
```

부분 인덱스를 쓰려면 플래너가 "쿼리 조건 ⇒ 인덱스 조건"을 증명해야 하는데,
파라미터는 그 증명의 재료가 되지 못한다. 그래서 상태값은 `literal_column` 으로
박는다. 닫힌 집합이고 사용자 입력이 아니므로 주입 위험은 없다.

만료 스윕은 재고 전체(3,600행)가 아니라 동시 선점 좌석 수(수백 행)만 훑는다는
전제로 설계했으므로, 이건 스타일 문제가 아니라 설계가 무너지는 문제다.
`test_sweep_uses_the_partial_index` 가 계획을 직접 보고 지킨다.

### UPDATE 의 바인드 파라미터 이름은 대상 테이블 컬럼과 겹칠 수 없다

`schedule_seats` 에 `schedule_id` 컬럼이 있으므로, `schedule_id` 라는 이름의
파라미터를 UPDATE 에 넘기면 SQLAlchemy 가 그것을 SET 절 값으로 해석하고 컴파일이
실패한다. `.params()` 는 DML 에서 아예 지원되지 않는다. 쿼리를 함수로 만든 것이
이 문제의 해결책이기도 하다 — 이름을 SQLAlchemy 가 유일하게 붙인다.

### 계획을 볼 때 파라미터를 펼치면 안 된다

처음 만든 계획 확인 테스트는 `literal_binds` 로 컴파일한 문장을 EXPLAIN 했다.
그러면 상태값을 파라미터로 되돌리는 사보타주가 **초록으로 통과한다** — 펼치는
순간 리터럴과 파라미터가 구분되지 않기 때문이다. 실제 드라이버 경로와 같은 문장,
같은 파라미터로 EXPLAIN 하도록 고쳤고, 그 함정을 `_sql` 주석에 남겼다.

### 1행 CTE 를 UPDATE 의 FROM 에 올리면 카테시안 곱 경고가 붙는다

원래 SQL 은 `UPDATE … FROM guard WHERE guard.ok` 였다. 표현식도 같은 SQL 을
만들지만 SQLAlchemy 의 린터가 매 실행마다 카테시안 곱 경고를 낸다. 의도한 교차
조인과 실수를 구분할 수 없게 되는 쪽이 손해라서, `guard` 를 스칼라 서브쿼리로
읽는다. `guard` 라는 이름은 SQL 에 그대로 남는다.

## 검토한 대안

**문자열 SQL 유지.** 가장 적은 변경이고, 잠금 절이 눈에 보인다는 장점이 실재한다.
버린 이유는 그 장점을 테스트로 대체할 수 있는데 단점(스키마 드리프트가 조용함,
호출부가 타입 없음)은 대체할 수 없기 때문이다.

**ORM 세션 도입.** 재고 경합 도메인에서 얻을 것이 없다. `UPDATE … RETURNING` 한
문장으로 판정하는 설계에 flush 순서와 identity map 이 끼어들면, 지금 근거로 삼고
있는 동시성 테스트의 의미가 흐려진다.

**선점 쿼리만 문자열로 남기기.** 실제로 처음 이 방향으로 만들었다. 두 방식이
한 파일에 섞이면 새 쿼리를 어느 쪽으로 쓸지가 매번 판단거리가 되고, "이 쿼리는
왜 예외인가"를 설명하는 주석이 계속 늘어난다. 표현식으로도 잠금 절이 정확히
같은 SQL 로 나오는 것을 확인했으므로 예외를 두지 않는다.

## 되돌리는 방법

`queries.py` 를 문자열 SQL 로 되돌리고 `tests/test_queries.py` 를 지우면 된다.
호출부는 dict 로 돌아가고, 그때는 `hold_seats` 의 `schedule_id` 파라미터 이름
충돌을 다시 만나므로 UPDATE 문의 파라미터 이름에 접두어를 붙여야 한다.
