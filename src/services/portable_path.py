"""Portable names for the *destination* of a write.

The vault is synchronised by Git to Android (shared storage) and to Windows and
macOS machines. Git on Linux accepts names those platforms cannot hold -- `?`,
`:`, `"`, `*`, a trailing dot or space, `CON.md`, and two siblings that differ
only by case (`projects/KINKVERSE` next to `projects/kinkverse`). A write tool
that creates such a name breaks the phone's sync for everyone, so a **new**
name is made portable *before* anything is written, and the caller is told
exactly what happened (the owner's rule: fix automatically, never silently).

This module is pure: no filesystem access of its own. The caller supplies
`list_siblings`, so the tool decides which vault root a listing reads, and a
test can use a dictionary.

The rules port `scripts/check_portable_paths.py` in the vault repository, which
is the reference:

* A component that already exists (byte-for-byte, as listed) is **never**
  changed, whatever it contains. Historical names stay; reads are unaffected.
* Only a new component is normalised: NFC; each of `< > : " \\ | ? * [ ] # ^`
  and the control characters become `-` (a trailing `?` or `"` is dropped);
  a trailing space or dot at the end of the component is stripped; a Windows device name gets a `-`
  suffix; a component over 240 UTF-8 bytes is shortened, keeping the extension.
* A new name that collides with an existing sibling under case/Unicode folding
  reuses a directory's spelling, and refuses for a file. **Nothing is ever
  overwritten and no suffix is invented.**
* Dot-prefixed names, `.` and `..` are passed through unchanged: the server's
  existing path guard already decides what to do with them.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Callable, Iterable

#: One directory entry as `list_siblings` reports it: `(name, is_directory)`.
Entry = tuple[str, bool]

MAX_COMPONENT_BYTES = 240

#: Refusal codes. Both are declared in `src/services/refusals.py`.
UNPORTABLE_PATH = "unportable_path"
PATH_CASE_COLLISION = "path_case_collision"

_INVALID = re.compile(r'[<>:"\\|?*\[\]#^]|[\x00-\x1f\x7f]')
_WINDOWS_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$", re.I)
_SENTINEL = "\x00"
_REPLACED = re.compile(r"[ ]*\x00(?:[ \x00])*")
#: Trailing characters stripped from a stem. `?` and `"` are *dropped* rather
#: than turned into a hyphen, so `Why now?.md` reads `Why now.md`.
_TRAILING = " .?\""


def notice_line(raw: str, safe: str, reasons: list[str]) -> str:
    """The `path_normalized:` line that tells the caller what was changed."""
    # JSON-quoted, so a newline or quote in the caller's path can never start a
    # new line (a forged `MCP-REFUSAL`) or make the field ambiguous.
    def q(text: str) -> str:
        return json.dumps(text, ensure_ascii=False)

    return f'path_normalized: {q(raw)} -> {q(safe)} ({"; ".join(reasons)})'


class PortablePathRefusal(ValueError):
    """The destination cannot be made portable without guessing."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _describe_char(ch: str) -> str:
    return f"U+{ord(ch):04X}" if ord(ch) < 0x20 or ord(ch) == 0x7F else f"'{ch}'"


def _replace_invalid(text: str, found: list[str]) -> str:
    for ch in _INVALID.findall(text):
        if ch not in found:
            found.append(ch)
    marked = _INVALID.sub(_SENTINEL, text)

    def _join(match: re.Match) -> str:
        spaced = " " in match.group(0)
        at_edge = match.start() == 0 or match.end() == len(marked)
        return " - " if spaced and not at_edge else "-"

    return _REPLACED.sub(_join, marked)


def _truncate(name: str, ext: str) -> str:
    """`name + ext` within `MAX_COMPONENT_BYTES`, cut at a character boundary."""
    budget = MAX_COMPONENT_BYTES - len(ext.encode("utf-8"))
    cut = name.encode("utf-8")[: max(budget, 1)].decode("utf-8", "ignore")
    return cut.rstrip(" .") + ext


def portable_component(name: str, *, is_file: bool) -> tuple[str, list[str]]:
    """`name` made portable, with a reason per change. Raises on an empty result.

    Callers apply this only to a component that does not exist yet.
    """
    reasons: list[str] = []
    nfc = unicodedata.normalize("NFC", name)
    if nfc != name:
        reasons.append("normalized to Unicode NFC")

    # Only the very end of a component is a problem on Windows (a trailing dot
    # or space); `?` and `"` are dropped there too instead of hyphenated, so
    # `Why now?` reads `Why now`. Leading spaces, and a space or dot *inside*
    # the name (before the extension included), are legal and left alone.
    end = nfc.rstrip(_TRAILING)
    if end != nfc:
        dropped = nfc[len(end):]
        if "?" in dropped or '"' in dropped:
            reasons.append("trailing '?' or '\"' dropped (not allowed on Android/Windows)")
        if dropped.strip('?"'):
            reasons.append("trailing spaces/dots removed (not allowed on Windows)")
    nfc = end

    stem, ext = nfc, ""
    if is_file:
        dot = nfc.rfind(".")
        if 0 < dot < len(nfc) - 1:
            stem, ext = nfc[:dot], nfc[dot:]
    if ext:
        before = stem.rstrip('?"')
        if before != stem:
            reasons.append("trailing '?' or '\"' dropped (not allowed on Android/Windows)")
            stem = before

    found: list[str] = []
    stem = _replace_invalid(stem, found)
    ext = _replace_invalid(ext, found) if ext else ""
    if found:
        listed = ", ".join(_describe_char(ch) for ch in found)
        reasons.append(f"{listed} not allowed on Android/Windows, replaced with '-'")

    result = stem + ext
    if not stem or result in (".", ".."):
        raise PortablePathRefusal(
            UNPORTABLE_PATH,
            f"{name!r} has nothing left once the characters that Android/Windows "
            "reject are removed. Choose a name with letters or digits. Nothing "
            "was written.",
        )

    prefix = result.split(".", 1)[0]
    if _WINDOWS_RESERVED.match(prefix):
        result = prefix + "-" + result[len(prefix):]
        reasons.append(f"'{prefix}' is a reserved device name on Windows, '-' appended")

    if len(result.encode("utf-8")) > MAX_COMPONENT_BYTES:
        keep_ext = ext if len(ext.encode("utf-8")) < MAX_COMPONENT_BYTES // 2 else ""
        base = result[: len(result) - len(keep_ext)] if keep_ext else result
        result = _truncate(base, keep_ext)
        reasons.append(f"longer than {MAX_COMPONENT_BYTES} bytes, shortened")
        if not result or result.startswith("."):
            raise PortablePathRefusal(
                UNPORTABLE_PATH, f"{name!r} cannot be shortened to a usable name. Nothing was written."
            )
    return result, reasons


