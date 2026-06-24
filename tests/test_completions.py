"""Guards for the generated shell completions (v1-08 Phase 7).

The completion files under scripts/completions/ are generated from the argparse
parser by scripts/gen-completions.py. These tests load that generator and assert
(a) the committed files are not stale and (b) the completions actually cover the
parser's subcommands - so the "can't drift from the parser" promise is enforced,
not just claimed.
"""

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_GEN = _REPO / "scripts" / "gen-completions.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_completions", _GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen():
    return _load_generator()


def test_committed_completions_match_generator(gen):
    """The files on disk must be exactly what the generator produces today."""
    rendered = gen.generate()
    out_dir = _REPO / "scripts" / "completions"
    for shell, filename in gen.FILENAMES.items():
        committed = (out_dir / filename).read_text()
        assert committed == rendered[shell], (
            f"{filename} is stale; run `python3 scripts/gen-completions.py`"
        )


def test_all_subcommands_appear_in_every_shell(gen):
    """Every top-level subcommand must be completable in zsh, bash, and fish."""
    rendered = gen.generate()
    from owa_piggy.cli import _build_parser

    model = gen.collect(_build_parser())
    names = [c["name"] for c in model["commands"]]
    assert "token" in names and "profiles" in names  # sanity: parser was read
    for shell, text in rendered.items():
        for name in names:
            assert name in text, f"{name!r} missing from {shell} completion"


def test_profiles_subcommands_are_completed(gen):
    """The nested `profiles` subcommands (list/new/delete/...) must surface."""
    rendered = gen.generate()
    from owa_piggy.cli import _build_parser

    model = gen.collect(_build_parser())
    profiles = next(c for c in model["commands"] if c["name"] == "profiles")
    sub_names = [s["name"] for s in profiles["subcommands"]]
    assert "list" in sub_names and "delete" in sub_names  # sanity
    for shell, text in rendered.items():
        for sub in sub_names:
            assert sub in text, f"profiles {sub!r} missing from {shell} completion"
