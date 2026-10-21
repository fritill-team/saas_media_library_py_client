"""The committed stubs are what `proto/` generates today.

Generated code is committed (tools/generate.py), so nothing else forces it to
agree with the `.proto` beside it: a stale stub is a contract both sides pin
while neither side's behaviour moves.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "src" / "pkg_media" / "generated"


@pytest.fixture(scope="module")
def freshly_generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip(
        "grpc_tools",
        reason="grpcio-tools is the `codegen` group: sync with --all-groups or this proves nothing",
    )
    tmp_root = tmp_path_factory.mktemp("regen")
    # A copy: the generator deletes and rewrites the generated tree.
    shutil.copytree(ROOT / "proto", tmp_root / "proto")
    shutil.copytree(ROOT / "tools", tmp_root / "tools")
    (tmp_root / "src" / "pkg_media").mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, str(tmp_root / "tools" / "generate.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"generate.py failed:\n{result.stdout}\n{result.stderr}"
    return tmp_root / "src" / "pkg_media" / "generated"


def _files(root: Path) -> dict[Path, bytes]:
    # __pycache__ is left by importing the stubs; a fresh tree never has it.
    return {
        path.relative_to(root): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def test_the_committed_stubs_match_a_fresh_generation(freshly_generated: Path) -> None:
    committed, fresh = _files(GENERATED), _files(freshly_generated)
    missing = sorted(str(n) for n in fresh.keys() - committed.keys())
    extra = sorted(str(n) for n in committed.keys() - fresh.keys())
    differing = sorted(str(n) for n in fresh.keys() & committed.keys() if fresh[n] != committed[n])
    assert not (missing or extra or differing), (
        "the committed stubs are not what proto/ generates: run "
        "`uv run --group codegen python tools/generate.py` and commit the result.\n"
        f"  not generated any more: {extra}\n  not committed: {missing}\n  changed: {differing}"
    )


def test_the_generator_has_something_to_compare() -> None:
    # Without this, an empty proto/ would compare two empty trees and pass.
    assert (ROOT / "tools" / "generate.py").is_file()
    assert list((ROOT / "proto").rglob("*.proto")), "proto/ holds no .proto files"
    assert list(GENERATED.rglob("*_pb2.py")), "no generated stubs are committed"
