"""Optional graph snapshots and a shared budget for relationship reads."""
import pydantic_core
from mcp.types import CallToolResult, TextContent
from sqlalchemy import select
from sqlalchemy.orm import aliased

from src.mcp_server.read_result import ReadNoteResult, RelatedNote, RelationshipContext
from src.models.db import NoteLink, NoteMetadata


async def load_relationships(session, path, normalized_hash, uid, limit, owner_predicate):
    """Read the same indexed edges as the graph tools, scoped at both ends.

    Matching the source hash/grammar says only that the index saw this note's
    text. It never certifies other files, current target bytes, or a whole vault.
    """
    from src.services.indexer import CURRENT_EXTRACTION_VERSION

    source = (await session.execute(select(
        NoteMetadata.id, NoteMetadata.content_hash, NoteMetadata.extraction_version,
        NoteMetadata.links_truncated,
    ).where(
        NoteMetadata.file_path == path, owner_predicate(NoteMetadata, uid),
    ))).one_or_none()
    if source is None:
        return RelationshipContext(note_status="unindexed")
    other = aliased(NoteMetadata)
    outgoing = (await session.execute(
        select(other.file_path, other.title).select_from(NoteLink)
        .join(other, (NoteLink.target_note_id == other.id) & owner_predicate(other, uid))
        .where(NoteLink.source_note_id == source.id)
        .distinct().order_by(other.file_path, other.title).limit(limit + 1)
    )).all()
    backlinks = (await session.execute(
        select(other.file_path, other.title).select_from(NoteLink)
        .join(other, (NoteLink.source_note_id == other.id) & owner_predicate(other, uid))
        .where(NoteLink.target_note_id == source.id)
        .distinct().order_by(other.file_path, other.title).limit(limit + 1)
    )).all()
    return RelationshipContext(
        note_status="matches_read" if (
            source.content_hash == normalized_hash
            and source.extraction_version == CURRENT_EXTRACTION_VERSION
        ) else "stale",
        outgoing=[RelatedNote(path=row.file_path, title=row.title) for row in outgoing[:limit]],
        backlinks=[RelatedNote(path=row.file_path, title=row.title) for row in backlinks[:limit]],
        outgoing_has_more=True if len(outgoing) > limit else None,
        backlinks_has_more=True if len(backlinks) > limit else None,
        extraction_truncated=True if source.links_truncated else None,
    )


def response_size(result: ReadNoteResult) -> int:
    """Canonical CallToolResult size, including both SDK renderings/escaping.

    FastMCP renders the text block with indent=2 and separately validates the
    structured object. Counting just content, or one rendering, undercounts.
    JSON-RPC framing (notably the caller's request id) is outside this budget.
    """
    text = pydantic_core.to_json(result, fallback=str, indent=2).decode()
    return len(CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=result.model_dump(mode="json", by_alias=True),
    ).model_dump_json(by_alias=True, exclude_none=True))


def bound_relationship_read(result: ReadNoteResult, cap: int) -> ReadNoteResult:
    """Fit the complete opt-in result without inventing note/relationship text.

    Reserve roughly half for content; drop optional metadata whole and trim
    relationship lists before shortening the exact contiguous content window.
    Default reads never enter this function and retain their existing budgets.
    """
    if response_size(result) <= cap:
        return result
    if result.error is not None:
        raise ValueError("The relationship read cannot fit the response budget; read without relationships.")
    content = result.content or ""
    original_next = result.next_offset
    offset = result.offset or 0
    result.content = ""
    # Continuation is already explicit in next_offset. A prose notice would
    # compete with usable content twice in the SDK's two renderings.
    result.notice = None
    omissions = list(result.response_omissions or [])
    result.response_omissions = omissions
    for field in ("frontmatter", "frontmatter_yaml", "tags", "outline", "metadata_omissions", "metadata_coercions", "heading", "title"):
        if response_size(result) <= cap // 2:
            break
        if getattr(result, field) is not None:
            setattr(result, field, None)
            omissions.append(field)
    graph = result.relationships
    while graph and (graph.outgoing or graph.backlinks) and response_size(result) > cap // 2:
        if len(graph.outgoing) >= len(graph.backlinks):
            graph.outgoing.pop()
            graph.outgoing_has_more = True
        else:
            graph.backlinks.pop()
            graph.backlinks_has_more = True
    if not omissions:
        result.response_omissions = None
    elif response_size(result) > cap - 100:
        # A compact category still distinguishes omitted metadata from an
        # absent value on the note, without spending the minimum cap on names.
        result.response_omissions = ["metadata"]

    def window(length):
        result.content = content[:length]
        result.next_offset = offset + length if length < len(content) else original_next
        result.truncated = offset > 0 or result.next_offset is not None
        return response_size(result)

    if window(len(content)) <= cap:
        return result
    low, high = 0, len(content)
    while low < high:
        mid = (low + high + 1) // 2
        if window(mid) <= cap:
            low = mid
        else:
            high = mid - 1
    if window(low) > cap or (content and low == 0):
        raise ValueError("The relationship read cannot fit the response budget; read without relationships.")
    return result
