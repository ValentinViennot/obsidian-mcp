"""The connector icon, and why it is served from the origin root.

A custom MCP connector is identified in a client's UI by whatever icon it can
find at the server's origin. Every connector in this fleet rendered with the
same placeholder for one reason: all of them answered 404 at /favicon.ico, so
there was nothing to render but a default, and three different services became
visually indistinguishable in a list.

That matters more than decoration. The icon is how you tell, at a glance, which
system an agent is about to touch.
"""

from __future__ import annotations

from pathlib import Path

BRAND = Path(__file__).resolve().parent.parent / "src" / "control_panel" / "static" / "brand"

#: A connector icon is never rendered large. These ceilings are deliberate: a
#: 2.8 MB source shrank to ~5 KB with no visible loss at 128px, and an icon
#: that loads slowly is worse than no icon at all.
MAX_ICO_BYTES = 40 * 1024
MAX_PNG_BYTES = 20 * 1024


def test_the_brand_assets_exist():
    assert (BRAND / "favicon.ico").is_file(), "no favicon: the connector shows a placeholder"
    assert (BRAND / "icon-128.png").is_file()


def test_the_assets_stay_small():
    """Guards against someone dropping the 2.8 MB original in here."""
    ico = (BRAND / "favicon.ico").stat().st_size
    png = (BRAND / "icon-128.png").stat().st_size
    assert ico <= MAX_ICO_BYTES, f"favicon.ico is {ico} bytes; optimise it"
    assert png <= MAX_PNG_BYTES, f"icon-128.png is {png} bytes; optimise it"


def test_the_favicon_is_a_real_multi_resolution_icon():
    """A single 16px frame renders badly wherever a client asks for more."""
    data = (BRAND / "favicon.ico").read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00", "not an ICO container"
    count = int.from_bytes(data[4:6], "little")
    assert count >= 2, f"only {count} resolution(s) in the icon"


def test_the_png_is_a_png():
    assert (BRAND / "icon-128.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_routes_are_declared_at_the_origin_root():
    """Under /admin/ a client would not find them, and behind auth it could not
    fetch them — an icon is not a secret."""
    main = (Path(__file__).resolve().parent.parent / "src" / "main.py").read_text()
    assert '@app.get("/favicon.ico"' in main
    assert '@app.get("/icon.png"' in main
