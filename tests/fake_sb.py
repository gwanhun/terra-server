"""아주 작은 인메모리 Supabase 흉내 — OTA 테스트용.

MagicMock 체인(select→eq→in_→order→limit→execute)은 호출 순서가 조금만 달라도 깨져서,
필터·정렬·갱신이 실제로 동작하는 가짜를 둔다. 지원: table / select / eq / neq / in_ / is_ /
lt / gt / gte / not_(바로 다음 필터 1개 부정) / order / limit / single / insert / update /
upsert(on_conflict) / delete / execute. 그 외 메서드는 AttributeError.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    def __init__(self, store: dict[str, list[dict[str, Any]]], table: str) -> None:
        self._rows = store.setdefault(table, [])
        self._table = table
        self._filters: list[tuple[str, str, Any, bool]] = []
        self._negate = False
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None
        self._single = False
        self._op: str | None = None
        self._payload: Any = None
        self._on_conflict: list[str] = []

    def _add(self, op: str, col: str, val: Any) -> "_Query":
        self._filters.append((op, col, val, self._negate))
        self._negate = False
        return self

    # --- builder ---
    def select(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        return self._add("eq", col, val)

    def neq(self, col: str, val: Any) -> "_Query":
        return self._add("neq", col, val)

    def in_(self, col: str, vals: list[Any]) -> "_Query":
        return self._add("in", col, list(vals))

    def is_(self, col: str, val: Any) -> "_Query":
        return self._add("is", col, None if val == "null" else val)

    @property
    def not_(self) -> "_Query":
        # supabase-py 처럼 `.not_.is_("col", "null")` — 바로 다음 필터 하나를 부정
        self._negate = True
        return self

    def lt(self, col: str, val: Any) -> "_Query":
        # ISO 문자열 비교 — 테스트 시각은 aware datetime.isoformat()(+00:00) 로 통일할 것
        return self._add("lt", col, val)

    def gt(self, col: str, val: Any) -> "_Query":
        # lt 와 같은 문자열 비교. None 값 행은 불일치
        return self._add("gt", col, val)

    def gte(self, col: str, val: Any) -> "_Query":
        # lt 와 같은 ISO 문자열 비교. None 값 행은 불일치
        return self._add("gte", col, val)

    def order(self, col: str, desc: bool = False) -> "_Query":
        self._order = (col, desc); return self

    def limit(self, n: int) -> "_Query":
        self._limit = n; return self

    def range(self, lo: int, hi: int) -> "_Query":
        """PostgREST range(lo, hi) — 양끝 포함 오프셋 슬라이스."""
        self._range = (lo, hi); return self

    def single(self) -> "_Query":
        self._single = True; return self

    def insert(self, payload: Any) -> "_Query":
        self._op, self._payload = "insert", payload; return self

    def update(self, payload: dict[str, Any]) -> "_Query":
        self._op, self._payload = "update", payload; return self

    def upsert(self, payload: Any, on_conflict: str = "id") -> "_Query":
        self._op, self._payload = "upsert", payload
        self._on_conflict = [c.strip() for c in on_conflict.split(",")]
        return self

    def delete(self) -> "_Query":
        self._op = "delete"; return self

    # --- exec ---
    def _match(self, row: dict[str, Any]) -> bool:
        for op, col, val, neg in self._filters:
            if self._hit(op, row.get(col), val) == neg:
                return False
        return True

    @staticmethod
    def _hit(op: str, v: Any, val: Any) -> bool:
        if op == "eq":
            return v == val
        if op == "neq":
            return v != val
        if op == "in":
            return v in val
        if op == "is":
            return v == val
        if op == "lt":
            return v is not None and v < val
        if op == "gt":
            return v is not None and v > val
        if op == "gte":
            return v is not None and v >= val
        raise AttributeError(op)

    def execute(self) -> _Result:
        now = datetime.now(timezone.utc).isoformat()
        if self._op == "insert":
            rows = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in rows:
                row = {"id": str(uuid.uuid4()), "created_at": now, "updated_at": now, **dict(p)}
                self._rows.append(row)
                out.append(dict(row))
            return _Result(out)
        if self._op == "upsert":
            rows = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in rows:
                hit = next((r for r in self._rows
                            if all(r.get(c) == p.get(c) for c in self._on_conflict)), None)
                if hit is None:
                    hit = {"id": str(uuid.uuid4()), "created_at": now, **dict(p)}
                    self._rows.append(hit)
                else:
                    hit.update(p)
                out.append(dict(hit))
            return _Result(out)
        matched = [r for r in self._rows if self._match(r)]
        if self._op == "update":
            for r in matched:
                r.update(self._payload)
            return _Result([dict(r) for r in matched])
        if self._op == "delete":
            # supabase-py 처럼 지운 행 목록을 돌려준다. store 리스트는 제자리 수정.
            self._rows[:] = [r for r in self._rows if not self._match(r)]
            return _Result([dict(r) for r in matched])
        if self._order:
            col, desc = self._order
            matched.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
        rng = getattr(self, "_range", None)
        if rng is not None:
            matched = matched[rng[0] : rng[1] + 1]
        if self._limit is not None:
            matched = matched[: self._limit]
        if self._single:
            return _Result(dict(matched[0]) if matched else None)
        return _Result([dict(r) for r in matched])


class FakeSB:
    """`FakeSB(cameras=[...], ota_jobs=[...])` 처럼 초기 행을 넘긴다. `.rows("t")` 로 현재 상태 확인."""

    def __init__(self, **tables: list[dict[str, Any]]) -> None:
        self.store: dict[str, list[dict[str, Any]]] = {k: [dict(r) for r in v] for k, v in tables.items()}

    def table(self, name: str) -> _Query:
        return _Query(self.store, name)

    def rows(self, name: str) -> list[dict[str, Any]]:
        return self.store.setdefault(name, [])

    def one(self, name: str, **where: Any) -> dict[str, Any] | None:
        for r in self.rows(name):
            if all(r.get(k) == v for k, v in where.items()):
                return r
        return None


__all__ = ["FakeSB"]
