"""`list_tasks` rendering, cursor and argument validation (#350, D6–D8).

Offline. The renderer and the cursor are pure; the paging tests emulate the
query's ordering and resume predicate in Python (`page`) so they can follow
`next` to the end. The PostgreSQL side — the same predicate in SQL, the
compact-cursor resolution, tenancy — is pinned in
`tests/integration/test_list_tasks_pg.py`.
"""
import asyncio
import base64
import datetime
import hashlib
import json
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.tools import (  # noqa: E402
    LIST_TASKS_ABBREVIATED_STRUCTURE_MAX,
    LIST_TASKS_FOOTER_MAX,
    LIST_TASKS_HEADER_MAX,
    _task_filters_digest,
    _task_owner_discriminator,
    decode_task_cursor,
    encode_task_cursor,
    render_abbreviated_task_row,
    render_incomplete_notice,
    render_task_header,
    render_task_results,
    render_task_row,
)

D = datetime.date


def row(path="Projects/Alpha.md", line=1, *, status_char=" ", text="t", due=None,
        scheduled=None, start=None, done_on=None, priority=None, tags=None,
        tags_cut=False, heading_path=None, text_cut=False, state=None):
    return {
        "status_char": status_char, "file_path": path, "line": line, "due": due,
        "scheduled": scheduled, "start": start, "done_on": done_on,
        "priority": priority, "tags": tags, "tags_cut": tags_cut,
        "heading_path": heading_path, "text": text, "text_cut": text_cut,
        "state": state or {"x": "done", "X": "done", "-": "cancelled"}.get(status_char, "open"),
    }


def header_args(**kw):
    base = dict(status="open", folder=None, due_by=None, scheduled_by=None, tags=None,
                not_scanned=0, failed=0, capped=0, cut_tags=None)
    base.update(kw)
    return base


OWNER = _task_owner_discriminator(7)
FILTERS = _task_filters_digest(None, "open", None, None, [])


def render(rows, *, budget=40_000, more_rows=False, **kw):
    return render_task_results(
        rows, budget=budget, header_args=header_args(**kw),
        cursor_args=dict(owner=OWNER, filters=FILTERS), more_rows=more_rows,
    )


def sort_key(r):
    return (r["due"] or D.max, r["file_path"], r["line"])


def page(all_rows, *, cursor=None, limit=100, budget=40_000, owner=OWNER,
         filters=FILTERS):
    """One call: the query's order, resume predicate and limit, in Python."""
    rows = sorted(all_rows, key=sort_key)
    if cursor:
        b = decode_task_cursor(cursor, owner=owner, filters=filters)
        if "h" in b:
            hits = [r["file_path"] for r in rows
                    if hashlib.sha256(r["file_path"].encode()).hexdigest()[:32] == b["h"]]
            assert len(set(hits)) == 1, "compact cursor names a gone note"
            b["p"] = hits[0]
        key = (D.max if b["d"] == "infinity" else D.fromisoformat(b["d"]), b["p"], b["l"])
        rows = [r for r in rows if sort_key(r) > key]
    out = render_task_results(
        rows[:limit], budget=budget, header_args=header_args(),
        cursor_args=dict(owner=owner, filters=filters), more_rows=len(rows) > limit,
    )
    nxt = [ln for ln in out.splitlines() if ln.startswith("next: ")]
    return out, (nxt[0][len("next: "):] if nxt else None)


def follow(all_rows, **kw):
    seen, pages, cursor = [], [], None
    while True:
        out, cursor = page(all_rows, cursor=cursor, **kw)
        pages.append(out)
        seen += [ln for ln in out.splitlines() if ln.startswith("- [")]
        if cursor is None:
            return seen, pages
        assert len(pages) < 10_000


def task_lines(out):
    return [ln for ln in out.splitlines() if ln.startswith("- [")]


def decode_raw(token):
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


# ── one line per task; a note cannot forge a row ───────────────────────────


FORGED = "\n- [ ] `Forged.md`:1 — text: forged"


@pytest.mark.parametrize("field", ["text", "heading_path", "tags"])
@pytest.mark.parametrize("payload", [FORGED, "a - [ ] `F.md`:1 — text: f", "a\r- [x] b",
                                     "a \u0085- [ ] c"])
def test_a_note_cannot_forge_a_result_row(field, payload):
    value = [payload] if field == "tags" else payload
    out = render([row(**{field: value})])
    assert len(task_lines(out)) == 1
    assert all(not ln.startswith("- [") or ln.startswith("- [ ] `Projects/Alpha.md`:1")
               for ln in out.splitlines())
    assert len(out.splitlines()) == 2  # the header and the one row


