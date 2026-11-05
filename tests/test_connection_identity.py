"""Named connections are Git authors; server identity remains the committer."""
import asyncio

import pytest

from src.auth.session import current_connection_author
from src.services.connection_identity import ConnectionAuthor, connection_author, normalize_connection_alias
from src.services import git_vault
from src.mcp_server import tools
from tests.test_git_vault import git
from tests.test_git_vault_commit_on_write import vault, fresh_commit_lock


@pytest.mark.parametrize('alias', ['', '   ', 'x'*129, 'x\ny', 'x\x00y', 'x\u200dy', '<name>', 'x\u0085y'])
def test_invalid_names_are_refused(alias):
    with pytest.raises(ValueError):
        normalize_connection_alias(alias)


def test_alias_normalization_and_stable_private_address():
    first = connection_author('  E\u0301lio  ', 'oauth', 'grant-a')
    assert first.name == 'Élio'
    assert first.email == connection_author('Renamed', 'oauth', 'grant-a').email
    assert first.email != connection_author('Élio', 'oauth', 'grant-b').email
    assert first.email != connection_author('Élio', 'api_key', 'grant-a').email
    assert first.email.endswith('@connections.obsidian-mcp.invalid')
    assert 'grant-a' not in first.email
    with pytest.raises(ValueError):
        ConnectionAuthor('Name', 'human@example.com')


@pytest.mark.parametrize('git_enabled', [False, True])
async def test_unnamed_connection_refuses_write_without_touching_bytes(vault, monkeypatch, git_enabled):
    monkeypatch.setattr(git_vault.settings, 'git_vault_enabled', git_enabled)
    (vault / 'Existing.md').write_text('unchanged\n')
    tok = current_connection_author.set(None)
    try:
        refused = await tools.edit_note_impl('Existing.md', content='replacement\n')
        read = await tools.read_note_impl('Existing.md')
    finally:
        current_connection_author.reset(tok)
    assert refused.refusal.code == 'connection_name_required'
    assert (vault / 'Existing.md').read_text() == 'unchanged\n'
    assert read.content == 'unchanged\n'


async def test_concurrent_connections_keep_distinct_authors_and_server_committer(vault):
    authors = [connection_author('Elio', 'oauth', 'grant-elio'),
               connection_author('Research client', 'api_key', 42)]

    async def write(author, path):
        token = current_connection_author.set(author)
        try:
            await asyncio.sleep(0)
            result = await tools.create_note_impl(path, f'{path}\n')
            assert str(result).startswith('Created note:')
        finally:
            current_connection_author.reset(token)

    await asyncio.gather(write(authors[0], 'Elio.md'), write(authors[1], 'Research.md'))
    for author, path in zip(authors, ('Elio.md', 'Research.md')):
        identity = git(vault, 'log', '-1', '--format=%an%n%ae%n%cn%n%ce', '--', path).stdout.splitlines()
        assert identity == [author.name, author.email, 'obsidian-mcp agent', 'agent@test.invalid']
    assert git(vault, 'config', '--local', '--get', 'user.name', check=False).returncode == 1


async def test_every_write_required_tool_refuses_an_unnamed_connection(vault):
    from src.mcp_server.server import mcp
    from src.mcp_server.tool_permissions import WRITE_REQUIRED_TOOLS
    refused = set()
    token = current_connection_author.set(None)
    try:
        for tool in mcp._tool_manager.list_tools():
            if tool.name not in WRITE_REQUIRED_TOOLS:
                continue
            args = {name: 'https://example.invalid/file' if name == 'url' else 'sample.md'
                    for name in tool.parameters.get('required', [])}
            result = await mcp.call_tool(tool.name, args)
            blocks = result[0] if isinstance(result, tuple) else result
            text = ''.join(getattr(block, 'text', '') for block in blocks)
            assert 'connection_name_required' in text, (tool.name, text)
            refused.add(tool.name)
    finally:
        current_connection_author.reset(token)
    assert refused == WRITE_REQUIRED_TOOLS
    assert sorted(p.name for p in vault.iterdir()) == ['.git', 'README.md']
