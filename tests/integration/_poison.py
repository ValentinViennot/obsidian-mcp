"""A genuine server-side failure on one chosen note (#308, design D6).

The quarantine tests need a real PostgreSQL statement error at a chosen write
site for a chosen path — not a Python-raised exception, so the savepoint and
transaction recovery are what is exercised. Since D1/D2 made a NUL note index
normally, no ordinary note content reaches these sites any more, so the
failure is planted with triggers driven by a rules table:

- `upsert`   — BEFORE INSERT on `notes_metadata` (fires for the batch upsert,
  including the ON CONFLICT path);
- `move`     — BEFORE UPDATE whose `file_path` changes to the path;
- `tsvector` — BEFORE UPDATE that changes `content_tsvector` of the path (the
  incremental pass's keyword vector and the full rebuild alike);
- `link`     — BEFORE INSERT on `note_links` whose source note has the path;
- `batch`    — a statement-level failure (22000) of any multi-row INSERT on
  `notes_metadata`, which no single row reproduces (the path is ignored).

Each rule raises the SQLSTATE it names (default 54000, program_limit_exceeded).

A rule with `shots` fails only that many times, then stops (#308 C8, the
confirm-retry): the count is drawn from a sequence, which is not
transactional, so the savepoint rollback of the failing statement does not
give the shot back. One limited rule at a time — `poison()` restarts the
sequence.
"""
from sqlalchemy import text

_INSTALL = [
    "CREATE TABLE IF NOT EXISTS poison_rules ("
    "  site text NOT NULL, path text NOT NULL, "
    "  errcode text NOT NULL DEFAULT '54000', shots integer)",
    "CREATE SEQUENCE IF NOT EXISTS poison_shot_seq",
    """
    CREATE OR REPLACE FUNCTION poison_fire(code text, shots integer)
    RETURNS text LANGUAGE plpgsql AS $$
    BEGIN
      IF code IS NOT NULL AND shots IS NOT NULL
         AND nextval('poison_shot_seq') > shots THEN
        RETURN NULL;
      END IF;
      RETURN code;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION poison_notes() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE code text; n integer;
    BEGIN
      IF TG_OP = 'INSERT' THEN
        SELECT errcode, shots INTO code, n FROM poison_rules
         WHERE site = 'upsert' AND path = NEW.file_path;
      ELSIF NEW.file_path IS DISTINCT FROM OLD.file_path THEN
        SELECT errcode, shots INTO code, n FROM poison_rules
         WHERE site = 'move' AND path = NEW.file_path;
      ELSIF NEW.content_tsvector IS DISTINCT FROM OLD.content_tsvector THEN
        SELECT errcode, shots INTO code, n FROM poison_rules
         WHERE site = 'tsvector' AND path = NEW.file_path;
      END IF;
      code := poison_fire(code, n);
      IF code IS NOT NULL THEN
        RAISE EXCEPTION 'synthetic % failure for a test', TG_OP
          USING ERRCODE = code;
      END IF;
      RETURN NEW;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION poison_links() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE code text; k integer;
    BEGIN
      SELECT r.errcode, r.shots INTO code, k
        FROM poison_rules r JOIN notes_metadata n ON n.file_path = r.path
       WHERE r.site = 'link' AND n.id = NEW.source_note_id
       LIMIT 1;
      code := poison_fire(code, k);
      IF code IS NOT NULL THEN
        RAISE EXCEPTION 'synthetic link failure for a test'
          USING ERRCODE = code;
      END IF;
      RETURN NEW;
    END $$
    """,
    "DROP TRIGGER IF EXISTS poison_notes ON notes_metadata",
    "CREATE TRIGGER poison_notes BEFORE INSERT OR UPDATE ON notes_metadata "
    "FOR EACH ROW EXECUTE FUNCTION poison_notes()",
    "DROP TRIGGER IF EXISTS poison_links ON note_links",
    "CREATE TRIGGER poison_links BEFORE INSERT ON note_links "
    "FOR EACH ROW EXECUTE FUNCTION poison_links()",
    # `batch`: a class-22 failure only a multi-row INSERT produces, so no
    # single row reproduces it — the case the replay must not attribute.
    """
    CREATE OR REPLACE FUNCTION poison_batch() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      IF (SELECT count(*) FROM inserted) > 1
         AND EXISTS (SELECT 1 FROM poison_rules WHERE site = 'batch') THEN
        RAISE EXCEPTION 'synthetic batch-only failure for a test'
          USING ERRCODE = '22000';
      END IF;
      RETURN NULL;
    END $$
    """,
    "DROP TRIGGER IF EXISTS poison_batch ON notes_metadata",
    "CREATE TRIGGER poison_batch AFTER INSERT ON notes_metadata "
    "REFERENCING NEW TABLE AS inserted "
    "FOR EACH STATEMENT EXECUTE FUNCTION poison_batch()",
    "DELETE FROM poison_rules",
]

_UNINSTALL = [
    "DROP TRIGGER IF EXISTS poison_notes ON notes_metadata",
    "DROP TRIGGER IF EXISTS poison_links ON note_links",
    "DROP TRIGGER IF EXISTS poison_batch ON notes_metadata",
    "DROP FUNCTION IF EXISTS poison_batch()",
    "DROP FUNCTION IF EXISTS poison_notes()",
    "DROP FUNCTION IF EXISTS poison_links()",
    "DROP FUNCTION IF EXISTS poison_fire(text, integer)",
    "DROP TABLE IF EXISTS poison_rules",
    "DROP SEQUENCE IF EXISTS poison_shot_seq",
]


async def install(sessionmaker) -> None:
    async with sessionmaker() as session:
        for stmt in _INSTALL:
            await session.execute(text(stmt))
        await session.commit()


async def uninstall(sessionmaker) -> None:
    async with sessionmaker() as session:
        for stmt in _UNINSTALL:
            await session.execute(text(stmt))
        await session.commit()


async def poison(
    sessionmaker, site: str, path: str, errcode: str = "54000",
    shots: int | None = None,
) -> None:
    async with sessionmaker() as session:
        if shots is not None:
            await session.execute(text("ALTER SEQUENCE poison_shot_seq RESTART"))
        await session.execute(
            text(
                "INSERT INTO poison_rules (site, path, errcode, shots) "
                "VALUES (:s, :p, :c, :n)"
            ),
            {"s": site, "p": path, "c": errcode, "n": shots},
        )
        await session.commit()


async def cure(sessionmaker, path: str | None = None) -> None:
    async with sessionmaker() as session:
        if path is None:
            await session.execute(text("DELETE FROM poison_rules"))
        else:
            await session.execute(
                text("DELETE FROM poison_rules WHERE path = :p"), {"p": path}
            )
        await session.commit()