def normalize_destination(
    rel_path: str,
    list_siblings: Callable[[str], Iterable[Entry]],
    *,
    ignore: str | None = None,
) -> tuple[str, list[str]]:
    """`(safe_path, reasons)` for the destination `rel_path`.

    `list_siblings(parent)` returns `(name, is_dir)` for the entries of the
    vault-relative directory `parent` (`""` is the root) and an empty iterable
    when it does not exist. It is called only for parents that exist, with the
    spelling the vault really uses.

    When nothing needs fixing the input is returned **unchanged** (same object),
    so the caller's existing behaviour is exactly preserved. `reasons` is empty
    exactly then.

    `ignore` is a vault-relative path that is not a collision candidate: the
    source of a move, so that a case-only rename of a file is not refused for
    colliding with itself.

    Raises `PortablePathRefusal` for a name that cannot be fixed without
    guessing, and for a new file name that collides with an existing sibling.
    """
    raw = str(rel_path)
    if raw.endswith("/") or raw.startswith("/"):
        return rel_path, []  # the existing guard words that refusal
    parts = raw.split("/")
    kept = [p for p in parts if p not in ("", ".")]
    if not kept:
        return rel_path, []

    out: list[str] = []
    reasons: list[str] = []
    parent_exists = True
    parent = ""
    changed = False
    leaf_exists = False
    last = len(kept) - 1

    def explained(message: str, tail: list[str], more: list[str]) -> str:
        """`message` plus the explanation of the path the caller never sent."""
        if not (reasons or more):
            return message
        return f"{message}\n{notice_line(raw, '/'.join(out + tail), reasons + more)}"

    for i, comp in enumerate(kept):
        is_file = i == last
        if comp.startswith("."):
            # `.`/`..` handled above or by the guard; dot-prefixed names are
            # the server's existing hidden-path rule. Not ours to change.
            out.append(comp)
            parent_exists = False
            continue

        entries: dict[str, bool] = {}
        if parent_exists:
            entries = {name: is_dir for name, is_dir in list_siblings(parent)}
        if comp in entries:
            leaf_exists = leaf_exists or is_file
            out.append(comp)
            parent_exists = entries[comp] and not is_file
            parent = f"{parent}/{comp}" if parent else comp
            continue

        safe, why = portable_component(comp, is_file=is_file)
        key = _fold(safe)
        twins = sorted(
            n for n in entries
            if _fold(n) == key and (f"{parent}/{n}" if parent else n) != ignore
        )
        where = f"{parent}/" if parent else ""
        if twins:
            twin = next((n for n in twins if n == safe), twins[0])
            twin_is_dir = entries[twin]
            if not is_file and twin_is_dir:
                if twin != comp:
                    why.append(
                        f"existing folder '{twin}' reused (names that differ only by case "
                        "or Unicode form cannot coexist on Android/Windows/macOS)"
                    )
                safe = twin
            else:
                case_only = _fold(comp) == key
                kind = "folder" if twin_is_dir else "file"
                raise PortablePathRefusal(
                    PATH_CASE_COLLISION if case_only else UNPORTABLE_PATH,
                    explained(
                        (f"{where}{comp!s} would become {where}{safe} on Android/Windows, "
                         if safe != comp else f"{where}{comp!s} is new, ")
                        + f"but a {kind} {where}{twin} already exists there and a "
                        "case-insensitive filesystem cannot hold both. Nothing was "
                        "written; use the existing path (edit_note to change it) or "
                        "choose a different name.",
                        [safe], why,
                    ),
                )
        elif not is_file:
            # A brand-new folder: nothing below it exists either.
            pass
        if safe != comp or why:
            changed = True
            reasons.extend(why)
        out.append(safe)
        parent_exists = bool(twins) and not is_file
        parent = f"{parent}/{safe}" if parent else safe

    if not changed:
        return rel_path, []
    safe_path = "/".join(out)
    if leaf_exists:
        # A folder was renamed or reused on the way, and the leaf is already
        # there: a tool's `overwrite=True` would replace a file the caller never
        # named. Refused whatever `overwrite` says.
        if safe_path == ignore:
            message = (
                f"{raw} normalizes to {safe_path}, which is the source of this "
                "move: it would not move anywhere. Nothing was written."
            )
        else:
            message = (
                f"{raw} would land on {safe_path} on Android/Windows, but that "
                "file already exists and you did not name it. Nothing was "
                "written (overwrite does not apply to a path you did not name); "
                "name the existing path explicitly to replace it, or choose a different name."
            )
        raise PortablePathRefusal(
            PATH_CASE_COLLISION, explained(message, [], [])
        )
    return safe_path, reasons
