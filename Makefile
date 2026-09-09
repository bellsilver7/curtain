.PHONY: up down migrate revision migrate-down migrate-sql drift api worker test test-unit test-concurrency load lint fmt

up:            ## 로컬 postgres + redis 기동
	docker compose up -d --wait

down:
	docker compose down -v

migrate: up    ## 스키마 적용 (최신 리비전까지)
	alembic upgrade head

revision:      ## 모델 변경 후 리비전 생성. 생성물은 반드시 읽고 손볼 것 (ADR 0002)
	@test -n "$(m)" || (echo 'usage: make revision m="설명"'; exit 1)
	alembic revision --autogenerate -m "$(m)"

migrate-down:  ## 한 단계 되돌리기
	alembic downgrade -1

migrate-sql:   ## DB에 대지 않고 SQL만 출력 (리뷰용)
	alembic upgrade head --sql

drift: up      ## 모델과 DB가 어긋났는지 확인. CI에 걸어두면 좋다
	alembic check

api:
	uvicorn app.main:app --reload --port 8000

worker:        ## 배치 워커 4종 동시 실행 (§2.1)
	python -m app.worker

test-unit:     ## DB 없이 도는 도메인 테스트만
	pytest -m "not integration" -q

test: up       ## 전체. §10 표를 그대로 실행한다
	pytest -q

test-concurrency: up  ## 2주차의 진짜 산출물
	pytest tests/test_concurrency.py -q -p no:randomly

load: up       ## 오픈런 부하 (§10 마지막 두 줄)
	locust -f load/locustfile.py --host http://localhost:8000

lint:
	ruff check app tests && mypy

fmt:
	ruff format app tests && ruff check --fix app tests
