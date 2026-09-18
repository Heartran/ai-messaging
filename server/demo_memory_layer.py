"""Prototype demonstration of the Memory Layer (Issue #11).

This script demonstrates the classification, storage, retrieval, and
supersession capabilities of the Memory Layer for AI agents.
"""

import json
import sqlite3
import tempfile
from pathlib import Path

from aim_server.db import init_db, connect, now_utc
from aim_server.memory_models import (
    MemoryType,
    MemoryStatus,
    StoreMemoryRequest,
    UpdateMemoryRequest,
    SupersedeMemoryRequest,
    DisputeMemoryRequest,
    SearchMemoriesRequest,
)
from aim_server.memory_store import MemoryStore


def demo_memory_layer():
    """Run a comprehensive demonstration of the Memory Layer."""
    
    # Create a temporary database for the demo
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "demo_aim.db")
        init_db(db_path)
        conn = connect(db_path)
        
        try:
            print("=" * 70)
            print("MEMORY LAYER PROTOTYPE - Issue #11: Cognitive Memory for AIM")
            print("=" * 70)
            print()
            
            # Initialize memory store
            store = MemoryStore(conn)
            
            # Insert a test participant (agent)
            agent_id = _setup_test_agent(conn)
            
            print("1. STORING MEMORIES - Four Classification Types")
            print("-" * 70)
            
            # Store a FACT
            fact_req = StoreMemoryRequest(
                memory_type=MemoryType.FACT,
                content="Supabase is the backend for the beta release.",
                confidence=0.98,
                tags=["backend", "infrastructure", "beta"]
            )
            fact_mem = store.store_memory(fact_req, agent_id)
            print(f"[OK] Stored FACT: {fact_mem.content}")
            print(f"  ID: {fact_mem.memory_id}, Confidence: {fact_mem.confidence}")
            print()
            
            # Store a DECISION
            decision_req = StoreMemoryRequest(
                memory_type=MemoryType.DECISION,
                content="QR ticket validation uses SHA-256 hash combined with isActive flag.",
                confidence=0.95,
                tags=["qr-system", "ticket", "validation"],
                metadata={"reasoning": "SHA-256 provides collision resistance", "date": "2026-09-18"}
            )
            decision_mem = store.store_memory(decision_req, agent_id)
            print(f"[OK] Stored DECISION: {decision_mem.content}")
            print(f"  ID: {decision_mem.memory_id}, Confidence: {decision_mem.confidence}")
            print(f"  Metadata: {decision_mem.metadata}")
            print()
            
            # Store CONTEXT (temporary)
            context_req = StoreMemoryRequest(
                memory_type=MemoryType.CONTEXT,
                content="Currently working on QR code integration for ticket validation.",
                confidence=0.90,
                tags=["qr-system", "work-in-progress"]
            )
            context_mem = store.store_memory(context_req, agent_id)
            print(f"[OK] Stored CONTEXT: {context_mem.content}")
            print()
            
            # Store KNOWLEDGE (derived)
            knowledge_req = StoreMemoryRequest(
                memory_type=MemoryType.KNOWLEDGE,
                content="TicketOne and VivaTicket sources have different isActive semantics.",
                confidence=0.85,
                tags=["qr-system", "ticket-sources", "data-model"],
                metadata={"impact": "high", "documented": True}
            )
            knowledge_mem = store.store_memory(knowledge_req, agent_id)
            print(f"[OK] Stored KNOWLEDGE: {knowledge_mem.content}")
            print()
            
            print("2. SEARCHING MEMORIES - Compact Retrieval")
            print("-" * 70)
            
            # Search by type
            search_req = SearchMemoriesRequest(
                memory_types=[MemoryType.DECISION, MemoryType.FACT],
                limit=10
            )
            total, results = store.search_memories(search_req)
            print(f"Found {total} decisions and facts:")
            for mem in results:
                print(f"  - [{mem.memory_type.value}] {mem.content[:60]}...")
            print()
            
            # Search by tags
            search_req = SearchMemoriesRequest(
                tags=["qr-system"],
                limit=10
            )
            total, results = store.search_memories(search_req)
            print(f"Found {total} memories tagged 'qr-system':")
            for mem in results:
                print(f"  - [{mem.memory_type.value}] {mem.content[:60]}...")
            print()
            
            # Search by text query
            search_req = SearchMemoriesRequest(
                query="backend",
                limit=10
            )
            total, results = store.search_memories(search_req)
            print(f"Found {total} memories mentioning 'backend':")
            for mem in results:
                print(f"  - {mem.content}")
            print()
            
            print("3. SUPERSESSION - Evolution of Decisions")
            print("-" * 70)
            
            # Store a revised decision
            revised_decision_req = StoreMemoryRequest(
                memory_type=MemoryType.DECISION,
                content="QR ticket validation now uses SHA-256 with HMAC for additional security.",
                confidence=0.97,
                tags=["qr-system", "ticket", "validation", "security"]
            )
            revised_mem = store.store_memory(revised_decision_req, agent_id)
            print(f"[OK] Stored revised DECISION: {revised_mem.content}")
            print()
            
            # Mark old decision as superseded
            supersede_req = SupersedeMemoryRequest(
                superseded_memory_id=decision_mem.memory_id,
                superseding_memory_id=revised_mem.memory_id,
                reason="Added HMAC for security hardening based on threat analysis"
            )
            old_mem, new_mem = store.supersede_memory(supersede_req)
            print(f"[OK] Superseded MEM-{old_mem.memory_id} with MEM-{new_mem.memory_id}")
            print(f"  Old status: {old_mem.status.value} -> New status: {new_mem.status.value}")
            print()
            
            print("4. DISPUTE HANDLING - Contradictions")
            print("-" * 70)
            
            # Store a conflicting fact
            conflicting_req = StoreMemoryRequest(
                memory_type=MemoryType.FACT,
                content="PostgreSQL is the backend database for the system.",
                confidence=0.80,
                tags=["backend", "database"]
            )
            conflicting_mem = store.store_memory(conflicting_req, agent_id)
            print(f"[OK] Stored fact: {conflicting_mem.content}")
            print()
            
            # Flag it as disputed
            dispute_req = DisputeMemoryRequest(
                memory_id=fact_mem.memory_id,
                conflicting_memory_id=conflicting_mem.memory_id,
                reason="Conflict with MEM-{}: unclear whether Supabase or PostgreSQL is the actual backend".format(conflicting_mem.memory_id)
            )
            disputed_mem = store.dispute_memory(dispute_req)
            print(f"[OK] Marked MEM-{disputed_mem.memory_id} as {disputed_mem.status.value}")
            print(f"  Reason: Conflicting information about backend choice")
            print()
            
            print("5. PROJECT CONTEXT - Compact Knowledge Retrieval")
            print("-" * 70)
            
            # Get project context
            context = store.get_project_context(project_id=None)
            print(f"Project Context Summary:")
            print(f"  Total Memories: {context.total_memories}")
            print(f"  Key Decisions: {len(context.key_decisions)}")
            print(f"  Key Facts: {len(context.key_facts)}")
            print(f"  Active Context: {len(context.active_context)}")
            print(f"  Derived Knowledge: {len(context.derived_knowledge)}")
            print()
            print("Context Summary:")
            print(context.summary)
            print()
            
            print("6. UPDATING MEMORIES - Refinement")
            print("-" * 70)
            
            # Update confidence
            update_req = UpdateMemoryRequest(
                memory_id=context_mem.memory_id,
                confidence=0.95
            )
            updated_mem = store.update_memory(update_req)
            print(f"[OK] Updated MEM-{updated_mem.memory_id}")
            print(f"  Old confidence: 0.90 -> New confidence: {updated_mem.confidence}")
            print()
            
            print("7. STATISTICS")
            print("-" * 70)
            
            # Count memories by type
            for mtype in MemoryType:
                search_req = SearchMemoriesRequest(memory_types=[mtype])
                count, _ = store.search_memories(search_req)
                print(f"  {mtype.value}: {count} memories")
            
            print()
            print("=" * 70)
            print("PROTOTYPE DEMONSTRATION COMPLETE")
            print("=" * 70)
            print()
            print("Key Features Demonstrated:")
            print("+ Memory Classification (FACT, DECISION, CONTEXT, KNOWLEDGE)")
            print("+ Provenance Tracking (Creator, Timestamp, Source)")
            print("+ Confidence Levels (0.0-1.0 for belief tracking)")
            print("+ Semantic Tags (for efficient retrieval)")
            print("+ Supersession Relationships (evolution of decisions)")
            print("+ Dispute Handling (contradiction tracking)")
            print("+ Compact Project Context (20-30 relevant memories vs infinite chat)")
            print("+ Full-text & Tag-based Search")
            print()
            print("Next Steps:")
            print("-> Add vector embeddings (EmbeddingGemma) for semantic search")
            print("-> Implement retrieval ranking by relevance")
            print("-> Add API endpoints to HTTP server")
            print("-> Integrate with MCP protocol")
            print("-> Add agent memory vs project memory distinction")
            print()
        
        finally:
            conn.close()


def _setup_test_agent(conn: sqlite3.Connection) -> int:
    """Insert a test agent participant."""
    now = now_utc()
    cursor = conn.execute(
        """
        INSERT INTO participants
            (name, machine, client_type, agent_type, registered_at, token_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("Test Agent", "dev-machine", "code", "claude", now, "test_token_hash")
    )
    conn.commit()
    return cursor.lastrowid


if __name__ == "__main__":
    demo_memory_layer()
