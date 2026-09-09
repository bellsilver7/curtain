.PHONY: up down migrate api worker test test-unit test-concurrency load lint fmt

up:            ## 로컬 postgres + redis 기동
	docker compose up -d --wait

down:
	docker compose down -v

migrate: up    ## 스키마 적용
	docker compose exec -T postgres psql -U curtain -d curtain < migrations/0001_init.sql

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
