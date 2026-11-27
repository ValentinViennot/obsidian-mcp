"""New destination names are made portable, and the response says so.

The vault is synced by Git to Android/Windows/macOS, which cannot hold `?`, `:`,
`"`, `*`, a trailing dot, `CON.md`, or two siblings differing only by case.
Owner's rule: "Fix names automatically but not silently". The pure rules live in
`src/services/portable_path.py`; the wiring (create_note, move_note, write_file,
request_upload, import_from_url) is exercised against a real tmp vault.
"""
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import git_vault, transfer, vault_fs  # noqa: E402
from src.services import portable_path as pp  # noqa: E402
from src.services.refusals import BODY_CODES  # noqa: E402


# ── pure rules ──────────────────────────────────────────────────────────────


def tree(*paths):
    """A `list_siblings` over a dict of vault-relative files and folders.

    A trailing `/` marks a folder.
    """
    dirs: dict[str, set] = {}
    for p in paths:
        is_dir = p.endswith("/")
        parts = p.rstrip("/").split("/")
        for i in range(len(parts)):
            parent = "/".join(parts[:i])
            last = i == len(parts) - 1
            dirs.setdefault(parent, set()).add((parts[i], (not last) or is_dir))

    def list_siblings(parent):
        return sorted(dirs.get(parent, ()))

    return list_siblings


NOTHING = tree()


def norm(path, *existing, ignore=None):
    return pp.normalize_destination(path, tree(*existing), ignore=ignore)


def test_a_portable_name_is_returned_unchanged_and_without_notes():
    path = "projects/Café d'été/Réunion 2026.md"
    out, notes = pp.normalize_destination(path, NOTHING)
    assert out is path and notes == []


def test_accents_apostrophes_spaces_and_capitals_are_kept():
    out, notes = norm("Notes/L'été à Paris: Été.md")
    assert out == "Notes/L'été à Paris - Été.md"
    assert notes


def test_a_trailing_question_mark_is_dropped_not_hyphenated():
    out, notes = norm("Why now?.md")
    assert out == "Why now.md"
    assert any("trailing" in n for n in notes)


def test_a_trailing_quote_is_dropped():
    out, _ = norm('Quote".md')
    assert out == "Quote.md"


def test_a_colon_inside_a_name_becomes_a_hyphen():
    out, notes = norm("Plan: phase 1.md")
    assert out == "Plan - phase 1.md"
    assert any("':'" in n for n in notes)


def test_every_forbidden_character_is_replaced_and_runs_collapse():
    out, _ = norm("a?b.md")
    assert out == "a-b.md"
    out, _ = norm('a<>:"\\|?*[]#^b.md')
    assert out == "a-b.md"
    out, _ = norm("tab\tand\x01ctrl.md")
    assert "\t" not in out and "\x01" not in out


def test_control_characters_are_reported_by_code_point():
    _, notes = norm("a\x01b.md")
    assert any("U+0001" in n for n in notes)


def test_trailing_dots_and_spaces_are_stripped_at_the_end_of_a_component_only():
    assert norm("a.md ")[0] == "a.md"
    assert norm("Folder. /x.md")[0] == "Folder/x.md"
    assert norm("name.")[0] == "name"
    assert norm("Why now ?.md")[0] == "Why now .md"


def test_leading_spaces_and_a_space_before_the_extension_are_legal_and_left_alone():
    for p in (" foo.md", "foo .md", "a. .md", " lead/ x .md"):
        out, notes = pp.normalize_destination(p, NOTHING)
        assert out is p and notes == [], p
    # ... so they never collide with the name they used to be rewritten to.
    assert pp.normalize_destination(" foo.md", tree("foo.md")) == (" foo.md", [])


def test_a_name_changes_exactly_when_a_reason_is_given():
    import itertools

    atoms = ["a", " ", ".", "?", '"', ":", "é", "\x01", "md", "CON"]
    for n in range(1, 5):
        for combo in itertools.product(atoms, repeat=n):
            name = "".join(combo)
            if name.startswith(".") or name.strip(" ") == "":
                continue
            for is_file in (True, False):
                try:
                    out, why = pp.portable_component(name, is_file=is_file)
                except pp.PortablePathRefusal:
                    continue
                assert (out != name) == bool(why), (name, is_file, out, why)


