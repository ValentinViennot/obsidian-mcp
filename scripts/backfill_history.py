"""Backfill temporal embeddings for a git-backed vault.

Invoked by `make backfill-history` (and `make backfill-history-dry`), which run
it as a one-off `docker compose run --rm` container so it reads the current
`.env` and works whether the service is up or down — the same reasoning
`make reset-embeddings` documents (#142).

    python -m scripts.backfill_history --dry-run
    python -m scripts.backfill_history --user-id 3 --limit 200

## What it does, and what it deliberately does not

It walks the vault's git history (`src/services/history_indexer.py`), embeds
every *superseded* content version of every note, and stores each with the
half-open interval over which that text was the note's content. The note's
present content is not re-embedded — its rows already exist and are the ones
every search reads; only their `valid_from` is stamped.

**Ordinary search is unchanged by this.** Every current-state reader filters on
`valid_to IS NULL`, so the rows this writes are invisible to `semantic_search`
and `find_related` unless a caller explicitly asks for a point in time. That is
asserted by `tests/test_temporal_current_state_queries.py` at the statement
level and by `tests/integration/test_temporal_embeddings_pg.py` against a real
database; if either fails, do not run this against a vault you care about.

## Running it against a large repository

- `--dry-run` walks the whole history, chunks every version and reports how
  many versions and chunks *would* be embedded, without calling the provider
  and without writing anything. Run it first: it is the only way to know what
  a real run will cost, and on a decade-old vault the answer can be an order of
  magnitude more chunks than the vault has today.
- Work is committed **one note at a time** and an already-backfilled version is
  skipped by matching its exact interval, so an interrupted run resumes rather
  than restarting and a completed run is a no-op. There is no cursor to reset
  and nothing to clean up after a Ctrl-C.
- `--limit` bounds a run to N notes, which is how to spread a first backfill
  over several windows rather than holding the embedding provider for hours.
- Progress is printed every `--progress-every` notes and the same line is
  logged, so a run inside a container is followable with `docker logs`.

## The provider load this puts on a shared endpoint

Every version is a provider call, and a vault's history is much larger than its
present. `OllamaProvider` batches natively (32 chunks per request), which is
what makes this practical at all — but the endpoint is shared, so prefer a
`--limit`ed run during quiet hours over one unbounded run.
"""
import argparse
import asyncio
import logging
import sys

from src.config import settings
from src.database import async_session, engine
from src.services.history_indexer import (
    GitError,
    backfill_vault_history,
    count_current_and_historical,
)

logger = logging.getLogger("scripts.backfill_history")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backfill_history",
        description="Embed a git-backed vault's superseded note versions.",
    )
    parser.add_argument(
        "--vault",
        default=None,
        help="Vault directory (default: VAULT_PATH). Must be inside a git "
        "repository; a vault in a subdirectory of a larger repo is fine.",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Owner of the notes to backfill. Omit for the NULL-owned slice, "
        "which is what a single-user deployment has. This is a scoping "
        "value, not the absence of one — it maps exactly as the read path's "
        "owner predicate does.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be embedded and write nothing. Makes no "
        "provider call.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after this many notes. Safe to re-run: an already "
        "backfilled version is skipped, so successive limited runs make "
        "progress.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Print a progress line every N notes (0 to disable).",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    vault = args.vault or settings.vault_path
    mode = "DRY RUN — nothing will be written" if args.dry_run else "writing"
    print(f"History backfill of {vault} ({mode})")

    async with async_session() as session:
        before = await count_current_and_historical(session)
    print(f"Before: {before[0]} current rows, {before[1]} historical rows")

    stats = await backfill_vault_history(
        async_session,
        vault,
        user_id=args.user_id,
        dry_run=args.dry_run,
        limit=args.limit,
        progress_every=max(0, args.progress_every),
        on_progress=print,
    )

    print(stats.render())
    if args.dry_run:
        print(
            f"Would embed {stats.versions_embedded} versions "
            f"({stats.chunks_embedded} chunks). Nothing was written."
        )
        return 0

    async with async_session() as session:
        after = await count_current_and_historical(session)
    print(f"After:  {after[0]} current rows, {after[1]} historical rows")
    if after[0] != before[0]:
        # Loud, because the one thing this tool must never do is change what an
        # ordinary search returns. It only ever inserts rows with a non-NULL
        # `valid_to` and updates `valid_from` on existing current rows, so the
        # count of current rows is an invariant of the run.
        print(
            "WARNING: the number of CURRENT embedding rows changed "
            f"({before[0]} -> {after[0]}). The backfill must never add or "
            "remove current vectors — an ordinary index pass running "
            "concurrently would explain it, anything else is a bug. Verify "
            "before trusting search results.",
            file=sys.stderr,
        )
    if stats.notes_without_metadata:
        print(
            f"{stats.notes_without_metadata} path(s) had history but no "
            "notes_metadata row — deleted notes, or notes the indexer has not "
            "reached yet. Their history was not stored; see the module "
            "docstring in src/services/history_indexer.py."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args(argv)
    try:
        return asyncio.run(run(args))
    except GitError as exc:
        print(f"backfill_history failed: {exc}", file=sys.stderr)
        return 2
    finally:
        try:
            asyncio.run(engine.dispose())
        except Exception:  # pragma: no cover - best-effort teardown
            pass


if __name__ == "__main__":
    sys.exit(main())
