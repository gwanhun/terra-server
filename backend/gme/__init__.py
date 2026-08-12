"""GME(Gecko Motion Engine) presence 결과 → 저장 목적지 라우팅 (terra-server 측).

이 패키지는 **판정(decision)을 만들지 않는다.** 판정은 gecko-vision-gate 의
`gme_presence` wrapper 가 생성하고, 여기서는 그 판정을 서버의 저장 목적지로
매핑하는 **라우팅 + dry-run 계획**만 담당한다.

## dependency 핀
판정을 만드는 gecko-vision-gate 코어는 아래 commit 에 **고정**한다.
실제 분석기(analyzer)는 이 commit 의 `gme_presence.analyze_presence` 를 써야 한다.
(terra-server 런타임에는 torch 를 끌어오지 않도록 analyzer 를 주입받는 구조 —
 실제 설치·R2 write 는 리뷰 후 별도 단계에서 진행)
"""

from __future__ import annotations

PINNED_GME_COMMIT = "077bed1d643819055651914e2685c68e3781c584"
GME_SOURCE_REPO = "https://github.com/S-Soo100/gecko-vision-gate.git"