def test_windows_device_names_get_a_hyphen_suffix_on_the_stem():
    assert norm("CON.md")[0] == "CON-.md"
    assert norm("con.md")[0] == "con-.md"
    assert norm("notes/LPT3.txt")[0] == "notes/LPT3-.txt"
    assert norm("AUX/x.md")[0] == "AUX-/x.md"
    assert norm("Console.md")[0] == "Console.md"


def test_a_long_component_is_shortened_at_a_character_boundary_keeping_the_extension():
    name = "é" * 200 + ".md"  # 400 bytes
    out, notes = norm(name)
    assert out.endswith(".md")
    assert len(out.encode("utf-8")) <= pp.MAX_COMPONENT_BYTES
    assert out.encode("utf-8").decode("utf-8") == out  # no half character
    assert any("240" in n for n in notes)


def test_nfd_input_is_normalised_to_nfc():
    import unicodedata

    nfd = unicodedata.normalize("NFD", "Café.md")
    out, notes = norm(nfd)
    assert out == unicodedata.normalize("NFC", "Café.md") and notes


def test_an_empty_result_is_refused():
    for bad in ("???.md", "?", '"', "a/ ."):
        with pytest.raises(pp.PortablePathRefusal) as e:
            norm(bad)
        assert e.value.code == pp.UNPORTABLE_PATH


def test_existing_components_are_never_changed_whatever_they_contain():
    out, notes = norm("weird?dir:/old: note?.md", "weird?dir:/", "weird?dir:/old: note?.md")
    assert out == "weird?dir:/old: note?.md" and notes == []


def test_a_new_file_in_an_existing_weird_folder_only_changes_the_new_part():
    out, _ = norm("weird?dir:/new?.md", "weird?dir:/")
    assert out == "weird?dir:/new.md"


def test_an_existing_folder_differing_only_by_case_is_reused():
    out, notes = norm("projects/KINKVERSE/x.md", "projects/kinkverse/")
    assert out == "projects/kinkverse/x.md"
    assert any("kinkverse" in n for n in notes)


def test_an_existing_folder_differing_only_by_unicode_form_is_reused():
    import unicodedata

    existing = unicodedata.normalize("NFD", "Café")
    out, _ = norm("Café/x.md", existing + "/")
    assert out == existing + "/x.md"


def test_the_folder_case_match_is_per_parent_not_global():
    out, notes = norm("a/KINKVERSE/x.md", "projects/kinkverse/")
    assert out is not None and notes == []


def test_a_new_file_colliding_by_case_with_an_existing_file_is_refused():
    with pytest.raises(pp.PortablePathRefusal) as e:
        norm("notes/Plan.md", "notes/plan.md")
    assert e.value.code == pp.PATH_CASE_COLLISION
    assert "notes/plan.md" in e.value.message


def test_a_sanitised_name_that_lands_on_an_existing_file_is_refused_not_overwritten():
    with pytest.raises(pp.PortablePathRefusal) as e:
        norm("Why now?.md", "Why now.md")
    assert e.value.code == pp.UNPORTABLE_PATH


def test_a_file_name_colliding_with_a_folder_is_refused():
    with pytest.raises(pp.PortablePathRefusal):
        norm("Archive", "archive/")


def test_a_file_cannot_take_a_folders_place_as_a_parent():
    with pytest.raises(pp.PortablePathRefusal) as e:
        norm("notes/x.md", "Notes")
    assert e.value.code == pp.PATH_CASE_COLLISION


def test_the_exact_existing_name_is_left_to_the_tools_own_exists_logic():
    out, notes = norm("notes/plan.md", "notes/plan.md", "notes/Plan.md")
    assert out == "notes/plan.md" and notes == []


