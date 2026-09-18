"""Focused tests for MemoryStore updates."""

from aim_server.db import init_db, connect
from aim_server.memory_models import MemoryType, StoreMemoryRequest, UpdateMemoryRequest
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
