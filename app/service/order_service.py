"""주문 사가 — 설계 문서: 확정 트랜잭션

confirm_paid() 는 동기 응답·웹훅·리컨실러가 **공유하는 단 하나의 확정 경로**다. 갈라지면 셋 다 다르게 틀린다.
"""

# TODO(3주차): create_pending(), confirm_paid(), cancel()
