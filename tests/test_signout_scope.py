"""웹 콘솔·스크립트의 Supabase 로그아웃이 전부 scope local 인지 검사.

supabase-js signOut() / supabase-py sign_out() 기본 범위는 global — 그 계정의 모든 기기 세션을
지운다. 10/8 베타 패널 계정 일괄 조회(로그인→signOut ×25)가 테스터 폰 앱 세션을 전부 날려
앱이 refresh_token_not_found 로 로그아웃됐다. 새 핸들러를 복붙하다 기본값이 다시 들어오는 걸 막는다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# signOut( / sign_out( 호출과 괄호 안 인자 (한 줄 기준 — 지금 호출은 전부 한 줄)
CALL = re.compile(r"\.(signOut|sign_out)\(([^)]*)\)")
LOCAL = re.compile(r"""scope["']?\s*:\s*["']local["']""")

TARGETS = [ROOT / "web" / "index.html", *sorted((ROOT / "scripts").glob("*.py"))]


def _calls(path: Path) -> list[tuple[int, str]]:
    out = []
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        out += [(no, m.group(0)) for m in CALL.finditer(line)]
    return out


@pytest.mark.parametrize("path", TARGETS, ids=lambda p: str(p.relative_to(ROOT)))
def test_signout_is_local_scope(path: Path) -> None:
    bad = [f"{path.name}:{no} {call}" for no, call in _calls(path) if not LOCAL.search(call)]
    assert not bad, "scope local 없는 로그아웃 (기본 global 은 앱 세션까지 지움):\n" + "\n".join(bad)


def test_web_console_has_signout_calls() -> None:
    """정규식이 아무것도 못 잡아서 통과하는 걸 막는 가드."""
    assert len(_calls(ROOT / "web" / "index.html")) >= 9
