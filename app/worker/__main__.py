"""워커 4종 동시 실행 — 설계 문서: 배치 워커

make worker 의 엔트리포인트. 각 tick 주기는 policy.py 가 아니라 여기서 관리한다.
"""

# TODO(2주차): asyncio.gather 로 4개 루프. 하나가 죽어도 나머지는 살아야 한다