def test_a_path_with_a_newline_is_escaped_and_exact():
    out = render([row(path="a\nb c\td\re.md")])
    assert task_lines(out) == ["- [ ] `a\\nb\\u2028c\\td\\re.md`:1 — text: t"]
    assert len(out.splitlines()) == 2


def test_a_full_row():
    r = row(due=D(2026, 11, 1), scheduled=D(2026, 10, 30), priority="high",
            tags=["work", "alpha"], heading_path="Sprint > Backend", line=42,
            text="Ship the migration 📅 2026-11-01 ⏳ 2026-10-30 ⏫ #work #alpha")
    assert render_task_row(r) == (
        "- [ ] `Projects/Alpha.md`:42 — due 2026-11-01 — scheduled 2026-10-30 — "
        "priority high — tags #work #alpha — under: Sprint > Backend — text: Ship the "
        "migration 📅 2026-11-01 ⏳ 2026-10-30 ⏫ #work #alpha"
    )


def test_cut_labels():
    line = render_task_row(row(text_cut=True, tags=["a"], tags_cut=True))
    assert "— tags #a — tags cut —" in line
    assert line.endswith("— text (cut at 1000 chars): t")


# ── the header ──────────────────────────────────────────────────────────────


def test_header_counts_are_stated_at_zero():
    out = render([])
    assert out == (
        "Tasks — status: open — shown: 0 — more: no — not yet scanned for tasks: 0 "
        "notes — tasks could not be indexed: 0 notes — over the task cap: 0 notes"
    )
    out = render([], tags=["x"], cut_tags=0)
    assert out.endswith("— tasks with cut tags: 0")


def test_an_empty_result_is_a_header_and_not_a_refusal():
    out = render([])
    assert "MCP-REFUSAL" not in out and "next:" not in out
    assert out.startswith("Tasks — ") and "shown: 0" in out


def test_the_incomplete_notice():
    assert render_incomplete_notice(0, 0) is None
    out = render([row()], not_scanned=3)
    assert "not yet scanned for tasks: 3 notes" in out
    assert "Incomplete list:" in out.splitlines()[-1]
    out = render([row()], failed=1)
    assert "tasks could not be indexed: 1 notes" in out
    assert "could not be indexed" in out.splitlines()[-1]


def test_echoes_are_cut_at_64():
    out = render([], folder="F" * 300, tags=["t" * 128] * 32, cut_tags=0)
    header = out.splitlines()[0]
    assert "folder: " + "F" * 63 + "…" in header
    assert len(header) <= LIST_TASKS_HEADER_MAX


def test_the_fixed_bounds():
    worst = render_task_header(
        status="cancelled", folder="F" * 1024, due_by="2026-01-01",
        scheduled_by="2026-01-01", tags=["t" * 128] * 32, shown=999, more=True,
        not_scanned=10**10 - 1, failed=10**10 - 1, capped=10**10 - 1, cut_tags=10**10 - 1,
    )
    assert len(worst) <= LIST_TASKS_HEADER_MAX, len(worst)
    assert len(render_incomplete_notice(1, 1)) <= 85
    compact = "next: " + encode_task_cursor(
        owner=OWNER, filters=FILTERS, due_key="infinity", path="é" * 1024,
        line=2_147_483_647, compact=True,
    )
    assert len(compact) <= LIST_TASKS_FOOTER_MAX, len(compact)
    r = row(path="", text="", line=10**10 - 1, tags=["a"], tags_cut=True,
            heading_path="h", status_char="X")
    structure = render_abbreviated_task_row(r, 10_000)
    assert len(structure) <= LIST_TASKS_ABBREVIATED_STRUCTURE_MAX, structure
    # The design's arithmetic: at least 200 characters of the minimum cap
    # remain for an abbreviated first row's path and text.
    assert LIST_TASKS_HEADER_MAX + LIST_TASKS_FOOTER_MAX + LIST_TASKS_ABBREVIATED_STRUCTURE_MAX <= 1000 - 200


# ── the character budget ────────────────────────────────────────────────────


