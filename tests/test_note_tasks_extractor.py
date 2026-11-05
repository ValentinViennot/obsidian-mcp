"""The task extractor's grammar (#350, design D2/D3, `note-tasks` spec)."""
import datetime
import time

import pytest

from src.config import MAX_TASKS_PER_NOTE, TASK_TEXT_MAX_CHARS
from src.services import tasks as task_mod
from src.services.tasks import (
    TASKS_SINCE_EXTRACTION_VERSION,
    body_line_offset,
    extract_tasks_bounded,
)


def extract(body, offset=0, **kw):
    return extract_tasks_bounded(body, line_offset=offset, **kw)


def only(body, offset=0):
    tasks, truncated = extract(body, offset)
    assert not truncated
    assert len(tasks) == 1, tasks
    return tasks[0]


# ── recognition ─────────────────────────────────────────────────────────────


def test_open_and_done_checkboxes_are_tasks():
    body = "intro\n\n- [ ] write report\n* [x] send invoice\n"
    tasks, truncated = extract(body)
    assert not truncated
    assert [(t.line, t.status_char, t.text) for t in tasks] == [
        (3, " ", "write report"),
        (4, "x", "send invoice"),
    ]


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_a_checkbox_in_a_fenced_block_is_not_a_task(fence):
    body = f"{fence}\n- [ ] not a task\n{fence}\n- [ ] real\n"
    tasks, _ = extract(body)
    assert [t.text for t in tasks] == ["real"]
    assert tasks[0].line == 4


def test_a_checkbox_decided_by_inline_code_masking_is_not_a_task():
    assert extract("`- [ ]` example syntax\n")[0] == []
    # A marker character that masking changed is not a task either.
    assert extract("- [`] x`\n")[0] == []


def test_inline_code_after_the_marker_does_not_unmake_the_task():
    t = only("- [ ] run `make test` now")
    assert t.text == "run `make test` now"


def test_indented_ordered_and_callout_tasks_are_tasks():
    body = "    - [ ] subtask\n\t- [ ] tabbed\n1. [ ] numbered\n> - [ ] in a callout\n"
    tasks, _ = extract(body)
    assert [t.text for t in tasks] == ["subtask", "tabbed", "numbered", "in a callout"]
    assert extract("2) [ ] paren\n> > + [ ] nested quote\n")[0][1].text == "nested quote"


@pytest.mark.parametrize(
    "line",
    ["- [ ]no space", "[ ] no bullet", "- [xx] two chars", "- [\t] tab marker",
     "-[ ] no gap", "- [ ] nbsp marker", "- [[] bracket", "- []] bracket",
     "1234567890. [ ] ten digits"],
)
def test_shapes_that_are_not_checkboxes(line):
    assert extract(line + "\n")[0] == []


def test_a_checkbox_at_end_of_line_with_no_text():
    t = only("- [ ]")
    assert t.text == "" and t.state == "open"


def test_a_tasks_plugin_query_block_is_not_evaluated():
    body = "```tasks\nnot done\n- [ ] inside the query\n```\n\n[[Other]] and [[Tasks/List]]\n"
    assert extract(body)[0] == []


# ── line numbers ────────────────────────────────────────────────────────────


def test_line_numbers_count_the_frontmatter():
    # A 4-line block (delimiters included) and a task on the body's 2nd line.
    assert body_line_offset("---\na: 1\nb: 2\n---\n") == 4
    t = only("first\n- [ ] second", offset=4)
    assert t.line == 6


@pytest.mark.parametrize("nl", ["\r\n", "\r", "\n"])
def test_crlf_and_cr_line_endings(nl):
    body = nl.join(["# H", "x", "- [ ] a", "- [x] b", ""])
    tasks, _ = extract(body)
    assert [(t.line, t.text, t.heading_path) for t in tasks] == [
        (3, "a", "H"), (4, "b", "H"),
    ]
    assert body_line_offset(f"---{nl}a: 1{nl}---{nl}") == 3


