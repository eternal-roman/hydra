"""Distilled memory tests \u2014 Phase 5."""
import sys
import pathlib
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.memory import DistilledMemory, MEMORY_BUDGET_BYTES


def _fresh(tmp: pathlib.Path):
    return DistilledMemory(user_id="test", companion_id="apex", root=tmp)


def test_remember_and_recall():
    with tempfile.TemporaryDirectory() as td:
        m = _fresh(pathlib.Path(td))
        m.remember("goals", "saving for daughter's college")
        assert any(e.fact == "saving for daughter's college" for e in m.recall("goals"))


def test_dedupe_same_fact_refreshes_ts():
    with tempfile.TemporaryDirectory() as td:
        m = _fresh(pathlib.Path(td))
        m.remember("goals", "x")
        first_ts = m.recall("goals")[0].ts
        m.remember("goals", "x")
        second_ts = m.recall("goals")[0].ts
        assert second_ts >= first_ts
        assert len(m.recall("goals")) == 1


def test_forget_topic():
    with tempfile.TemporaryDirectory() as td:
        m = _fresh(pathlib.Path(td))
        m.remember("goals", "x")
        m.remember("risk_tolerance", "y")
        removed = m.forget("goals")
        assert removed == 1
        assert not m.recall("goals")
        assert m.recall("risk_tolerance")


def test_forget_all():
    with tempfile.TemporaryDirectory() as td:
        m = _fresh(pathlib.Path(td))
        m.remember("goals", "x")
        m.remember("risk_tolerance", "y")
        assert m.forget(None) == 2
        assert not m.recall()


def test_persistence_round_trip():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        m1 = _fresh(root)
        m1.remember("goals", "saving for college")
        # Reload
        m2 = _fresh(root)
        assert any(e.fact == "saving for college" for e in m2.recall("goals"))


def test_budget_enforced():
    with tempfile.TemporaryDirectory() as td:
        m = _fresh(pathlib.Path(td))
        # Pile facts until we exceed budget; older ones should drop.
        # Enough to trip the budget; smaller than before to keep the test quick.
        for i in range(200):
            m.remember(f"topic_{i % 10}", f"fact number {i} " + "x" * 80)
        assert len(m.compose_block().encode("utf-8")) <= MEMORY_BUDGET_BYTES
        # v2.26.2: assert eviction actually fired — the pre-fix code kept all
        # 200 entries and relied on compose_block() truncation, which made the
        # assertion above pass vacuously.
        assert len(m.recall()) < 200
        assert "...[trunc]" not in m.compose_block()
        # Oldest facts evicted, newest retained (LRU by timestamp).
        facts = [e.fact for e in m.recall()]
        assert any("fact number 199 " in f for f in facts)
        assert not any("fact number 0 " in f for f in facts)


def test_budget_survives_reload():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        m1 = _fresh(root)
        for i in range(200):
            m1.remember("notes", f"fact number {i} " + "x" * 80)
        m2 = _fresh(root)  # persisted JSONL must already be within budget
        assert len(m2._render().encode("utf-8")) <= MEMORY_BUDGET_BYTES


def test_oversized_fact_capped():
    with tempfile.TemporaryDirectory() as td:
        m = _fresh(pathlib.Path(td))
        m.remember("dump", "y" * 10_000)
        entries = m.recall("dump")
        assert len(entries) == 1
        assert len(entries[0].fact.encode("utf-8")) <= MEMORY_BUDGET_BYTES
        assert entries[0].fact.endswith("...[trunc]")


def test_compose_block_empty():
    with tempfile.TemporaryDirectory() as td:
        m = _fresh(pathlib.Path(td))
        assert m.compose_block() == ""


def test_compose_block_groups_by_topic():
    with tempfile.TemporaryDirectory() as td:
        m = _fresh(pathlib.Path(td))
        m.remember("goals", "A")
        m.remember("goals", "B")
        m.remember("risk_tolerance", "C")
        block = m.compose_block()
        assert "**goals**" in block
        assert "**risk_tolerance**" in block
        assert "A" in block and "B" in block and "C" in block


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all memory tests passed")