def test_the_character_cap_stops_a_page_early():
    rows = [row(path=f"n{i:03d}.md", text="x" * 900) for i in range(500)]
    out, cursor = page(rows, limit=500, budget=40_000)
    assert len(out) <= 40_000
    assert "more: yes" in out.splitlines()[0]
    shown = task_lines(out)
    assert 0 < len(shown) < 500
    last_path = f"n{len(shown) - 1:03d}.md"
    assert decode_raw(cursor)["p"] == last_path
    out2, _ = page(rows, limit=500, budget=40_000, cursor=cursor)
    assert task_lines(out2)[0].startswith(f"- [ ] `n{len(shown):03d}.md`")


def test_paging_reaches_every_row_once():
    rows = [row(path=f"n{i % 21:02d}.md", line=i + 1,
                due=None if i % 3 else D(2026, 1, 1) + datetime.timedelta(days=i % 40))
            for i in range(1234)]
    seen, pages = follow(rows, limit=100)
    assert len(seen) == 1234 == len(set(seen))
    assert len(pages) == 13


def test_a_page_boundary_between_two_undated_tasks():
    rows = [row(path="a.md", line=n) for n in (1, 2, 3)]
    seen, pages = follow(rows, limit=1)
    assert seen == [f"- [ ] `a.md`:{n} — text: t" for n in (1, 2, 3)]
    assert len(pages) == 3 and "next:" not in pages[-1]
    assert decode_raw(page(rows, limit=1)[1])["d"] == "infinity"


# ── the cursor ──────────────────────────────────────────────────────────────


def test_cursor_round_trip_and_shape():
    token = encode_task_cursor(owner=OWNER, filters=FILTERS, due_key="2026-11-01",
                               path="Projects/Alpha.md", line=42, compact=False)
    assert decode_task_cursor(token, owner=OWNER, filters=FILTERS) == {
        "d": "2026-11-01", "p": "Projects/Alpha.md", "l": 42,
    }
    raw = decode_raw(token)
    assert set(raw) == {"v", "o", "f", "d", "p", "l"}
    assert "=" not in token


@pytest.mark.parametrize("mutate", [
    lambda p: {**p, "v": 2},
    lambda p: {**p, "d": None},
    lambda p: {**p, "d": "2026-02-30"},
    lambda p: {**p, "l": 0},
    lambda p: {**p, "l": True},
    lambda p: {**p, "l": "3"},
    lambda p: {**p, "h": "0" * 32},             # both p and h
    lambda p: {k: v for k, v in p.items() if k != "p"},  # neither
    lambda p: {**p, "p": "x" * 1025},
    lambda p: {**p, "id": 5},
    lambda p: {**p, "p": "a\x00b.md"},          # NUL: never a stored path
    lambda p: {**p, "p": "a\ud800b.md"},        # unpaired surrogate
])
def test_a_tampered_cursor_is_refused(mutate):
    token = encode_task_cursor(owner=OWNER, filters=FILTERS, due_key="infinity",
                               path="a.md", line=1, compact=False)
    payload = mutate(decode_raw(token))
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(tools._TaskCursorRefused):
        decode_task_cursor(forged, owner=OWNER, filters=FILTERS)


@pytest.mark.parametrize("token", ["", "!!!", "e30", "bm90IGpzb24", "W10"])
def test_garbage_cursors_are_refused(token):
    with pytest.raises(tools._TaskCursorRefused):
        decode_task_cursor(token, owner=OWNER, filters=FILTERS)


def test_a_cursor_with_another_filter_digest_is_refused():
    token = encode_task_cursor(owner=OWNER, filters=FILTERS, due_key="infinity",
                               path="a.md", line=1, compact=False)
    done = _task_filters_digest(None, "done", None, None, [])
    assert done != FILTERS
    with pytest.raises(tools._TaskCursorRefused):
        decode_task_cursor(token, owner=OWNER, filters=done)


def test_a_cursor_with_another_owners_discriminator_is_refused():
    token = encode_task_cursor(owner=OWNER, filters=FILTERS, due_key="infinity",
                               path="a.md", line=1, compact=False)
    for other in (_task_owner_discriminator(8), _task_owner_discriminator(None)):
        assert other != OWNER
        with pytest.raises(tools._TaskCursorRefused):
            decode_task_cursor(token, owner=other, filters=FILTERS)


def test_the_filter_digest_normalises():
    assert _task_filters_digest(None, "open", None, None, ["b", "a"]) == \
        _task_filters_digest("", "open", None, None, ["a", "b"])
    assert _task_filters_digest("P/", "open", None, None, []) != FILTERS


