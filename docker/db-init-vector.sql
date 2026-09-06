-- Auto-run by the pgvector/pgvector image on first start when mounted at
-- /docker-entrypoint-initdb.d/. The Postgres entrypoint already creates
-- the database and user from POSTGRES_DB / POSTGRES_USER / POSTGRES_PASSWORD;
-- we just need to enable pgvector inside that database.
CREATE EXTENSION IF NOT EXISTS vector;
