"""raw SQL — 설계 문서: 재고 모델, 좌석 선점, 만료 스윕, 좌석맵 조회 부하

ORM 으로 감싸지 않는다. 이 쿼리들의 정확한 형태가 곧 설계다 (결정 기록: 마이그레이션 전략).
FOR UPDATE SKIP LOCKED, CTE 안의 guard 절, 조건부 WHERE status = 기대값 —
전부 ORM 이 가려버리면 리뷰에서 보이지 않는 것들이다.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

# bigint[] 파라미터. asyncpg 에 파이썬 리스트를 넘기려면 타입을 명시해야 한다.
_SEAT_IDS = sa.bindparam("seat_ids", type_=ARRAY(sa.BigInteger))
_ZONES = sa.bindparam("zones", type_=ARRAY(sa.Text))
_ROWS = sa.bindparam("row_labels", type_=ARRAY(sa.Text))
_WIDTHS = sa.bindparam("widths", type_=ARRAY(sa.Integer))
_GRADES = sa.bindparam("grades", type_=ARRAY(sa.Text))
_PRICES = sa.bindparam("prices", type_=ARRAY(sa.Integer))


# ══════════════════════════════════════════════════════════════════ 좌석 전개 (재고 모델)

INSERT_VENUE = sa.text("""
INSERT INTO venues (name, address) VALUES (:name, :address) RETURNING id
""")


CREATE_VENUE_SEATS = sa.text("""
-- 공연장 물리 좌석 생성. 열 정의를 배열로 받아 generate_series 로 전개한다.
-- 1,200번의 개별 INSERT 는 그 자체가 나중의 병목 재현을 방해한다 — 한 문장으로 끝낸다.
INSERT INTO seats (venue_id, zone, row_label, col_no)
SELECT :venue_id, layout.zone, layout.row_label, g.col_no
  FROM unnest(:zones, :row_labels, :widths) AS layout(zone, row_label, width),
       LATERAL generate_series(1, layout.width) AS g(col_no)
ON CONFLICT (venue_id, zone, row_label, col_no) DO NOTHING
RETURNING id
""").bindparams(_ZONES, _ROWS, _WIDTHS)


EXPAND_SCHEDULE_SEATS = sa.text("""
-- 회차 오픈 시 재고 행을 전개한다 (재고 모델). schedules × seats → schedule_seats.
--
-- ON CONFLICT DO NOTHING 이 이 문장을 멱등하게 만든다. 회차 오픈 처리가 두 번
-- 실행되거나 재시도되어도 재고가 늘어나지 않는다 — uq_schedule_seat 가 받아낸다.
INSERT INTO schedule_seats (schedule_id, seat_id, grade, price)
SELECT :schedule_id, s.id, gp.grade, gp.price
  FROM seats s
  JOIN unnest(:zones, :row_labels, :grades, :prices)
         AS gp(zone, row_label, grade, price)
    ON gp.zone = s.zone AND gp.row_label = s.row_label
 WHERE s.venue_id = :venue_id
ON CONFLICT (schedule_id, seat_id) DO NOTHING
RETURNING id
""").bindparams(_ZONES, _ROWS, _GRADES, _PRICES)


# ══════════════════════════════════════════════════════════════ 좌석 선점

LOCK_USER_QUOTA = sa.text("""
-- (회차, 사용자) 단위 어드바이저리 락. HOLD_SEATS 직전에 같은 트랜잭션에서 잡는다.
--
-- 왜 필요한가: HOLD_SEATS 의 owned 절은 "이 유저가 이 회차에 몇 석 갖고 있나"를
-- 세는 술어(predicate) 다. 행 잠금은 target 좌석에만 걸리므로, 서로 다른
-- 좌석을 노리는 동시 요청들은 아무것도 공유하지 않고 각자 owned=0 을 읽는다.
-- READ COMMITTED 에서 전형적인 write skew 이고, 실제로 4매 한도가 7매로 새는 것을
-- 테스트로 재현했다 (test_quota_is_enforced_under_concurrency).
--
-- 셀 행이 아예 없을 수도 있는 술어는 SELECT ... FOR UPDATE 로 잠글 수 없다(phantom).
-- SERIALIZABLE 로 올리면 정확하지만 경합 경로 전체에 재시도 루프가 붙는다.
-- 어드바이저리 락은 (회차, 사용자) 하나만 직렬화하므로 좌석 경합 경로에는 영향이 없다.
-- 커밋/롤백 시 자동 해제되고, 해시 충돌은 불필요한 직렬화일 뿐 오답이 아니다.
SELECT pg_advisory_xact_lock(hashtext(:quota_key)::bigint)
""")


HOLD_SEATS = sa.text("""
-- 좌석 선점. 요청 좌석 전량을 잡거나 0석이다 (원칙 "부분 성공은 없다").
--
-- 애플리케이션에 분기가 없다는 게 핵심이다. "3석은 됐고 1석은 안 됨" 이라는
-- 중간 상태가 만들어질 수 있는 코드 경로 자체가 존재하지 않는다.
WITH owned AS (
    -- 회차당 보유 매수 (정책 상수 MAX_SEATS_PER_ORDER).
    -- 유효한 hold 와 이미 결제된 좌석을 함께 센다. 만료된 hold 는 세지 않는다.
    SELECT count(*) AS n
      FROM schedule_seats ss
      LEFT JOIN order_items oi ON oi.schedule_seat_id = ss.id
      LEFT JOIN orders o       ON o.id = oi.order_id AND o.status = 'PAID'
     WHERE ss.schedule_id = :schedule_id
       AND ( (ss.status = 'HELD'
              AND ss.held_by = :user_id
              AND ss.hold_expires_at > now())
          OR (ss.status = 'BOOKED' AND o.user_id = :user_id) )
),
target AS (
    SELECT id
      FROM schedule_seats
     WHERE schedule_id = :schedule_id
       AND seat_id = ANY(:seat_ids)
       AND ( status = 'AVAILABLE'
             -- 만료된 hold 는 스윕 워커를 기다리지 않고 여기서 즉시 회수한다.
             -- 워커는 1s tick 이라 그 사이에도 좌석은 팔릴 수 있어야 한다.
          OR (status = 'HELD' AND hold_expires_at < now()) )
     ORDER BY seat_id   -- 잠금 순서 고정 = 교차 요청 간 데드락 회피
       FOR UPDATE
),
guard AS (
    -- 전량 확보 여부와 구매 한도를 SQL 안에서 판정한다.
    -- 밖에서 세면 세는 시점과 쓰는 시점 사이에 남이 끼어든다 (원칙 "모든 상태 전이는 조건부 쓰기").
    SELECT (SELECT count(*) FROM target) = cardinality(:seat_ids)
       AND (SELECT n FROM owned) + cardinality(:seat_ids) <= :max_seats
        AS ok
)
UPDATE schedule_seats s
   SET status          = 'HELD',
       held_by         = :user_id,
       hold_expires_at = now() + make_interval(secs => :hold_ttl_sec),
       updated_at      = now()
  FROM guard
 WHERE s.id IN (SELECT id FROM target)
   AND guard.ok        -- 하나라도 모자라거나 한도를 넘으면 0행
