# .venv 가 있으면 activate 없이도 그 안의 실행파일을 쓴다.
# make 는 셸을 새로 띄우므로 activate 여부에 기대면 이 파일이 깨지기 쉽다.
VENV    := .venv
ALEMBIC := $(if $(wildcard $(VENV)/bin/alembic),$(VENV)/bin/alembic,alembic)
PYTEST  := $(if $(wildcard $(VENV)/bin/pytest),$(VENV)/bin/pytest,pytest)
UVICORN := $(if $(wildcard $(VENV)/bin/uvicorn),$(VENV)/bin/uvicorn,uvicorn)
LOCUST  := $(if $(wildcard $(VENV)/bin/locust),$(VENV)/bin/locust,locust)
RUFF    := $(if $(wildcard $(VENV)/bin/ruff),$(VENV)/bin/ruff,ruff)
MYPY    := $(if $(wildcard $(VENV)/bin/mypy),$(VENV)/bin/mypy,mypy)
PYTHON  := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)

# 이 프로젝트가 요구하는 최소 파이썬. macOS 기본 python3 은 아직 3.9 인 경우가 많다.
PY_BOOTSTRAP ?= python3

.PHONY: help install check-deps up down migrate revision migrate-down migrate-sql drift \
        api worker test test-unit test-concurrency load lint fmt

help:          ## 사용 가능한 타깃
	@awk 'match($$0, /^[a-zA-Z_-]+:.*## /) { \
	        split($$0, a, "## "); sub(/:.*/, "", $$1); \
	        printf "  %-18s %s\n", $$1, a[2] }' $(MAKEFILE_LIST)

install:       ## 가상환경 생성 + 의존성 설치 (처음 한 번)
	@$(PY_BOOTSTRAP) -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
	  || { echo ""; \
	       echo "  $(PY_BOOTSTRAP) 이 3.11 미만입니다. pyproject.toml 은 3.11+ 를 요구합니다."; \
	       echo "  버전을 지정해 다시 실행하세요:  make install PY_BOOTSTRAP=python3.12"; \
	       echo ""; exit 1; }
	$(PY_BOOTSTRAP) -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -e ".[dev]"
	@echo ""
	@echo "  완료. make 는 .venv 를 자동으로 찾으므로 activate 없이 바로 쓸 수 있습니다."
	@echo "  직접 명령을 칠 때만:  source $(VENV)/bin/activate"
	@echo ""

check-deps:
	@command -v $(ALEMBIC) >/dev/null 2>&1 || { \
	  echo ""; \
	  echo "  의존성이 설치되지 않았습니다 (alembic 을 찾을 수 없음)."; \
	  echo "  처음이라면:  make install"; \
	  echo ""; exit 1; }

up:            ## 로컬 postgres + redis 기동
	docker compose up -d --wait

down:          ## 컨테이너와 볼륨까지 정리
	docker compose down -v

migrate: check-deps up  ## 스키마 적용 (최신 리비전까지)
	$(ALEMBIC) upgrade head

revision: check-deps    ## 모델 변경 후 리비전 생성. 생성물은 반드시 읽고 손볼 것 (ADR 0002)
	@test -n "$(m)" || (echo 'usage: make revision m="설명"'; exit 1)
	$(ALEMBIC) revision --autogenerate -m "$(m)"

migrate-down: check-deps  ## 한 단계 되돌리기
	$(ALEMBIC) downgrade -1

migrate-sql: check-deps   ## DB에 대지 않고 SQL만 출력 (리뷰용)
	$(ALEMBIC) upgrade head --sql

drift: check-deps up      ## 모델과 DB가 어긋났는지 확인. CI에 걸어두면 좋다
	$(ALEMBIC) check

api: check-deps           ## 개발 서버 (http://localhost:8000/docs)
	$(UVICORN) app.main:app --reload --port 8000

worker: check-deps        ## 배치 워커 4종 동시 실행 (§2.1)
	$(PYTHON) -m app.worker

test-unit: check-deps     ## DB 없이 도는 도메인 테스트만
	$(PYTEST) -m "not integration" -q

test: check-deps up       ## 전체. docs/design.md §10 표를 그대로 실행한다
	$(PYTEST) -q

test-concurrency: check-deps up  ## 2주차의 진짜 산출물
	$(PYTEST) tests/test_concurrency.py -q

load: check-deps up       ## 오픈런 부하 (§10 마지막 두 줄)
	$(LOCUST) -f load/locustfile.py --host http://localhost:8000

lint: check-deps          ## ruff + mypy
	$(RUFF) check app tests && $(MYPY)

fmt: check-deps           ## 포매팅 + 자동 수정
	$(RUFF) format app tests && $(RUFF) check --fix app tests
