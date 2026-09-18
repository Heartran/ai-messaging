"""Focused tests for MemoryStore updates."""

from aim_server.db import init_db, connect
from aim_server.memory_models import (
    MemoryStatus,
    MemoryType,
    SearchMemoriesRequest,
    StoreMemoryRequest,
    UpdateMemoryRequest,
)
from aim_server.memory_store import MemoryStore


def test_tag_only_and_metadata_only_updates_are_applied(tmp_path):
    db_path = tmp_path / "memory.db"
    init_db(str(db_path))
    conn = connect(str(db_path))
    try:
        store = MemoryStore(conn)
        memory = store.store_memory(
            StoreMemoryRequest(
                memory_type=MemoryType.FACT,
                content="Original content",
                tags=["original"],
                metadata={"source": "test"},
            ),
            creator_id=None,
        )

        tagged = store.update_memory(
            UpdateMemoryRequest(memory_id=memory.memory_id, tags=["updated"])
        )
        assert tagged.tags == ["updated"]
        assert tagged.metadata == {"source": "test"}

        with_metadata = store.update_memory(
            UpdateMemoryRequest(
                memory_id=memory.memory_id,
                metadata={"source": "updated", "reviewed": True},
            )
        )
        assert with_metadata.tags == ["updated"]
        assert with_metadata.metadata == {"source": "updated", "reviewed": True}
    finally:
        conn.close()


def test_search_status_filter_includes_historical_memories(tmp_path):
    db_path = tmp_path / "memory.db"
    init_db(str(db_path))
    conn = connect(str(db_path))
    try:
        store = MemoryStore(conn)
        memories = [
            store.store_memory(
                StoreMemoryRequest(memory_type=MemoryType.FACT, content=status.value),
                creator_id=None,
            )
            for status in MemoryStatus
        ]
        for memory, status in zip(memories, MemoryStatus):
            store.update_memory(
                UpdateMemoryRequest(memory_id=memory.memory_id, status=status)
            )

        default_count, default_results = store.search_memories(SearchMemoriesRequest())
        assert default_count == 2
        assert {memory.status for memory in default_results} == {
            MemoryStatus.ACTIVE,
            MemoryStatus.DISPUTED,
        }

        for status in (MemoryStatus.SUPERSEDED, MemoryStatus.ARCHIVED):
            count, results = store.search_memories(
                SearchMemoriesRequest(status=status)
            )
            assert count == 1
            assert [memory.status for memory in results] == [status]
    finally:
        conn.close()
