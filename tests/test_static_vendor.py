"""The dashboard's pinned third-party files: the vendored d3 and fonts.

``service/static/vendor/`` and ``service/static/fonts/`` hold the only
files in the UI that the project didn't write. Each is pinned by its
sha256 in ``service/static/THIRD_PARTY.sha256``, ships with its licence,
and is never fetched at runtime (the service's CSP allows nothing but
itself). These tests keep that true: the hashes match, nothing unlisted
sneaks in, git leaves the bytes alone, the wheel ships them, and no
first-party code reaches the one part of d3 the CSP would refuse.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "src" / "claude_token_lens" / "service" / "static"
MANIFEST = STATIC_DIR / "THIRD_PARTY.sha256"
PINNED_DIRS = ("vendor", "fonts")

#: Each pinned file's licence, next to it.
LICENCES = {
    "vendor/d3-7.9.0.min.js": "vendor/LICENSE-d3-7.9.0",
    "fonts/InterVariable-4.1.woff2": "fonts/LICENSE-Inter-4.1.txt",
    "fonts/JetBrainsMono-2.304-Regular.woff2": "fonts/OFL-JetBrainsMono-2.304.txt",
    "fonts/JetBrainsMono-2.304-Medium.woff2": "fonts/OFL-JetBrainsMono-2.304.txt",
}

#: d3-dsv builds its row parsers with ``new Function``, which the CSP's
#: ``script-src 'self'`` refuses at runtime. First-party code never
#: touches it, so a chart can't fail only once it meets real data.
_DSV_NAMES = re.compile(r"\b(?:csvParse|csvParseRows|tsvParse|tsvParseRows|dsvFormat)\b|\bd3\.(?:csv|tsv|dsv)\s*\(")


def _manifest() -> dict[str, str]:
    entries = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        entries[name.lstrip("*")] = digest
    return entries


def _pinned_files() -> list[str]:
    return sorted(
        path.relative_to(STATIC_DIR).as_posix()
        for folder in PINNED_DIRS
        for path in (STATIC_DIR / folder).rglob("*")
        if path.is_file()
    )


def test_manifest_lists_exactly_the_files_on_disk() -> None:
    assert sorted(_manifest()) == _pinned_files()


@pytest.mark.parametrize("name", sorted(_manifest()))
def test_pinned_file_matches_its_sha256(name: str) -> None:
    digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()
    assert digest == _manifest()[name], f"{name} changed; re-pin it in THIRD_PARTY.sha256 on purpose"


@pytest.mark.parametrize("name,licence", sorted(LICENCES.items()))
def test_every_pinned_file_ships_with_its_licence(name: str, licence: str) -> None:
    assert name in _manifest()
    assert licence in _manifest()
    assert (STATIC_DIR / licence).stat().st_size > 200


def test_d3_is_the_pinned_release() -> None:
    bundle = (STATIC_DIR / "vendor" / "d3-7.9.0.min.js").read_text(encoding="utf-8")
    assert '"7.9.0"' in bundle


@pytest.mark.parametrize("name", sorted(_manifest()))
def test_every_pinned_name_carries_its_version(name: str) -> None:
    """The service lets the browser keep a pinned file for a year
    (``immutable``), keyed by its URL. A re-pin under the same name
    would leave every browser that had visited on the old bytes, so each
    name says which release it is, and a new release is a new URL."""
    assert re.search(r"-\d+(?:\.\d+)+(?:[-.]|$)", Path(name).name), name


@pytest.mark.parametrize("name", [n for n in sorted(LICENCES) if n.endswith(".woff2")])
def test_fonts_are_woff2(name: str) -> None:
    assert (STATIC_DIR / name).read_bytes()[:4] == b"wOF2"


def test_git_leaves_the_pinned_bytes_alone() -> None:
    """With ``core.autocrlf=true`` (this repo's Windows default and CI's
    windows-latest), git would rewrite the minified bundle's line endings
    and break its hash; the fonts are binary."""
    rules = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^src/claude_token_lens/service/static/vendor/\*\* -text$", rules, re.MULTILINE)
    assert re.search(r"^src/claude_token_lens/service/static/fonts/\*\* binary$", rules, re.MULTILINE)


def test_package_data_ships_the_pinned_folders() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data = config["tool"]["setuptools"]["package-data"]["claude_token_lens"]
    for pattern in ("service/static/*", "service/static/vendor/*", "service/static/fonts/*"):
        assert pattern in data, pattern


def test_first_party_code_never_uses_the_d3_parsers_the_csp_refuses() -> None:
    first_party = [p for p in STATIC_DIR.iterdir() if p.is_file() and p.suffix in (".html", ".js", ".css")]
    assert len(first_party) >= 18
    for path in first_party:
        text = path.read_text(encoding="utf-8")
        assert not _DSV_NAMES.search(text), f"{path.name} calls a d3-dsv parser, which needs new Function"


def test_d3_is_only_reached_through_its_shim() -> None:
    """Chart code imports ``./d3.js``, never the bundle itself, so the
    bundle has one door and one place that knows it's a UMD global."""
    for path in STATIC_DIR.glob("*.js"):
        text = path.read_text(encoding="utf-8")
        if path.name == "d3.js":
            assert 'import "./vendor/d3-7.9.0.min.js";' in text
            assert "export default globalThis.d3;" in text
        else:
            assert "vendor/" not in text, f"{path.name} reaches into vendor/ directly; import ./d3.js"
