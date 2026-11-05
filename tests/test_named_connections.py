"""Named consent, validation, issuance inheritance and per-grant presentation."""
import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.api.routes import CreateKeyRequest
from src.control_panel.routes import _key_name_error
from src.models.db import OAuthCode
from src.services.connection_identity import connection_author
from test_issue_191_oauth_events import _authorize_post, _User, FakeClient, REGISTERED_URI
from test_oauth_panel_scope_display import render_oauth_page
from _oauth_grant_fakes import FakeToken


def test_consent_records_normalized_alias_independently_of_application(monkeypatch):
    response, session = _authorize_post(
        monkeypatch, client=FakeClient(client_name="Application", redirect_uris=[REGISTERED_URI], user_id=5),
        session_user=_User(5), connection_alias="  E\u0301quipe rédaction  ",
    )
    assert response.status_code == 302
    code = next(row for row in session.added if isinstance(row, OAuthCode))
    assert code.connection_alias == "Équipe rédaction"


@pytest.mark.parametrize("alias", ["", "   ", "x" * 129, "a\nb", "a\u200bb", "a<b"])
def test_bad_alias_mints_nothing(monkeypatch, alias):
    response, session = _authorize_post(
        monkeypatch, client=FakeClient(redirect_uris=[REGISTERED_URI], user_id=5),
        session_user=_User(5), connection_alias=alias,
    )
    assert response.status_code == 400
    assert not session.added
    assert not session.committed


def test_denial_does_not_require_alias(monkeypatch):
    response, session = _authorize_post(
        monkeypatch, client=FakeClient(redirect_uris=[REGISTERED_URI], user_id=5),
        session_user=_User(5), action="deny", connection_alias="",
    )
    assert response.status_code == 302
    assert "error=access_denied" in response.headers['location']
    assert not session.added


def test_api_key_names_use_the_connection_policy():
    assert CreateKeyRequest(name="  E\u0301quipe  ").name == "Équipe"
    for value in (" ", "a\nb", "x" * 129, "<bot>"):
        with pytest.raises(ValidationError):
            CreateKeyRequest(name=value)
        assert _key_name_error(value)


def test_panel_distinguishes_two_connections_from_one_application():
    client = FakeClient(client_name="Example app")
    tokens = [FakeToken(grant_id="grant-a", connection_alias="Research"),
              FakeToken(grant_id="grant-b", connection_alias="Writing")]
    html = render_oauth_page(clients=[client], tokens=tokens)
    assert 'Research' in html and 'Writing' in html
    assert html.count('Application: Example app') == 2


def test_every_token_mint_explicitly_inherits_alias():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'src/oauth/routes.py').read_text())
    constructors = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == 'OAuthToken']
    assert len(constructors) == 4
    assert all('connection_alias' in {k.arg for k in n.keywords} for n in constructors)


def test_middleware_keeps_app_label_and_scopes_connection_author():
    from datetime import datetime, timedelta, timezone
    from src.auth.session import current_connection_author
    from src.models.db import OAuthToken
    from test_issue_77_usage_attribution import _drive, _MiddlewareSession
    outer = current_connection_author.get()
    token = OAuthToken(id=11, token_hash="synthetic", token_type="access",
        client_id="example-app", connection_alias="Research", scope="readwrite",
        grant_id="grant-research", grant_issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc)+timedelta(hours=1), revoked=False)
    seen = _drive("synthetic-oauth", _MiddlewareSession(
        oauth_token=token, client_row=(None, "Example application")))
    assert seen['actor'] == ('oauth', 'Example application', 'example-app')
    assert seen['connection'] == connection_author('Research', 'oauth', 'grant-research')
    assert seen['connection_after'] == outer


def test_invalid_legacy_key_name_allows_read_without_inventing_author():
    from src.auth.session import current_connection_author
    from src.models.db import APIKey
    from test_issue_77_usage_attribution import _drive, _MiddlewareSession
    outer = current_connection_author.get()
    key = APIKey(id=7,name="legacy\nname",key_hash="synthetic",key_prefix="omcp_test",
                 permission="readwrite",is_active=True)
    seen = _drive('omcp_synthetic', _MiddlewareSession(api_key=key))
    assert seen['status'] == 200
    assert seen['connection'] is None
    assert seen['connection_after'] == outer

@pytest.mark.asyncio
async def test_rename_preserves_key_and_stable_author_identity():
    from types import SimpleNamespace
    from src.api.routes import rename_key, RenameKeyRequest
    from src.control_panel.routes import rename_key_form
    key = SimpleNamespace(id=42, user_id=7, name='Old name', key_hash='unchanged-hash')
    class Session:
        committed = 0
        async def execute(self, statement):
            return SimpleNamespace(scalar_one_or_none=lambda: key)
        async def commit(self):
            self.committed += 1
    session = Session()
    user = SimpleNamespace(id=7, is_admin=False)
    before = connection_author(key.name, 'api_key', key.id)
    result = await rename_key(42, RenameKeyRequest(name='  New name  '), session=session, user=user)
    after = connection_author(key.name, 'api_key', key.id)
    assert result == {'id': 42, 'name': 'New name'}
    assert after.name == 'New name' and after.email == before.email
    assert key.key_hash == 'unchanged-hash' and key.id == 42
    assert session.committed == 1
    request = SimpleNamespace(session={})
    response = await rename_key_form(request, 42, name='invalid<name', session=session, user=user)
    assert response.status_code == 303 and request.session['flash_key_error']
    assert key.name == 'New name' and session.committed == 1
    user.id = 8
    from fastapi import HTTPException
    for callback in (
        lambda: rename_key(42, RenameKeyRequest(name='Other'), session=session, user=user),
        lambda: rename_key_form(request, 42, name='Other', session=session, user=user),
    ):
        with pytest.raises(HTTPException) as exc:
            await callback()
        assert exc.value.status_code == 403
    assert key.name == 'New name' and session.committed == 1
    user.id = 7
    response = await rename_key_form(request, 42, name='  Corrected name  ', session=session, user=user)
    assert response.status_code == 303 and key.name == 'Corrected name'
    assert key.key_hash == 'unchanged-hash' and session.committed == 2
    assert connection_author(key.name, 'api_key', key.id).email == before.email


def test_rename_rejects_invalid_names():
    from src.api.routes import RenameKeyRequest
    for name in ('', ' ', 'a<b', 'a\nb', 'x' * 129):
        with pytest.raises(ValidationError):
            RenameKeyRequest(name=name)