def test_a_renamed_or_reused_folder_never_steers_onto_an_existing_leaf():
    for dest, existing in (
        ("Projects/logo.png", ("projects/logo.png",)),
        ("x?/a.png", ("x/a.png",)),
        ("x?/a.png", ("x/", "x/a.png")),
        ("Notes/foo.md", ("notes/foo.md",)),
    ):
        with pytest.raises(pp.PortablePathRefusal) as e:
            norm(dest, *existing)
        assert e.value.code == pp.PATH_CASE_COLLISION, dest
        assert "path_normalized:" in e.value.message and dest in e.value.message


def test_a_leaf_that_exists_under_an_unchanged_path_is_still_the_tools_decision():
    assert norm("projects/logo.png", "projects/logo.png") == ("projects/logo.png", [])


def test_a_destination_that_normalizes_onto_the_move_source_is_explained():
    with pytest.raises(pp.PortablePathRefusal) as e:
        norm("notes/foo.md", "Notes/foo.md", ignore="Notes/foo.md")
    assert "source" in e.value.message and "path_normalized:" in e.value.message


def test_collision_refusals_after_a_folder_rename_carry_the_explanation():
    with pytest.raises(pp.PortablePathRefusal) as e:
        norm("x?/Plan.md", "x/", "x/plan.md")
    assert 'path_normalized: "x?/Plan.md" -> "x/Plan.md"' in e.value.message


def test_ignore_lets_a_case_only_rename_of_the_source_through():
    out, notes = norm("notes/plan.md", "notes/Plan.md", ignore="notes/Plan.md")
    assert out == "notes/plan.md" and notes == []


def test_dot_prefixed_names_dots_and_traversal_pass_through_for_the_existing_guard():
    for p in (".hidden/x.md", "a/.x.md", "../x.md", "a/../x.md", "a/", "/abs.md"):
        assert pp.normalize_destination(p, NOTHING) == (p, [])


def test_empty_and_dot_segments_are_left_alone_unless_something_else_changes():
    p = "a//b/./c.md"
    assert pp.normalize_destination(p, NOTHING) == (p, [])
    assert norm("a//b/./c?.md")[0] == "a/b/c.md"


def test_the_new_refusal_codes_are_declared():
    assert {pp.UNPORTABLE_PATH, pp.PATH_CASE_COLLISION} <= BODY_CODES


def test_commit_message_carries_the_note_before_the_trailers():
    msg = git_vault.build_message(
        "create_note", ["a.md"], "key", notes=['path_normalized: "a?.md" -> "a.md" (x)']
    )
    body, _, trailers = msg.rpartition("\n\n")
    assert "path_normalized" in body and trailers.startswith("Tool: create_note")


# ── the wired tools ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    vault_fs.reset_filesystem_probe_cache()
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)
    vault_fs.reset_filesystem_probe_cache()


@pytest.fixture
def no_index(monkeypatch):
    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def execute(self, statement):
            class Empty:
                def all(self):
                    return []

            return Empty()

        async def commit(self):
            return None

    async def _noop(_session):
        return None

    monkeypatch.setattr(tools, "async_session", FakeSession)
    monkeypatch.setattr(tools, "_enter_keyword_generation", _noop)


def files(root):
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
    )


async def test_create_note_writes_the_safe_path_and_says_so(vault):
    result = await tools.create_note_impl("Ideas/Why now?", "hello")
    assert files(vault) == ["Ideas/Why now.md"]
    assert result.startswith("Created note: Ideas/Why now.md")
    assert 'path_normalized: "Ideas/Why now?.md" -> "Ideas/Why now.md"' in result


async def test_create_note_with_a_portable_name_has_no_notice(vault):
    result = await tools.create_note_impl("Ideas/Fine.md", "hello")
    assert "path_normalized" not in result and "MCP-REFUSAL" not in result
    assert files(vault) == ["Ideas/Fine.md"]


async def test_create_note_reuses_an_existing_folder_spelling(vault):
    (vault / "projects" / "kinkverse").mkdir(parents=True)
    result = await tools.create_note_impl("projects/KINKVERSE/x.md", "hi")
    assert files(vault) == ["projects/kinkverse/x.md"]
    assert "path_normalized" in result and "kinkverse" in result
    assert sorted(p.name for p in (vault / "projects").iterdir()) == ["kinkverse"]