def test_the_compact_form_is_chosen_for_a_long_multibyte_path():
    path = "語" * 1024
    rows = [row(path=path, line=1), row(path=path, line=2)]
    out, cursor = page(rows, limit=1)
    assert len(cursor) <= 2048
    raw = decode_raw(cursor)
    assert "h" in raw and "p" not in raw
    out2, cursor2 = page(rows, limit=1, cursor=cursor)
    assert task_lines(out2)[0].endswith("— text: t") and f"`:{2}" in task_lines(out2)[0]
    assert cursor2 is None


def test_the_compact_form_is_chosen_at_the_minimum_cap():
    rows = [row(path="p" * 600, line=n, text="x" * 50) for n in (1, 2)]
    out, cursor = page(rows, limit=1, budget=1000)
    assert len(out) <= 1000
    assert "h" in decode_raw(cursor)


def test_no_index_id_appears_in_a_cursor():
    token = encode_task_cursor(owner=OWNER, filters=FILTERS, due_key="infinity",
                               path="a.md", line=1, compact=True)
    assert set(decode_raw(token)) == {"v", "o", "f", "d", "h", "l"}


# ── status, dates and tags at the tool boundary ─────────────────────────────


class _Result:
    def __init__(self, rows=(), scalar=0, one=(0, 0)):
        self._rows, self._scalar, self._one = list(rows), scalar, one

    def scalar_one(self):
        return self._scalar

    def one(self):
        return self._one

    def all(self):
        return self._rows

    def scalars(self):
        return self


class _FakeSession:
    """Records statements; answers every count with zero and the page with
    nothing. Enough to show that validation runs before any statement."""

    def __init__(self):
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, stmt, *_a, **_k):
        self.statements.append(str(stmt))
        return _Result()


@pytest.fixture
def fake_db(monkeypatch):
    sessions = []

    def factory():
        s = _FakeSession()
        sessions.append(s)
        return s

    monkeypatch.setattr(tools, "async_session", factory)
    return sessions


def call(**kw):
    return asyncio.run(tools.list_tasks_impl.__wrapped__(**kw))


@pytest.mark.parametrize("kw,bound", [
    (dict(status="pending"), "status must be one of"),
    (dict(due_by="tomorrow"), "due_by must be a YYYY-MM-DD"),
    (dict(scheduled_by="2026-02-30"), "scheduled_by must be a YYYY-MM-DD"),
    (dict(tags=["t"] * 33), "at most 32"),
    (dict(tags=["a" * 129]), "1 to 128"),
    (dict(tags=["#"]), "1 to 128"),
    (dict(tags=[""]), "1 to 128"),
    (dict(tags=[7]), "must be a string"),
    (dict(cursor="garbage"), "does not belong to this query"),
])
def test_invalid_arguments_are_refused_before_any_query(fake_db, kw, bound):
    out = call(**kw)
    assert bound in out
    assert out.rstrip().splitlines()[-1].startswith('MCP-REFUSAL {"code":"invalid_argument"')
    assert fake_db == [], "no session may be opened for an invalid argument"


def test_the_tags_argument_bounds(fake_db):
    assert "invalid_argument" in call(tags=["t"] * 33)
    assert "invalid_argument" in call(tags=["a" * 129])
    assert "invalid_argument" in call(tags=["#"])
    assert fake_db == []
    out = call(tags=["#" + "a" * 128] + ["b" * 128] * 31)
    assert "MCP-REFUSAL" not in out
    assert "tasks with cut tags: 0" in out
    assert len(fake_db) == 1


