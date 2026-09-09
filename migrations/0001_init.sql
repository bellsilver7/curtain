-- Curtain 초기 스키마 — 설계 문서 §3 참고
-- 실행: make migrate

BEGIN;

CREATE TYPE seat_status  AS ENUM ('AVAILABLE', 'HELD', 'BOOKED');
CREATE TYPE order_status AS ENUM ('PENDING', 'PAID', 'CANCELED', 'FAILED');
CREATE TYPE pay_status   AS ENUM ('REQUESTED', 'APPROVED', 'FAILED', 'REFUNDED');

-- ---------------------------------------------------------------- 사용자 · 장소

CREATE TABLE users (
  id         bigserial   PRIMARY KEY,
  email      text        NOT NULL UNIQUE,
  name       text        NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE venues (
  id      bigserial PRIMARY KEY,
  name    text      NOT NULL,
  address text      NOT NULL
);

-- 물리 좌석. 공연장에 고정이며 회차와 무관하다 (§11: 다중 공연장은 미룸).
CREATE TABLE seats (
  id        bigserial PRIMARY KEY,
  venue_id  bigint    NOT NULL REFERENCES venues(id),
  zone      text      NOT NULL,   -- 1층 / 2층 / 발코니
  row_label text      NOT NULL,   -- A, B, C …
  col_no    integer   NOT NULL,
  CONSTRAINT uq_seat_position UNIQUE (venue_id, zone, row_label, col_no)
);

-- ---------------------------------------------------------------- 공연 · 회차

CREATE TABLE performances (
  id               bigserial PRIMARY KEY,
  venue_id         bigint    NOT NULL REFERENCES venues(id),
  title            text      NOT NULL,
  running_time_min integer   NOT NULL,
  age_limit        integer   NOT NULL DEFAULT 0
);

CREATE TABLE schedules (
  id             bigserial   PRIMARY KEY,
  performance_id bigint      NOT NULL REFERENCES performances(id),
  starts_at      timestamptz NOT NULL,   -- 관람일시. 취소 수수료 계산의 기준
  sale_opens_at  timestamptz NOT NULL,   -- 이 시각 전 요청은 425 (§6.1)
  CONSTRAINT uq_schedule UNIQUE (performance_id, starts_at)
);

-- ---------------------------------------------------------------- 주문

CREATE TABLE orders (
  id              bigserial    PRIMARY KEY,
  user_id         bigint       NOT NULL REFERENCES users(id),
  schedule_id     bigint       NOT NULL REFERENCES schedules(id),
  status          order_status NOT NULL DEFAULT 'PENDING',
  total_amount    integer      NOT NULL,
  booking_fee     integer      NOT NULL DEFAULT 0,
  -- 멱등성 1겹 (§7.2). 같은 키로 몇 번 눌러도 주문은 하나다.
  idempotency_key text         NOT NULL UNIQUE,
  -- 중복 요청에 되돌려줄 최초 응답 스냅샷
  response_snapshot jsonb,
  created_at      timestamptz  NOT NULL DEFAULT now(),
  paid_at         timestamptz,
  canceled_at     timestamptz
);

-- 리컨실러가 훑는 대상은 PENDING 주문뿐이다 (§2.1 order_reconciler).
CREATE INDEX ix_orders_pending ON orders (created_at)
  WHERE status = 'PENDING';

-- ---------------------------------------------------------------- 재고 (핵심)

CREATE TABLE schedule_seats (
  id              bigserial   PRIMARY KEY,
  schedule_id     bigint      NOT NULL REFERENCES schedules(id),
  seat_id         bigint      NOT NULL REFERENCES seats(id),
  grade           text        NOT NULL,   -- VIP / R / S
  price           integer     NOT NULL,
  status          seat_status NOT NULL DEFAULT 'AVAILABLE',
  held_by         bigint      REFERENCES users(id),
  hold_expires_at timestamptz,
  updated_at      timestamptz NOT NULL DEFAULT now(),

  -- 오버부킹의 1차 방어선: 같은 회차에 같은 좌석은 한 행만 존재한다.
  CONSTRAINT uq_schedule_seat UNIQUE (schedule_id, seat_id),

  -- HELD 상태와 hold 메타데이터가 어긋난 행은 DB가 애초에 받지 않는다.
  CONSTRAINT ck_hold_shape CHECK (
       (status =  'HELD' AND held_by IS NOT NULL AND hold_expires_at IS NOT NULL)
    OR (status <> 'HELD' AND held_by IS NULL     AND hold_expires_at IS NULL)
  )
);

-- 좌석맵은 회차 단위 1,200행 전량 조회다. INCLUDE로 힙 접근을 없앤다.
CREATE INDEX ix_seatmap ON schedule_seats (schedule_id)
  INCLUDE (seat_id, grade, price, status);

-- 만료 스윕이 훑는 대상은 선점분뿐이다. 부분 인덱스로 스캔량을
-- 재고 전체에서 동시 선점 좌석 수(수백 행)로 줄인다.
CREATE INDEX ix_hold_expiry ON schedule_seats (hold_expires_at)
  WHERE status = 'HELD';

-- 이중 판매의 최종 방어선 (§7.2 DB 겹).
-- 한 재고 행은 평생 한 주문 항목에만 붙는다.
CREATE TABLE order_items (
  id               bigserial PRIMARY KEY,
  order_id         bigint    NOT NULL REFERENCES orders(id),
  schedule_seat_id bigint    NOT NULL UNIQUE REFERENCES schedule_seats(id),
  price            integer   NOT NULL
);

CREATE INDEX ix_order_items_order ON order_items (order_id);

-- ---------------------------------------------------------------- 결제

-- 주문당 여러 행 가능. 실패한 승인 시도도 남긴다 — 정산 분쟁의 유일한 근거.
CREATE TABLE payments (
  id           bigserial   PRIMARY KEY,
  order_id     bigint      NOT NULL REFERENCES orders(id),
  pg_tid       text        UNIQUE,        -- PG 거래번호. 웹훅 중복 판정 키
  status       pay_status  NOT NULL,
  amount       integer     NOT NULL,
  -- 취소 시점의 수수료 계산 결과 스냅샷. 정책이 바뀌어도 과거 근거는 안 흔들린다 (§7.3).
  fee_snapshot jsonb,
  requested_at timestamptz NOT NULL DEFAULT now(),
  approved_at  timestamptz,
  refunded_at  timestamptz
);

CREATE INDEX ix_payments_order ON payments (order_id);

-- ---------------------------------------------------------------- outbox

-- 확정 트랜잭션과 같이 커밋된다 (§7.1).
-- "좌석은 잡혔는데 알림톡이 안 갔다"가 원천적으로 생기지 않는다.
CREATE TABLE outbox (
  id           bigserial   PRIMARY KEY,
  topic        text        NOT NULL,
  payload      jsonb       NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz
);

CREATE INDEX ix_outbox_unpublished ON outbox (created_at)
  WHERE published_at IS NULL;

COMMIT;