async def test_create_note_refuses_a_case_collision_and_writes_nothing(vault):
    (vault / "Plan.md").write_text("keep", encoding="utf-8")
    result = await tools.create_note_impl("plan.md", "new")
    assert 'MCP-REFUSAL {"code":"path_case_collision"' in result
    assert "Plan.md" in result
    assert files(vault) == ["Plan.md"]
    assert (vault / "Plan.md").read_text(encoding="utf-8") == "keep"


async def test_create_note_never_overwrites_through_a_sanitised_name(vault):
    (vault / "Why now.md").write_text("keep", encoding="utf-8")
    result = await tools.create_note_impl("Why now?.md", "new")
    assert 'MCP-REFUSAL {"code":"unportable_path"' in result
    assert (vault / "Why now.md").read_text(encoding="utf-8") == "keep"
    assert files(vault) == ["Why now.md"]


async def test_create_note_leaves_historical_names_alone(vault):
    (vault / "old?dir").mkdir()
    (vault / "old?dir" / "a?.md").write_text("old", encoding="utf-8")
    result = await tools.create_note_impl("old?dir/a?.md", "new")
    # Same exact existing name: the tool's own exists logic answers, unchanged.
    assert "already exists" in result and "path_normalized" not in result
    assert (vault / "old?dir" / "a?.md").read_text(encoding="utf-8") == "old"
    result = await tools.create_note_impl("old?dir/b?.md", "new")
    assert 'path_normalized: "old?dir/b?.md" -> "old?dir/b.md"' in result
    assert (vault / "old?dir" / "b.md").exists()


async def test_create_note_refuses_an_unusable_name_without_writing(vault):
    result = await tools.create_note_impl("???", "x")
    assert 'MCP-REFUSAL {"code":"unportable_path"' in result
    assert files(vault) == []


async def test_write_file_normalises_and_reports(vault):
    result = await tools.write_file_impl("Attach/scan: 1.txt", "hi", encoding="text")
    assert files(vault) == ["Attach/scan - 1.txt"]
    assert result.startswith("Wrote 2 bytes to Attach/scan - 1.txt")
    assert 'path_normalized: "Attach/scan: 1.txt" -> "Attach/scan - 1.txt"' in result


async def test_write_file_overwrite_cannot_be_redirected_onto_another_file(vault):
    (vault / "a.txt").write_text("keep", encoding="utf-8")
    result = await tools.write_file_impl("a?.txt", "new", encoding="text", overwrite=True)
    assert 'MCP-REFUSAL {"code":"unportable_path"' in result
    assert (vault / "a.txt").read_text(encoding="utf-8") == "keep"


async def _seed(vault, rel, data=b"keep"):
    (vault / rel).parent.mkdir(parents=True, exist_ok=True)
    (vault / rel).write_bytes(data)


async def test_write_file_overwrite_never_lands_on_a_file_through_a_reused_folder(vault):
    await _seed(vault, "projects/logo.png")
    result = await tools.write_file_impl(
        "Projects/logo.png", "new", encoding="text", overwrite=True
    )
    assert 'MCP-REFUSAL {"code":"path_case_collision"' in result
    assert "projects/logo.png" in result and "path_normalized:" in result
    assert (vault / "projects" / "logo.png").read_bytes() == b"keep"
    assert files(vault) == ["projects/logo.png"]


async def test_write_file_overwrite_never_lands_on_a_file_through_a_renamed_folder(vault):
    await _seed(vault, "x/a.png")
    result = await tools.write_file_impl("x?/a.png", "new", encoding="text", overwrite=True)
    assert "MCP-REFUSAL" in result and "path_normalized:" in result
    assert (vault / "x" / "a.png").read_bytes() == b"keep"


async def test_create_note_never_lands_on_a_note_through_a_reused_folder(vault):
    await _seed(vault, "projects/idea.md")
    result = await tools.create_note_impl("Projects/idea", "new")
    assert 'MCP-REFUSAL {"code":"path_case_collision"' in result
    assert (vault / "projects" / "idea.md").read_bytes() == b"keep"


