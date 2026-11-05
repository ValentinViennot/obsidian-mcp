"""Real migration cutover and OAuth alias inheritance; synthetic throwaway DBs."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import _harness
from src.models.db import OAuthClient, OAuthCode, OAuthToken
from src.oauth import routes

pytestmark = _harness.requires_pgvector


@pytest.fixture
def db_url():
    yield from _harness.throwaway_database('named_connections', 64)


async def execute(url, sql):
    db = await asyncpg.connect(_harness.asyncpg_dsn(url))
    try:
        if sql.count(";") > 1:
            await db.execute(sql)
            return []
        return await db.fetch(sql)
    finally:
        await db.close()


def test_cutover_revokes_oauth_preserves_keys_and_replay_preserves_named(db_url):
    _harness.run_alembic(db_url, 'downgrade', '043')
    asyncio.run(execute(db_url, """
        INSERT INTO oauth_clients(client_id,client_name,redirect_uris,scope,token_endpoint_auth_method)
        VALUES ('test-client','Example app','[]','readwrite','none');
        INSERT INTO oauth_codes(code_hash,client_id,redirect_uri,scope,code_challenge,expires_at,used)
        VALUES ('legacy-code','test-client','https://example.test/cb','read','challenge',now()+interval '10 minutes',false);
        INSERT INTO oauth_tokens(token_hash,token_type,client_id,scope,grant_id,expires_at,revoked)
        VALUES ('legacy-token','access','test-client','read','old-grant',now()+interval '1 hour',false);
        INSERT INTO api_keys(name,key_hash,key_prefix,permission,is_active)
        VALUES ('Existing API key','test-key-hash','omcp_test','readwrite',true);
    """))
    _harness.run_alembic(db_url, 'upgrade', 'head')
    rows = asyncio.run(execute(db_url, """
        SELECT (SELECT revoked FROM oauth_tokens WHERE token_hash='legacy-token') revoked,
               (SELECT used FROM oauth_codes WHERE code_hash='legacy-code') used,
               (SELECT is_active FROM api_keys WHERE key_hash='test-key-hash') key_active
    """))
    assert dict(rows[0]) == {'revoked':True, 'used':True, 'key_active':True}
    asyncio.run(execute(db_url, """
        INSERT INTO oauth_tokens(token_hash,token_type,client_id,scope,grant_id,expires_at,revoked,connection_alias)
        VALUES ('new-token','access','test-client','read','new-grant',now()+interval '1 hour',false,'Research');
    """))
    _harness.run_alembic(db_url, 'stamp', '043')
    _harness.run_alembic(db_url, 'upgrade', 'head')
    assert not asyncio.run(execute(db_url, "SELECT revoked FROM oauth_tokens WHERE token_hash='new-token'"))[0]['revoked']
    _harness.run_alembic(db_url, 'check')


def test_exchange_and_refresh_preserve_alias_and_stable_grant(db_url, monkeypatch):
    async def scenario():
        engine = create_async_engine(db_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(routes, 'async_session', sessions)
        monkeypatch.setattr(routes.settings, 'multi_user_mode', False)
        verifier = 'v' * 64
        async with sessions() as session:
            session.add(OAuthClient(client_id='test-client',client_name='Example app',
                redirect_uris=['https://example.test/cb'],scope='read readwrite offline_access',
                token_endpoint_auth_method='none'))
            await session.flush()
            session.add(OAuthCode(code_hash=routes._hash('named-code'),client_id='test-client',
                redirect_uri='https://example.test/cb',scope='readwrite offline_access',
                code_challenge=routes._base64url_sha256(verifier),
                expires_at=datetime.now(timezone.utc)+timedelta(minutes=5),
                connection_alias='Research desk'))
            await session.commit()
        try:
            response = await routes._handle_auth_code({'code':'named-code','code_verifier':verifier,
                'redirect_uri':'https://example.test/cb'})
            assert response.status_code == 200, response.body
            refresh_value = json.loads(response.body)['refresh_token']
            response = await routes._handle_refresh({'refresh_token':refresh_value})
            assert response.status_code == 200, response.body
            async with sessions() as session:
                rows = (await session.execute(select(OAuthToken))).scalars().all()
                assert len(rows) == 4
                assert {row.connection_alias for row in rows} == {'Research desk'}
                assert len({row.grant_id for row in rows}) == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_unnamed_codes_cannot_be_exchanged_after_cutover(db_url, monkeypatch):
    async def scenario():
        engine = create_async_engine(db_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(routes, 'async_session', sessions)
        async with sessions() as session:
            session.add(OAuthClient(client_id='legacy-writer',client_name='Legacy app',
                redirect_uris=['https://example.test/cb'],scope='read',token_endpoint_auth_method='none'))
            await session.flush()
            session.add(OAuthCode(code_hash=routes._hash('unnamed-code'),client_id='legacy-writer',
                redirect_uri='https://example.test/cb',scope='read',code_challenge=routes._base64url_sha256('v'*64),
                expires_at=datetime.now(timezone.utc)+timedelta(minutes=5)))
            await session.commit()
        try:
            response = await routes._handle_auth_code({'code':'unnamed-code','code_verifier':'v'*64,
                'redirect_uri':'https://example.test/cb'})
            assert response.status_code == 400
            assert json.loads(response.body)['error'] == 'invalid_grant'
            async with sessions() as session:
                assert not (await session.execute(select(OAuthToken))).scalars().all()
        finally:
            await engine.dispose()
    asyncio.run(scenario())
