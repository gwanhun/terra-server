"""아주 작은 인메모리 Supabase 흉내 — OTA 테스트용.

MagicMock 체인(select→eq→in_→order→limit→execute)은 호출 순서가 조금만 달라도 깨져서,
필터·정렬·갱신이 실제로 동작하는 가짜를 둔다. 지원: table / select / eq / neq / in_ / is_ /
order / limit / single / insert / update / execute. 그 외 메서드는 AttributeError.
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
        self._filters: list[tuple[str, str, Any]] = []
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None
        self._single = False
        self._op: str | None = None
        self._payload: Any = None

    # --- builder ---
    def select(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self._filters.append(("eq", col, val)); return self

    def neq(self, col: str, val: Any) -> "_Query":
        self._filters.append(("neq", col, val)); return self

    def in_(self, col: str, vals: list[Any]) -> "_Query":
        self._filters.append(("in", col, list(vals))); return self

    def is_(self, col: str, val: Any) -> "_Query":
        self._filters.append(("is", col, None if val == "null" else val)); return self

    def order(self, col: str, desc: bool = False) -> "_Query":
        self._order = (col, desc); return self

    def limit(self, n: int) -> "_Query":
        self._limit = n; return self

    def single(self) -> "_Query":
        self._single = True; return self

    def insert(self, payload: Any) -> "_Query":
        self._op, self._payload = "insert", payload; return self

    def update(self, payload: dict[str, Any]) -> "_Query":
        self._op, self._payload = "update", payload; return self

    # --- exec ---
    def _match(self, row: dict[str, Any]) -> bool:
        for op, col, val in self._filters:
            v = row.get(col)
            if op == "eq" and v != val:
                return False
            if op == "neq" and v == val:
                return False
            if op == "in" and v not in val:
                return False
            if op == "is" and v != val:
                return False
        return True

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
        matched = [r for r in self._rows if self._match(r)]
        if self._op == "update":
            for r in matched:
                r.update(self._payload)
            return _Result([dict(r) for r in matched])
        if self._order:
            col, desc = self._order
            matched.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
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