async def test_import_from_url_overwrite_never_lands_through_a_reused_folder(vault, origin, monkeypatch):
    await _seed(vault, "projects/logo.png")

    async def never(*a, **k):
        raise AssertionError("fetched for a refused destination")

    monkeypatch.setattr(transfer, "fetch_url_guarded", never)
    result = await tools.import_from_url_impl(
        "https://example.com/a", "Projects/logo.png", overwrite=True
    )
    assert "MCP-REFUSAL" in result and "path_normalized:" in result
    assert (vault / "projects" / "logo.png").read_bytes() == b"keep"


async def test_request_upload_overwrite_never_lands_through_a_reused_folder(vault, mint):
    await _seed(vault, "projects/logo.png")
    result = await tools.request_upload_impl("Projects/logo.png", overwrite=True)
    assert "MCP-REFUSAL" in result and "path_normalized:" in result
    assert "rel" not in mint


async def test_a_refusal_after_normalization_carries_the_explanation(vault, no_index):
    result = await tools.move_note_impl("missing.md", "Archive/Done: it.md")
    assert "Source note not found" in result and "MCP-REFUSAL" in result
    assert 'path_normalized: "Archive/Done: it.md" -> "Archive/Done - it.md"' in result
    assert result.rstrip().splitlines()[-1].startswith("MCP-REFUSAL")


async def test_write_file_overwrite_of_an_exact_existing_name_still_works(vault):
    (vault / "odd?.txt").write_text("old", encoding="utf-8")
    result = await tools.write_file_impl("odd?.txt", "new", encoding="text", overwrite=True)
    assert result.startswith("Wrote 3 bytes to odd?.txt") and "path_normalized" not in result
    assert (vault / "odd?.txt").read_text(encoding="utf-8") == "new"


async def test_write_file_renames_a_windows_device_name(vault):
    result = await tools.write_file_impl("CON.txt", "x", encoding="text")
    assert files(vault) == ["CON-.txt"] and "CON-.txt" in result


async def test_move_note_normalises_only_the_destination(vault, no_index):
    (vault / "src?.md").write_text("body", encoding="utf-8")
    result = await tools.move_note_impl("src?.md", "Archive/Done: it?.md")
    assert (vault / "Archive" / "Done - it.md").read_text(encoding="utf-8") == "body"
    assert not (vault / "src?.md").exists()
    assert "Moved src?.md → Archive/Done - it.md" in result
    assert 'path_normalized: "Archive/Done: it?.md" -> "Archive/Done - it.md"' in result


async def test_move_note_refuses_a_case_collision_and_keeps_both(vault, no_index):
    (vault / "a.md").write_text("A", encoding="utf-8")
    (vault / "Target.md").write_text("T", encoding="utf-8")
    result = await tools.move_note_impl("a.md", "target.md")
    assert 'MCP-REFUSAL {"code":"path_case_collision"' in result
    assert (vault / "a.md").read_text(encoding="utf-8") == "A"
    assert (vault / "Target.md").read_text(encoding="utf-8") == "T"


async def test_move_note_into_an_existing_folder_by_another_case_reuses_it(vault, no_index):
    (vault / "projects").mkdir()
    (vault / "n.md").write_text("n", encoding="utf-8")
    result = await tools.move_note_impl("n.md", "PROJECTS/n.md")
    assert (vault / "projects" / "n.md").exists()
    assert "path_normalized" in result


async def test_move_note_onto_its_own_source_through_a_reused_folder_is_explained(vault, no_index):
    await _seed(vault, "Notes/foo.md", b"foo")
    result = await tools.move_note_impl("Notes/foo.md", "notes/foo.md")
    assert 'MCP-REFUSAL {"code":"path_case_collision"' in result
    assert "source" in result and "path_normalized:" in result
    assert (vault / "Notes" / "foo.md").read_bytes() == b"foo"