# ── status mapping ──────────────────────────────────────────────────────────


def test_status_mapping():
    body = "- [ ] a\n- [x] b\n- [X] c\n- [-] d\n- [/] e\n- [>] f\n"
    tasks, _ = extract(body)
    assert [(t.status_char, t.state) for t in tasks] == [
        (" ", "open"), ("x", "done"), ("X", "done"), ("-", "cancelled"),
        ("/", "open"), (">", "open"),
    ]


# ── fields ──────────────────────────────────────────────────────────────────


def test_dates_priority_and_tags_are_parsed():
    t = only("- [ ] ship 📅 2026-11-01 ⏳ 2026-10-30 🛫 2026-10-28 ⏫ #work #alpha")
    assert t.due == datetime.date(2026, 11, 1)
    assert t.scheduled == datetime.date(2026, 10, 30)
    assert t.start == datetime.date(2026, 10, 28)
    assert t.priority == "high"
    assert t.tags == ["work", "alpha"]
    assert t.tags_cut is False
    assert t.text == "ship 📅 2026-11-01 ⏳ 2026-10-30 🛫 2026-10-28 ⏫ #work #alpha"


def test_done_date():
    t = only("- [x] shipped ✅ 2026-10-01")
    assert t.done_on == datetime.date(2026, 10, 1)


@pytest.mark.parametrize("bad", ["2026-02-30", "0000-01-01", "2026-13-01"])
def test_an_invalid_date_stores_null_and_keeps_the_text(bad):
    t = only(f"- [ ] pay 📅 {bad}")
    assert t.due is None
    assert t.text == f"pay 📅 {bad}"


def test_a_signifier_with_no_date_stores_null():
    assert only("- [ ] pay 📅 soon").due is None
    assert only("- [ ] pay 📅 2026-01-011").due is None  # followed by a digit


def test_a_signifier_inside_inline_code_is_not_a_field():
    t = only("- [ ] document the `📅 2026-01-01` syntax")
    assert t.due is None
    t = only("- [ ] the `#tag` and `🔺` syntax")
    assert t.tags is None and t.priority is None


def test_the_last_occurrence_wins():
    assert only("- [ ] move 📅 2026-01-01 then 📅 2026-02-01").due == datetime.date(2026, 2, 1)
    assert only("- [ ] p 🔺 then 🔽").priority == "low"
    # The last signifier decides, even when it carries no valid date.
    assert only("- [ ] d 📅 2026-01-01 📅 2026-02-30").due is None


def test_fe0f_and_variant_signifiers():
    assert only("- [ ] a 📅️ 2026-03-01").due == datetime.date(2026, 3, 1)
    assert only("- [ ] a 📆 2026-03-02").due == datetime.date(2026, 3, 2)
    assert only("- [ ] a 🗓️ 2026-03-03").due == datetime.date(2026, 3, 3)
    assert only("- [ ] a 🗓2026-03-04").due == datetime.date(2026, 3, 4)
    assert only("- [ ] a ⌛ 2026-03-05").scheduled == datetime.date(2026, 3, 5)
    assert only("- [ ] a ⏳️2026-03-06").scheduled == datetime.date(2026, 3, 6)


@pytest.mark.parametrize(
    "emoji,name",
    [("🔺", "highest"), ("⏫", "high"), ("🔼", "medium"), ("🔽", "low"), ("⏬", "lowest")],
)
def test_priorities(emoji, name):
    assert only(f"- [ ] p {emoji}️").priority == name
    assert only(f"- [ ] p {emoji}").priority == name


def test_no_priority_is_null():
    assert only("- [ ] plain").priority is None


# ── tags ────────────────────────────────────────────────────────────────────


def test_the_tag_cap_keeps_the_first_64_distinct_and_says_so():
    tags = " ".join(f"#t{i:02d}" for i in range(1, 66))
    t = only(f"- [ ] many {tags} #t01")
    assert t.tags == [f"t{i:02d}" for i in range(1, 65)]
    assert t.tags_cut is True
    assert "t65" not in t.tags