RETURNING s.seat_id, s.grade, s.price, s.hold_expires_at
""").bindparams(_SEAT_IDS)


# 실패 응답의 unavailable_seat_ids 를 채우기 위한 조회 (API 스펙).
# 클라이언트가 좌석맵 전체를 다시 받지 않고 그 좌석만 회색으로 칠할 수 있게 한다.
FIND_UNAVAILABLE_SEATS = sa.text("""
SELECT seat_id
  FROM schedule_seats
 WHERE schedule_id = :schedule_id
   AND seat_id = ANY(:seat_ids)
   AND NOT ( status = 'AVAILABLE'
          OR (status = 'HELD' AND hold_expires_at < now()) )
""").bindparams(_SEAT_IDS)


RELEASE_HOLD = sa.text("""
-- 본인 hold 해제 (좌석 변경 시). 남의 hold 는 건드릴 수 없다 — held_by 조건이 그것.
UPDATE schedule_seats
   SET status = 'AVAILABLE', held_by = NULL, hold_expires_at = NULL, updated_at = now()
 WHERE schedule_id = :schedule_id
   AND seat_id = ANY(:seat_ids)
   AND status = 'HELD'
   AND held_by = :user_id
RETURNING seat_id
""").bindparams(_SEAT_IDS)


# ══════════════════════════════════════════════════════════ 만료 스윕

SWEEP_EXPIRED_HOLDS = sa.text("""
-- 1s tick. 클라이언트가 브라우저를 닫아버린 경우의 유일한 회수 수단이다.
UPDATE schedule_seats
   SET status = 'AVAILABLE', held_by = NULL, hold_expires_at = NULL, updated_at = now()
 WHERE id IN (
   SELECT id FROM schedule_seats
    WHERE status = 'HELD' AND hold_expires_at < now()
    ORDER BY hold_expires_at
    LIMIT :batch                 -- 한 tick 처리 상한. 긴 트랜잭션 방지
      FOR UPDATE SKIP LOCKED     -- 워커 N대가 서로 기다리지 않는다
 )
RETURNING schedule_id, seat_id   -- 좌석맵 캐시 무효화 대상
""")


# ══════════════════════════════════════════════════════════ 좌석맵 · 검증

SEATMAP = sa.text("""
-- 회차 단위 전량 조회. ix_seatmap 의 INCLUDE 로 힙 접근 없이 인덱스에서 끝난다 (좌석맵 조회 부하).
SELECT ss.seat_id, s.zone, s.row_label, s.col_no, ss.grade, ss.price, ss.status
  FROM schedule_seats ss
  JOIN seats s ON s.id = ss.seat_id
 WHERE ss.schedule_id = :schedule_id
 ORDER BY s.zone, s.row_label, s.col_no
""")


SEAT_STATUS_COUNTS = sa.text("""
-- 총량 보존 불변식 (검증 시나리오 마지막 줄). 전체 테스트 뒤에 항상 붙인다.
SELECT status::text AS status, count(*) AS n
  FROM schedule_seats
 WHERE schedule_id = :schedule_id
 GROUP BY status
""")