async def test_move_note_may_still_rename_a_note_by_case_only(vault, no_index):
    (vault / "Plan.md").write_text("p", encoding="utf-8")
    result = await tools.move_note_impl("Plan.md", "plan.md")
    assert "MCP-REFUSAL" not in result, result
    assert files(vault) == ["plan.md"]


async def test_move_note_does_not_touch_the_source_name(vault, no_index):
    (vault / "bad?.md").write_text("b", encoding="utf-8")
    result = await tools.move_note_impl("bad?.md", "good.md")
    assert "path_normalized" not in result
    assert files(vault) == ["good.md"]


@pytest.fixture
def origin(monkeypatch):
    monkeypatch.setattr(tools.settings, "mcp_hostname", "vault.example.com")
    monkeypatch.setattr(tools.settings, "base_url", "https://vault.example.com")
    monkeypatch.setattr(tools.settings, "_public_origin_explicit", True)


@pytest.fixture
def mint(origin, monkeypatch):
    seen = {}

    async def fake_mint(session, kind, rel, **kwargs):
        seen["rel"] = rel
        row = SimpleNamespace(
            public_id="up_0000000000000000000", expires_at=__import__("datetime").datetime(
                2030, 1, 1, tzinfo=__import__("datetime").timezone.utc
            ),
        )
        return "tok", row, None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(transfer, "mint_token", fake_mint)
    monkeypatch.setattr(tools, "async_session", Session)
    return seen


async def test_request_upload_mints_for_the_safe_path_and_says_so(vault, mint):
    result = await tools.request_upload_impl("Inbox/scan?.pdf")
    assert mint["rel"] == "Inbox/scan.pdf"
    assert "Upload link for `Inbox/scan.pdf`" in result
    assert 'path_normalized: "Inbox/scan?.pdf" -> "Inbox/scan.pdf"' in result


async def test_request_upload_refuses_a_collision_and_mints_nothing(vault, mint):
    (vault / "Scan.pdf").write_bytes(b"x")
    result = await tools.request_upload_impl("scan.pdf")
    assert 'MCP-REFUSAL {"code":"path_case_collision"' in result
    assert "rel" not in mint


@pytest.fixture
def fetch(monkeypatch):
    @asynccontextmanager
    async def fake_fetch(url, **kwargs):
        async def body():
            yield b"payload"

        yield transfer.FetchResult(
            chunks=body(), final_url="https://cdn.example.com/a", content_type="text/plain"
        )

    monkeypatch.setattr(transfer, "fetch_url_guarded", fake_fetch)


async def test_import_from_url_writes_the_safe_path_and_says_so(vault, origin, fetch, monkeypatch):
    @asynccontextmanager
    async def gate_ok(*a, **k):
        async def complete(result, published=None):
            return None

        yield SimpleNamespace(ok=True, complete=complete)

    monkeypatch.setattr(transfer, "lock_identity_for_publish", lambda *a, **k: gate_ok())

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(tools, "async_session", Session)
    result = await tools.import_from_url_impl("https://example.com/a", "Web/page: one.txt")
    assert files(vault) == ["Web/page - one.txt"]
    assert result.startswith("Imported 7 bytes to Web/page - one.txt")
    assert 'path_normalized: "Web/page: one.txt" -> "Web/page - one.txt"' in result


async def test_import_from_url_refuses_a_collision_before_fetching(vault, origin, monkeypatch):
    (vault / "Page.txt").write_text("keep", encoding="utf-8")

    async def never(*a, **k):
        raise AssertionError("fetched for a refused destination")

    monkeypatch.setattr(transfer, "fetch_url_guarded", never)
    result = await tools.import_from_url_impl("https://example.com/a", "page.txt")
    assert 'MCP-REFUSAL {"code":"path_case_collision"' in result
    assert (vault / "Page.txt").read_text(encoding="utf-8") == "keep"


async def test_reads_and_edits_never_normalise(vault):
    (vault / "odd?.md").write_text("hello", encoding="utf-8")
    # Reading a historical name keeps working.
    assert "hello" in str(await tools.read_file_impl("odd?.md"))