def test_an_over_long_tag_is_not_stored_and_is_flagged():
    t = only("- [ ] x #ok #" + "a" * 129)
    assert t.tags == ["ok"]
    assert t.tags_cut is True
    t = only("- [ ] x #" + "a" * 128)
    assert t.tags == ["a" * 128] and t.tags_cut is False


def test_dedup_keeps_the_first_occurrence():
    t = only("- [ ] x #b #a #b #c #a")
    assert t.tags == ["b", "a", "c"]
    assert t.tags_cut is False


# ── heading path, text, cap ─────────────────────────────────────────────────


def test_heading_path():
    body = "# Sprint\n## Backend\n### API\n## Frontend\n- [ ] task\n"
    assert only(body).heading_path == "Sprint > Frontend"


def test_no_heading_is_null_and_headings_in_code_are_ignored():
    assert only("- [ ] top").heading_path is None
    body = "```\n# not a heading\n```\n- [ ] t\n"
    assert only(body).heading_path is None
    assert only("  # indented is not a heading\n- [ ] t").heading_path is None


def test_heading_path_caps():
    body = "# " + "A" * 300 + "\n## " + "B" * 300 + "\n### " + "C" * 300 + "\n- [ ] t\n"
    path = only(body).heading_path
    assert path.startswith("A" * 200 + " > " + "B" * 200 + " > ")
    assert len(path) == 512


def test_text_cut():
    t = only("- [ ] " + "é" * (TASK_TEXT_MAX_CHARS + 5) + "   ")
    assert t.text == "é" * TASK_TEXT_MAX_CHARS
    assert t.text_cut is True
    t = only("- [ ] " + "é" * TASK_TEXT_MAX_CHARS + "   ")
    assert t.text_cut is False and len(t.text) == TASK_TEXT_MAX_CHARS


def test_the_per_note_cap_returns_truncated():
    body = "\n".join("- [ ] t%d" % i for i in range(MAX_TASKS_PER_NOTE + 1))
    tasks, truncated = extract(body)
    assert truncated is True
    assert len(tasks) == MAX_TASKS_PER_NOTE
    assert tasks[-1].text == "t%d" % (MAX_TASKS_PER_NOTE - 1)
    tasks, truncated = extract("\n".join("- [ ] t" for _ in range(MAX_TASKS_PER_NOTE)))
    assert truncated is False and len(tasks) == MAX_TASKS_PER_NOTE


def test_a_nul_is_refused_not_repaired():
    with pytest.raises(ValueError):
        extract("- [ ] a\x00b")


def test_the_backfill_constant():
    assert TASKS_SINCE_EXTRACTION_VERSION == 5


def test_the_shared_rules_are_imported_not_copied():
    from src.services import vault

    assert task_mod._ATX_HEADING_RE is vault._ATX_HEADING_RE
    assert task_mod.INLINE_TAG_RE is vault.INLINE_TAG_RE


# ── linear time ─────────────────────────────────────────────────────────────

TEN_MIB = 10 * 1024 * 1024
TIME_BOUND_SECONDS = 30.0


@pytest.mark.parametrize(
    "fragment",
    ["> > - [", "- [ ] ", "- [ ] x 📅 " + "1" * 64 + " ", "- [ ] \n", "> > - [\n",
     "1" * 9 + ". [", "#a" * 50 + "\n"],
)
def test_the_extractor_is_linear_on_adversarial_input(fragment):
    body = (fragment * (TEN_MIB // len(fragment) + 1))[:TEN_MIB]
    started = time.monotonic()
    tasks, truncated = extract(body)
    elapsed = time.monotonic() - started
    assert elapsed < TIME_BOUND_SECONDS, f"{elapsed:.1f}s on {fragment!r}"
    assert len(tasks) <= MAX_TASKS_PER_NOTE