def test_the_query_runs_in_one_read_only_snapshot(fake_db):
    call(status="all", due_by="2026-10-29", folder="Projects/")
    stmts = fake_db[0].statements
    assert stmts[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    page_sql = stmts[-1]
    assert "JOIN notes_metadata ON notes_metadata.id = note_tasks.note_id AND" in page_sql
    assert "notes_metadata.user_id IS NULL" in page_sql
    assert "ORDER BY coalesce(note_tasks.due" in page_sql


def _filter(rows, status):
    return [r for r in rows if status == "all" or r["state"] == status]


def test_status_mapping_in_rendering():
    rows = [row(path="n.md", line=1, status_char=" ", text="a"),
            row(path="n.md", line=2, status_char="/", text="b"),
            row(path="n.md", line=3, status_char="-", text="c"),
            row(path="n.md", line=4, status_char="x", text="d")]
    opened = task_lines(render(_filter(rows, "open")))
    assert [ln.rsplit(": ", 1)[1] for ln in opened] == ["a", "b"]
    cancelled = task_lines(render(_filter(rows, "cancelled")))
    assert cancelled == ["- [-] `n.md`:3 — text: c"]
    every = task_lines(render(_filter(rows, "all")))
    assert [ln[:5] for ln in every] == ["- [ ]", "- [/]", "- [-]", "- [x]"]


def test_the_due_filter_is_inclusive(fake_db):
    call(due_by="2026-10-29")
    page_sql = fake_db[0].statements[-1]
    assert "note_tasks.due <= " in page_sql


# ── the zero-rows-fit regression (Codex spec round 1) ───────────────────────


def test_a_first_row_that_cannot_fit_is_abbreviated_not_lost():
    path = "p" * 1024
    tags = [f"{i:02d}" + "t" * 126 for i in range(64)]
    rows = [row(path=path, line=n, text="x" * 1000, tags=tags, heading_path="H",
                due=D(2026, 1, n), priority="high") for n in range(1, 6)]
    seen, pages = follow(rows, limit=100, budget=1000)
    assert len(pages) == 5
    for out in pages:
        assert len(out) <= 1000, len(out)
        lines = task_lines(out)
        assert len(lines) == 1 and lines[0].endswith("— abbreviated")
        assert "— path cut" in lines[0] and "— tags omitted" in lines[0]
        assert "— due 2026-01-0" in lines[0] and "— priority high" in lines[0]
        assert "text (cut): x" in lines[0]
    assert len(seen) == 5 == len({ln.split("— due ")[1][:10] for ln in seen})


def test_an_abbreviated_row_keeps_a_short_path_whole():
    r = row(path="short.md", text="y" * 5000)
    line = render_abbreviated_task_row(r, 300)
    assert len(line) <= 300
    assert "`short.md`:1" in line and "path cut" not in line
    assert line.endswith("— abbreviated")


# ── Codex implementation review, round 1 ────────────────────────────────────


def test_an_abbreviated_row_says_its_tags_were_cut():
    r = row(path="p" * 1024, text="x" * 1000, tags=None, tags_cut=True)
    line = render_abbreviated_task_row(r, 300)
    assert "— tags omitted" in line and "— tags cut" in line
    r = row(path="p" * 1024, text="x" * 1000, tags=["a"] * 64, tags_cut=True)
    assert "— tags cut" in render_abbreviated_task_row(r, 300)
    r = row(path="p" * 1024, text="x" * 1000, tags=None, tags_cut=False)
    assert "tags" not in render_abbreviated_task_row(r, 300)


def test_path_escaping_is_injective():
    backslash_n = render_task_row(row(path="a\\nb.md"))
    newline = render_task_row(row(path="a\nb.md"))
    assert backslash_n != newline
    assert "`a\\\\nb.md`" in backslash_n and "`a\\nb.md`" in newline
    assert render_task_row(row(path="a\\u2028.md")) != render_task_row(row(path="a\u2028.md"))


def test_the_worst_case_at_the_minimum_cap_keeps_40_of_path_and_text():
    """Design D7 as corrected: header, closing notice, compact footer and an
    abbreviated row keeping four dates and a priority still leave at least 40
    characters each of path and text at `MAX_READ_RESPONSE_CHARS=1000`."""
    big = 10**10 - 1
    first = D(2026, 1, 1)
    rows = [row(path="é" * 1024, line=big, text="x" * 1000, due=first, scheduled=first,
                start=first, done_on=first, priority="highest",
                tags=["t" * 128] * 64, tags_cut=True, heading_path="H" * 512),
            row(path="é" * 1024, line=big + 1, text="y")]
    out = render_task_results(
        rows, budget=1000,
        header_args=header_args(status="cancelled", folder="F" * 1024,
                                due_by="2026-01-01", scheduled_by="2026-01-01",
                                tags=["t" * 128] * 32, not_scanned=big, failed=big,
                                capped=big, cut_tags=big),
        cursor_args=dict(owner=OWNER, filters=FILTERS), more_rows=True,
    )
    assert len(out) <= 1000, len(out)
    (line,) = task_lines(out)
    assert line.endswith("— abbreviated") and "— tags cut" in line
    for field in ("due 2026-01-01", "scheduled 2026-01-01", "start 2026-01-01",
                  "done 2026-01-01", "priority highest"):
        assert field in line
    path_shown = line.split("`")[1]
    text_shown = line.split("text (cut): ", 1)[1].rsplit(" — abbreviated", 1)[0]
    assert len(path_shown) >= 40 and len(text_shown) >= 40, (len(path_shown), len(text_shown))
    assert out.splitlines()[-1].startswith("next: ")
