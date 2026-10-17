"""Relationship reads through the real MCP protocol and indexed PostgreSQL graph."""
import hashlib
import json

import pytest
import pytest_asyncio
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
from src.config import settings
from src.auth.session import current_vault_root
from src.mcp_server.server import mcp
from src.mcp_server import tools
from src.models.db import NoteLink, NoteMetadata, User
from src.services import indexer

pytestmark = [_harness.requires_pgvector, pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("read_relationships", 8)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessions(migrated_url):
    engine = create_async_engine(migrated_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def corpus(sessions, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools, "async_session", sessions)
    monkeypatch.setattr(indexer, "async_session", sessions)
    async def no_usage(*args, **kwargs):
        pass
    monkeypatch.setattr(tools, "_log_usage", no_usage)
    async with sessions() as session:
        for model in (NoteLink, NoteMetadata, User):
            await session.execute(model.__table__.delete())
        await session.commit()
    for name, body in {
        "HowTo.md": "---\ntype: HowTo\n---\n# A procedure\nFollow [[Evergreen]].\n",
        "Evergreen.md": "---\ntype: Evergreen\n---\n# A principle\nSee [[Decision]].\n",
        "Decision.md": "---\ntype: Decision\n---\n# A decision\nBased on [[Evergreen]].\n",
        "Empty.md": "",
    }.items():
        (tmp_path / name).write_text(body)
    await indexer.index_vault(full_hash=True)
    return tmp_path


async def read(client, path, **kwargs):
    result = await client.call_tool("read_note", {"path": path, "include_relationships": True, **kwargs})
    assert not result.isError, result
    assert json.loads(result.content[0].text) == result.structuredContent
    assert len(result.model_dump_json(by_alias=True, exclude_none=True)) <= settings.max_read_response_chars
    return result.structuredContent


async def test_read_follow_and_backlink(corpus):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.initialize()
        default = await client.call_tool("read_note", {"path": "HowTo.md"})
        assert "relationships" not in default.structuredContent
        howto = await read(client, "HowTo.md")
        graph = howto["relationships"]
        assert graph["note_status"] == "matches_read"
        assert graph["basis"] == "indexed_snapshot"
        assert graph["completeness"] == "indexed_only"
        assert graph["outgoing"] == [{"path": "Evergreen.md", "title": "Evergreen"}]
        evergreen = await read(client, graph["outgoing"][0]["path"])
        assert {entry["path"] for entry in evergreen["relationships"]["backlinks"]} == {"HowTo.md", "Decision.md"}
        assert evergreen["relationships"]["outgoing"][0]["path"] == "Decision.md"


async def test_empty_unindexed_missing_and_stale(corpus):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.initialize()
        empty = await read(client, "Empty.md")
        assert empty["content"] == ""
        assert empty["relationships"]["outgoing"] == []
        assert empty["relationships"]["backlinks"] == []
        (corpus / "Unindexed.md").write_text("[[HowTo]]")
        unindexed = await read(client, "Unindexed.md")
        assert unindexed["relationships"]["note_status"] == "unindexed"
        (corpus / "HowTo.md").write_text("Now points at [[Decision]].")
        stale = await read(client, "HowTo.md")
        assert stale["relationships"]["note_status"] == "stale"
        assert stale["relationships"]["outgoing"][0]["path"] == "Evergreen.md"
        missing = await read(client, "Missing.md")
        assert "not found" in missing["error"].lower()
        assert "relationships" not in missing


async def test_owner_scoping_and_link_limits(corpus, sessions):
    async with sessions() as session:
        owned = {note.file_path: note for note in (await session.scalars(select(NoteMetadata))).all()}
        other = User(username="qa_relationship_other", password_hash="unused", vault_path="/qa/other")
        session.add(other)
        await session.flush()
        secret = NoteMetadata(user_id=other.id, file_path="private-secret.md", title="Private title", content_hash="unused")
        session.add(secret)
        await session.flush()
        session.add_all([
            NoteLink(source_note_id=owned["HowTo.md"].id, target_note_id=secret.id, target_path="private-secret.md", link_text="[[private-secret]]", position=0),
            NoteLink(source_note_id=secret.id, target_note_id=owned["Evergreen.md"].id, target_path="Evergreen.md", link_text="[[Evergreen]]", position=0),
        ])
        owned["Evergreen.md"].links_truncated = True
        await session.commit()
    async with create_connected_server_and_client_session(mcp) as client:
        await client.initialize()
        howto = await read(client, "HowTo.md")
        assert "private" not in json.dumps(howto)
        evergreen = await read(client, "Evergreen.md", relationship_limit=1)
        assert "Private title" not in json.dumps(evergreen)
        assert evergreen["relationships"]["backlinks_has_more"] is True
        assert evergreen["relationships"]["extraction_truncated"] is True


async def test_same_read_hash_handles_crlf(corpus):
    raw = b"---\r\ntype: HowTo\r\n---\r\n[[Evergreen]]\r\n"
    (corpus / "HowTo.md").write_bytes(raw)
    await indexer.index_vault(full_hash=True)
    async with create_connected_server_and_client_session(mcp) as client:
        await client.initialize()
        result = await read(client, "HowTo.md")
        assert result["relationships"]["note_status"] == "matches_read"
        assert result["content_hash"] == "sha256:" + hashlib.sha256(raw).hexdigest()


async def test_named_owner_uses_own_source_with_same_path(corpus, sessions):
    async with sessions() as session:
        owner = User(username="qa_relationship_owner", password_hash="unused", vault_path=str(corpus))
        session.add(owner)
        await session.flush()
        uid = owner.id
        for note in (await session.scalars(select(NoteMetadata))).all():
            note.user_id = uid
        session.add(NoteMetadata(file_path="HowTo.md", title="Foreign duplicate", content_hash="foreign"))
        await session.commit()
    token = tools.current_user_id.set(uid)
    root_token = current_vault_root.set((uid, corpus))
    try:
        async with create_connected_server_and_client_session(mcp) as client:
            await client.initialize()
            result = await read(client, "HowTo.md")
            assert result["relationships"]["note_status"] == "matches_read"
            assert result["relationships"]["outgoing"][0]["path"] == "Evergreen.md"
            assert "Foreign duplicate" not in json.dumps(result)
    finally:
        current_vault_root.reset(root_token)
        tools.current_user_id.reset(token)


async def test_old_extraction_and_unresolved_targets(corpus, sessions):
    async with sessions() as session:
        note = await session.scalar(select(NoteMetadata).where(NoteMetadata.file_path == "HowTo.md"))
        note.extraction_version = indexer.CURRENT_EXTRACTION_VERSION - 1
        session.add(NoteLink(source_note_id=note.id, target_path="NotIndexed", target_note_id=None,
                             link_text="[[NotIndexed]]", position=50))
        await session.commit()
    async with create_connected_server_and_client_session(mcp) as client:
        await client.initialize()
        result = await read(client, "HowTo.md")
        assert result["relationships"]["note_status"] == "stale"
        assert result["relationships"]["outgoing"] == [{"path": "Evergreen.md", "title": "Evergreen"}]


async def test_index_commit_between_queries_keeps_one_snapshot(corpus, sessions, monkeypatch):
    class PausingSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if "notes_metadata.links_truncated" in str(statement):
                async with sessions() as writer:
                    notes = {n.file_path: n for n in (await writer.scalars(select(NoteMetadata))).all()}
                    source = notes["HowTo.md"]
                    source.content_hash = "new indexed version"
                    await writer.execute(NoteLink.__table__.delete().where(NoteLink.source_note_id == source.id))
                    writer.add(NoteLink(source_note_id=source.id, target_note_id=notes["Decision.md"].id,
                                        target_path="Decision.md", link_text="[[Decision]]", position=0))
                    await writer.commit()
            return result
    monkeypatch.setattr(tools, "async_session", async_sessionmaker(sessions.kw["bind"], class_=PausingSession))
    async with create_connected_server_and_client_session(mcp) as client:
        await client.initialize()
        result = await read(client, "HowTo.md")
        assert result["relationships"]["note_status"] == "matches_read"
        assert result["relationships"]["outgoing"] == [{"path": "Evergreen.md", "title": "Evergreen"}]


@pytest.mark.parametrize("cap", [1000, 2000, 4000])
async def test_complete_budget_and_continuations(corpus, monkeypatch, cap):
    monkeypatch.setattr(settings, "max_read_response_chars", cap)
    body = '\x01"\nΩ' * 350
    (corpus / "HowTo.md").write_text("---\ntags: [" + ",".join("tag" + str(i) for i in range(300)) + "]\n---\n" + body)
    async with create_connected_server_and_client_session(mcp) as client:
        await client.initialize()
        first = await read(client, "HowTo.md")
        assert first["relationships"]["basis"] == "indexed_snapshot"
        assert first["relationships"]["note_status"] == "stale"
        assert first["relationships"]["completeness"] == "indexed_only"
        assert first["truncated"] is True
        assert first["next_offset"] == len(first["content"]) > 0
        assert body.startswith(first["content"])
        continuation = await read(client, "HowTo.md", offset=first["next_offset"])
        assert continuation["offset"] == len(first["content"])
        assert body.startswith(first["content"] + continuation["content"])
        if not first["relationships"]["outgoing"]:
            assert first["relationships"]["outgoing_has_more"] is True


async def test_relationship_limit_validation_and_default_compatibility(corpus):
    async with create_connected_server_and_client_session(mcp) as client:
        await client.initialize()
        absent = await client.call_tool("read_note", {"path": "HowTo.md"})
        false = await client.call_tool("read_note", {"path": "HowTo.md", "include_relationships": False})
        assert absent.model_dump_json() == false.model_dump_json()
        for limit in (0, 51):
            invalid = await read(client, "HowTo.md", relationship_limit=limit)
            assert "between 1 and 50" in invalid["error"]
            assert "relationships" not in invalid


async def test_unframeable_path_is_a_bounded_actionable_error(corpus, monkeypatch):
    monkeypatch.setattr(settings, "max_read_response_chars", 1000)
    path = "L" * 200 + ".md"
    (corpus / path).write_text("The default read still works.")
    async with create_connected_server_and_client_session(mcp) as client:
        await client.initialize()
        result = await client.call_tool("read_note", {"path": path, "include_relationships": True})
        assert result.isError
        assert "read without relationships" in result.content[0].text
        assert len(result.model_dump_json(by_alias=True, exclude_none=True)) <= 1000
        default = await client.call_tool("read_note", {"path": path})
        assert not default.isError
        assert default.structuredContent["content"] == "The default read still works."
