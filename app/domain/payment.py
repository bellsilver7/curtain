"""결제 결과의 어휘 — 설계 문서: 결제 사가

이 모듈은 표준 라이브러리만 import 한다. DB도 Redis도 PG사 SDK도 모른다
(계층 규칙).

여기서 정하는 것은 하나뿐이고, 그 하나가 이 설계의 전제다 —
**승인 요청의 결과는 두 가지가 아니라 세 가지다.**

>>> [r.value for r in PayResult]
['APPROVED', 'DECLINED', 'UNKNOWN']

UNKNOWN 을 별도 값으로 두지 않으면 무응답을 실패에 접어 넣게 되고, 그러면
"돈은 나갔는데 좌석이 없다"가 구조적으로 가능해진다. 무응답은 결론이 아니라
"아직 모른다"이고, 결론은 PG사에 되물어서만 얻는다
(원칙 "외부 호출은 리컨실러가 받친다").
"""

from __future__ import annotations

from enum import StrEnum


class PayResult(StrEnum):
    """승인·환불 시도의 결과.

    >>> PayResult.UNKNOWN.is_conclusive
    False
    >>> PayResult.DECLINED.is_conclusive
    True
    """

    #: 돈이 나갔다.
    APPROVED = "APPROVED"
    #: 돈이 나가지 않았다. 이것도 결론이다.
    DECLINED = "DECLINED"
    #: 응답을 못 받았다. 승인됐는지 아닌지 모른다.
    UNKNOWN = "UNKNOWN"

    @property
    def is_conclusive(self) -> bool:
        """이 결과만으로 주문을 확정하거나 취소해도 되는가.

        UNKNOWN 에서만 False 다. 호출부가 `if result == APPROVED: ... else: 취소`
        로 쓰는 것을 막기 위해 있는 성질이다 — 그 else 에 UNKNOWN 이 섞이는 것이
        이 도메인에서 가장 비싼 버그다.
        """
        return self is not PayResult.UNKNOWN
