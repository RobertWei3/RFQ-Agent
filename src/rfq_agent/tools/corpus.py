"""Search over a generated page corpus, with near-miss decoys and fault injection."""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from rfq_agent.config import FaultSettings
from rfq_agent.partno import normalize_part_no
from rfq_agent.tools.base import ToolHit, ToolTimeoutError

MIN_DECOY_PREFIX = 2


@dataclass(frozen=True)
class _IndexedPage:
    hit: ToolHit
    keys: tuple[str, ...]


class PageCorpusTool:
    """Exact matches on normalized part numbers plus lexicographic near-miss decoys.

    Decoys are pages whose key shares a long prefix with the query, mimicking
    the noisy results of a real site search.
    """

    def __init__(
        self,
        name: str,
        pages: list[_IndexedPage],
        *,
        top_k: int,
        max_decoys: int,
        faults: FaultSettings,
    ) -> None:
        self.name = name
        self.top_k = top_k
        self.max_decoys = max_decoys
        self.faults = faults
        self._by_key: dict[str, list[ToolHit]] = {}
        for page in pages:
            for key in page.keys:
                self._by_key.setdefault(key, []).append(page.hit)
        self._sorted_keys = sorted(self._by_key)

    @classmethod
    def from_jsonl(
        cls, name: str, path: Path, *, top_k: int, max_decoys: int, faults: FaultSettings
    ) -> PageCorpusTool:
        pages = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                record = json.loads(line)
                hit = ToolHit(page_id=record["page_id"], title=record["title"], text=record["text"])
                pages.append(_IndexedPage(hit, tuple(record["keys"])))
        return cls(name, pages, top_k=top_k, max_decoys=max_decoys, faults=faults)

    async def search(self, query: str) -> list[ToolHit]:
        key = normalize_part_no(query)
        if not key or self._inject_fault(key):
            return []
        exact = sorted(self._by_key.get(key, []), key=lambda h: h.page_id)
        return (exact + self._decoys(key))[: self.top_k]

    def _decoys(self, key: str) -> list[ToolHit]:
        if self.max_decoys == 0:
            return []
        keys = self._sorted_keys
        pos = bisect.bisect_left(keys, key)
        window = keys[max(0, pos - self.max_decoys) : pos + self.max_decoys + 1]
        scored = [
            (_common_prefix(k, key), k)
            for k in window
            if k != key and _common_prefix(k, key) >= MIN_DECOY_PREFIX
        ]
        scored.sort(key=lambda item: (-item[0], item[1]))
        hits: list[ToolHit] = []
        for _, neighbour in scored[: self.max_decoys]:
            hits.extend(sorted(self._by_key[neighbour], key=lambda h: h.page_id))
        return hits

    def _inject_fault(self, key: str) -> bool:
        """Raise an injected timeout, or return True for an injected empty result."""
        faults = self.faults
        if not faults.enabled:
            return False
        digest = hashlib.sha256(f"{faults.seed}:{self.name}:{key}".encode()).digest()
        draw = int.from_bytes(digest[:8], "big") / 2**64
        if draw < faults.timeout_rate:
            raise ToolTimeoutError(f"{self.name} timed out for {key!r} (injected)")
        return draw < faults.timeout_rate + faults.empty_rate


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n
