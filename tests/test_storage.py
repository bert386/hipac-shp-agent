"""Storage prune tests. Run: python tests/test_storage.py"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hipac_agent.storage import Storage  # noqa: E402


def _save(store, mac, n):
    ids = []
    for _ in range(n):
        ids.append(store.save_result(
            {"receiver": {"mac_address": mac, "ip_address": "1.2.3.4"}, "nodes": []},
            "raw", "2026-01-01T00:00:00Z", "1.2.3.4",
        ))
    return ids


def test_prune_keeps_latest_uploaded_per_receiver():
    store = Storage(":memory:")
    ids = _save(store, "aa:bb", 8)
    store.mark_uploaded(ids)                    # all uploaded

    removed = store.prune(keep_per_receiver=5)
    assert removed == 3, removed
    # remaining: 5; pending count still 0
    assert store.pending_count() == 0
    # pruning again is a no-op
    assert store.prune(keep_per_receiver=5) == 0


def test_prune_never_deletes_pending():
    store = Storage(":memory:")
    ids = _save(store, "cc:dd", 6)
    store.mark_uploaded(ids[:2])                # only 2 uploaded, 4 pending
    removed = store.prune(keep_per_receiver=1)  # keep only newest 1
    # newest are pending (not deletable); only uploaded-beyond-keep get removed
    assert store.pending_count() == 4
    assert removed <= 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("All storage tests passed.")
