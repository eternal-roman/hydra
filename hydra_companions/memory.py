"""Per-companion distilled memory.

Companions write durable facts about the user via the `remember` tool
(Phase 5+). Facts are topic-bucketed and loaded into the system prompt
on every subsequent turn.

Storage: `.hydra-companions/memory/{user}_{companion}.jsonl`
Each line: `{"ts": float, "topic": str, "fact": str}`

Budget: 4KB per-companion (LRU eviction by timestamp).

Phase 5 ships without actual tool-use wiring (Phase 1 still runs
pre-rendered context injection). When the Phase 5 branch lands, the
Companion.respond() loop is updated to include distilled memory in the
system prompt block before calling the provider.
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hydra_companions.config import MEMORY_DIR


MEMORY_BUDGET_BYTES = 4096
# Cap per fact so a single oversized fact can never exceed the whole
# budget on its own (which would force eviction to wipe every other entry).
MAX_FACT_BYTES = 1024


@dataclass(frozen=True)
class MemoryEntry:
    ts: float
    topic: str
    fact: str


class DistilledMemory:
    def __init__(self, user_id: str, companion_id: str, root: Optional[Path] = None):
        self.user_id = user_id
        self.companion_id = companion_id
        self._path = (root or MEMORY_DIR) / f"{user_id}_{companion_id}.jsonl"
        self._entries: list[MemoryEntry] = []
        self._load()

    # ----- I/O -----

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            for ln in self._path.read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                try:
                    d = json.loads(ln)
                    self._entries.append(MemoryEntry(
                        ts=float(d.get("ts", 0)),
                        topic=str(d.get("topic", "")),
                        fact=str(d.get("fact", "")),
                    ))
                except (json.JSONDecodeError, ValueError):
                    continue
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("w", encoding="utf-8") as f:
                for e in self._entries:
                    f.write(json.dumps({"ts": e.ts, "topic": e.topic, "fact": e.fact}) + "\n")
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")

    # ----- public API -----

    def remember(self, topic: str, fact: str) -> None:
        topic = (topic or "").strip().lower() or "general"
        fact = (fact or "").strip()
        if not fact:
            return
        raw = fact.encode("utf-8")
        if len(raw) > MAX_FACT_BYTES:
            fact = raw[:MAX_FACT_BYTES].decode("utf-8", errors="ignore") + " ...[trunc]"
        now = time.time()
        # Dedupe: if same topic + fact already present, just refresh ts.
        for i, e in enumerate(self._entries):
            if e.topic == topic and e.fact == fact:
                self._entries[i] = MemoryEntry(ts=now, topic=topic, fact=fact)
                self._enforce_budget()
                self._persist()
                return
        self._entries.append(MemoryEntry(ts=now, topic=topic, fact=fact))
        self._enforce_budget()
        self._persist()

    def recall(self, topic: Optional[str] = None) -> list[MemoryEntry]:
        if topic is None:
            return list(self._entries)
        t = topic.strip().lower()
        return [e for e in self._entries if e.topic == t]

    def forget(self, topic: Optional[str] = None) -> int:
        """Drop entries by topic (or all when topic is None). Returns count removed."""
        if topic is None:
            n = len(self._entries)
            self._entries.clear()
            self._persist()
            return n
        t = topic.strip().lower()
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.topic != t]
        self._persist()
        return before - len(self._entries)

    def compose_block(self, max_bytes: int = MEMORY_BUDGET_BYTES) -> str:
        """Render as a compact markdown block for system-prompt injection."""
        blob = self._render()
        if blob and len(blob.encode("utf-8")) > max_bytes:
            blob = blob[:max_bytes - 12] + "\n...[trunc]"
        return blob

    def _render(self) -> str:
        """Untruncated render — budget enforcement must measure this, not
        compose_block(), whose truncation would mask any overage."""
        if not self._entries:
            return ""
        buckets: dict[str, list[str]] = {}
        for e in self._entries:
            buckets.setdefault(e.topic, []).append(e.fact)
        lines = ["## What you remember about the user"]
        for topic in sorted(buckets):
            lines.append(f"- **{topic}**:")
            for f in buckets[topic]:
                lines.append(f"  - {f}")
        return "\n".join(lines)

    # ----- budget enforcement -----

    def _enforce_budget(self) -> None:
        """LRU eviction by timestamp when the untruncated render exceeds
        budget. The >1 guard keeps the newest fact even in the pathological
        single-oversized-entry case (MAX_FACT_BYTES makes that unreachable
        in practice)."""
        while len(self._entries) > 1 and len(self._render().encode("utf-8")) > MEMORY_BUDGET_BYTES:
            # drop oldest
            self._entries.sort(key=lambda e: e.ts)
            self._entries.pop(0)
