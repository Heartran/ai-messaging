"""Memory Store service layer - operations for the Memory Layer.

Implements CRUD operations and semantic search for memories with full
provenance tracking, supersession, and dispute handling.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from aim_server.db import now_utc
from aim_server.memory_models import (
    MemoryType,
    MemoryStatus,
    MemoryResponse,
    StoreMemoryRequest,
    UpdateMemoryRequest,
    SupersedeMemoryRequest,
    DisputeMemoryRequest,
    SearchMemoriesRequest,
    ProjectContextResponse,
)


class MemoryStore:
    """Service for managing memories with provenance and semantic relationships."""

    def __init__(self, conn: sqlite3.Connection):
        """Initialize with database connection."""
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    def store_memory(
        self,
        request: StoreMemoryRequest,
        creator_id: int
    ) -> MemoryResponse:
        """Store a new memory with classification and provenance.

        Args:
            request: StoreMemoryRequest with memory content and classification
            creator_id: Participant ID of the agent creating this memory

        Returns:
            MemoryResponse with the stored memory and its ID
        """
        now = now_utc()
        metadata_json = json.dumps(request.metadata) if request.metadata else None

        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO memories
                    (memory_type, content, status, confidence, source_message_id,
                     project_id, creator_id, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.memory_type.value,
                    request.content,
                    MemoryStatus.ACTIVE.value,
                    request.confidence,
                    request.source_message_id,
                    request.project_id,
                    creator_id,
                    metadata_json,
                    now,
                    now,
                ),
            )
            memory_id = cursor.lastrowid

            # Store unique tags as one transaction with the memory row.
            for tag in dict.fromkeys(request.tags):
                self.conn.execute(
                    "INSERT INTO memory_tags (memory_id, tag) VALUES (?, ?)",
                    (memory_id, tag),
                )
        return self._fetch_memory(memory_id)

    def update_memory(
        self,
        request: UpdateMemoryRequest
    ) -> MemoryResponse:
        """Update an existing memory's properties.

        Args:
            request: UpdateMemoryRequest with updates to apply

        Returns:
            Updated MemoryResponse
        """
        now = now_utc()

        # Build update query dynamically based on provided fields
        updates = []
        params = []

        if request.content is not None:
            updates.append("content = ?")
            params.append(request.content)

        if request.confidence is not None:
            updates.append("confidence = ?")
            params.append(request.confidence)

        if request.status is not None:
            updates.append("status = ?")
            params.append(request.status.value)

        if not updates and request.tags is None and request.metadata is None:
            # No updates provided
            return self._fetch_memory(request.memory_id)

        # Always update the timestamp
        updates.append("updated_at = ?")
        params.append(now)
        params.append(request.memory_id)

        update_sql = f"UPDATE memories SET {', '.join(updates)} WHERE id = ?"
        self.conn.execute(update_sql, params)

        # Handle tags separately
        if request.tags is not None:
            self.conn.execute("DELETE FROM memory_tags WHERE memory_id = ?", (request.memory_id,))
            for tag in request.tags:
                self.conn.execute(
                    "INSERT INTO memory_tags (memory_id, tag) VALUES (?, ?)",
                    (request.memory_id, tag),
                )

        # Handle metadata
        if request.metadata is not None:
            metadata_json = json.dumps(request.metadata) if request.metadata else None
            self.conn.execute(
                "UPDATE memories SET metadata = ? WHERE id = ?",
                (metadata_json, request.memory_id),
            )

        self.conn.commit()
        return self._fetch_memory(request.memory_id)

    def supersede_memory(
        self,
        request: SupersedeMemoryRequest
    ) -> tuple[MemoryResponse, MemoryResponse]:
        """Mark one memory as superseded by another.

        Creates a lineage relationship and updates statuses to reflect
        the supersession (evolution, contradiction resolution, etc).

        Args:
            request: SupersedeMemoryRequest

        Returns:
            Tuple of (superseded_memory, superseding_memory)
        """
        now = now_utc()

        # Mark the old memory as superseded
        self.conn.execute(
            "UPDATE memories SET status = ? WHERE id = ?",
            (MemoryStatus.SUPERSEDED.value, request.superseded_memory_id),
        )

        # Record the lineage relationship
        self.conn.execute(
            """
            INSERT INTO memory_lineage (superseded_id, superseding_id, reason, recorded_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                request.superseded_memory_id,
                request.superseding_memory_id,
                request.reason,
                now,
            ),
        )

        self.conn.commit()

        return (
            self._fetch_memory(request.superseded_memory_id),
            self._fetch_memory(request.superseding_memory_id),
        )

    def dispute_memory(
        self,
        request: DisputeMemoryRequest
    ) -> MemoryResponse:
        """Flag a memory as disputed or contradictory.

        Records the dispute without changing the original memory's status;
        instead marks it as DISPUTED so it can be investigated and resolved.

        Args:
            request: DisputeMemoryRequest

        Returns:
            Updated MemoryResponse with DISPUTED status
        """
        now = now_utc()

        # Mark memory as disputed
        self.conn.execute(
            "UPDATE memories SET status = ? WHERE id = ?",
            (MemoryStatus.DISPUTED.value, request.memory_id),
        )

        # Record the dispute
        self.conn.execute(
            """
            INSERT INTO memory_disputes (memory_id, conflicting_id, reason, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                request.memory_id,
                request.conflicting_memory_id,
                request.reason,
                now,
            ),
        )

        self.conn.commit()
        return self._fetch_memory(request.memory_id)

    def search_memories(
        self,
        request: SearchMemoriesRequest
    ) -> tuple[int, list[MemoryResponse]]:
        """Search for memories by type, tags, text, or project.

        Implements the selective semantic retrieval pattern: returns a compact,
        relevant context instead of all memories.

        Args:
            request: SearchMemoriesRequest with search criteria

        Returns:
            Tuple of (total_count, results)
        """
        where_clauses = ["status IN ('ACTIVE', 'DISPUTED')"]  # exclude SUPERSEDED, ARCHIVED
        params = []

        # Filter by memory type
        if request.memory_types:
            types = [t.value for t in request.memory_types]
            placeholders = ",".join(["?" for _ in types])
            where_clauses.append(f"memory_type IN ({placeholders})")
            params.extend(types)

        # Filter by project
        if request.project_id is not None:
            where_clauses.append("project_id = ?")
            params.append(request.project_id)

        # Filter by status
        if request.status is not None:
            where_clauses.insert(0, f"status = '{request.status.value}'")

        # Filter by minimum confidence
        if request.min_confidence > 0:
            where_clauses.append("confidence >= ?")
            params.append(request.min_confidence)

        # Build base query
        where_sql = " AND ".join(where_clauses)

        # Full-text search on content
        if request.query:
            # Simple substring match for now; can upgrade to FTS5 later
            where_clauses.append("content LIKE ?")
            params.append(f"%{request.query}%")
            where_sql = " AND ".join(where_clauses)

        # Tag filtering (all tags must be present)
        if request.tags:
            tag_placeholders = ",".join(["?" for _ in request.tags])
            where_sql += f"""
                AND id IN (
                    SELECT memory_id FROM memory_tags
                    WHERE tag IN ({tag_placeholders})
                    GROUP BY memory_id
                    HAVING COUNT(DISTINCT tag) = ?
                )
            """
            params.extend(request.tags)
            params.append(len(request.tags))

        # Count total results
        count_query = f"SELECT COUNT(*) as cnt FROM memories WHERE {where_sql}"
        count_result = self.conn.execute(count_query, params).fetchone()
        total_count = count_result["cnt"] if count_result else 0

        # Fetch paginated results
        query = f"""
            SELECT id FROM memories
            WHERE {where_sql}
            ORDER BY created_at DESC
            LIMIT ? OFFSET 0
        """

        # Note: we rebuild params here to avoid param count issues
        search_params = []

        # Rebuild params for the paginated query
        search_params.extend(params)
        search_params.append(request.limit)

        results = self.conn.execute(query, search_params).fetchall()
        memories = [self._fetch_memory(row["id"]) for row in results]

        return total_count, memories

    def get_project_context(
        self,
        project_id: Optional[int] = None
    ) -> ProjectContextResponse:
        """Build a compact project context from relevant memories.

        Retrieves key decisions, facts, active context, and derived knowledge
        for a project, implementing the 'compact and relevant context' pattern
        that replaces infinite chat history.

        Args:
            project_id: Optional project ID to scope the context

        Returns:
            ProjectContextResponse with curated memories
        """
        now = now_utc()
        project_filter = "AND project_id = ?" if project_id else "AND project_id IS NULL"
        filter_param = (project_id,) if project_id else ()

        # Fetch top decisions (high confidence, sorted by recency)
        decisions = self._fetch_memories_by_type(
            MemoryType.DECISION,
            project_id,
            limit=5
        )

        # Fetch top facts
        facts = self._fetch_memories_by_type(
            MemoryType.FACT,
            project_id,
            limit=5
        )

        # Fetch active context (recent, temporary info)
        context = self._fetch_memories_by_type(
            MemoryType.CONTEXT,
            project_id,
            limit=5
        )

        # Fetch derived knowledge
        knowledge = self._fetch_memories_by_type(
            MemoryType.KNOWLEDGE,
            project_id,
            limit=5
        )

        # Get total count
        total_query = f"""
            SELECT COUNT(*) as cnt FROM memories
            WHERE status IN ('ACTIVE', 'DISPUTED')
            {project_filter}
        """
        total_result = self.conn.execute(total_query, filter_param).fetchone()
        total_count = total_result["cnt"] if total_result else 0

        summary = self._build_context_summary(decisions, facts, context, knowledge)

        return ProjectContextResponse(
            project_id=project_id,
            summary=summary,
            key_decisions=decisions,
            key_facts=facts,
            active_context=context,
            derived_knowledge=knowledge,
            total_memories=total_count,
            last_updated=now,
        )

    def _fetch_memory(self, memory_id: int) -> MemoryResponse:
        """Fetch a single memory by ID with all relationships."""
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()

        if not row:
            raise ValueError(f"Memory {memory_id} not found")

        # Fetch tags
        tags = [
            r["tag"] for r in self.conn.execute(
                "SELECT tag FROM memory_tags WHERE memory_id = ? ORDER BY tag",
                (memory_id,),
            ).fetchall()
        ]

        # Fetch supersession info
        superseded_by = self.conn.execute(
            "SELECT superseding_id FROM memory_lineage WHERE superseded_id = ? LIMIT 1",
            (memory_id,),
        ).fetchone()

        supersedes = self.conn.execute(
            "SELECT superseded_id FROM memory_lineage WHERE superseding_id = ? LIMIT 1",
            (memory_id,),
        ).fetchone()

        metadata = json.loads(row["metadata"]) if row["metadata"] else {}

        return MemoryResponse(
            memory_id=row["id"],
            memory_type=MemoryType(row["memory_type"]),
            content=row["content"],
            status=MemoryStatus(row["status"]),
            confidence=row["confidence"],
            tags=tags,
            source_message_id=row["source_message_id"],
            project_id=row["project_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            superseded_by=superseded_by["superseding_id"] if superseded_by else None,
            supersedes=supersedes["superseded_id"] if supersedes else None,
            metadata=metadata,
        )

    def _fetch_memories_by_type(
        self,
        memory_type: MemoryType,
        project_id: Optional[int] = None,
        limit: int = 5
    ) -> list[MemoryResponse]:
        """Fetch top memories of a specific type for a project."""
        query = """
            SELECT id FROM memories
            WHERE memory_type = ? AND status IN ('ACTIVE', 'DISPUTED')
        """
        params = [memory_type.value]

        if project_id is not None:
            query += " AND project_id = ?"
            params.append(project_id)
        else:
            query += " AND project_id IS NULL"

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        return [self._fetch_memory(row["id"]) for row in rows]

    def _build_context_summary(
        self,
        decisions: list[MemoryResponse],
        facts: list[MemoryResponse],
        context: list[MemoryResponse],
        knowledge: list[MemoryResponse],
    ) -> str:
        """Build a human-readable summary of the project context."""
        lines = []

        if decisions:
            lines.append("**Key Decisions:**")
            for mem in decisions[:3]:
                lines.append(f"- {mem.content[:100]}...")

        if facts:
            lines.append("\n**Key Facts:**")
            for mem in facts[:3]:
                lines.append(f"- {mem.content[:100]}...")

        if context:
            lines.append("\n**Active Context:**")
            for mem in context[:3]:
                lines.append(f"- {mem.content[:100]}...")

        if knowledge:
            lines.append("\n**Derived Knowledge:**")
            for mem in knowledge[:3]:
                lines.append(f"- {mem.content[:100]}...")

        return "\n".join(lines) if lines else "No memories found for this project."
