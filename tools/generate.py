"""Regenerate the committed stubs from ``proto/``.

Run from the repository root:

    uv run --group codegen python tools/generate.py            # stubs + descriptor set
    uv run --group codegen python tools/generate.py --baseline # also move the baseline

``grpcio-tools`` is a codegen dependency, never a runtime one: the stubs are
generated here and committed, never by a consumer or during the wheel build.
``tests/certify/test_stubs_are_current.py`` fails when they are stale.

Two fixes are applied to protoc's output:

1. Imports of other generated modules are re-rooted under ``pkg_media.generated``.
   protoc writes ``from fritill.media.v1 import assets_pb2``, which resolves
   only when the generated tree is on ``sys.path`` (true in this checkout,
   false in every consumer).
2. An ``__init__.py`` is written in every generated directory, so the stubs
   never rely on namespace packages (which would merge with any other
   distribution shipping a top-level ``fritill``).

Plain ``generate.py`` never touches ``proto/baseline.binpb``; only
``--baseline`` does (see tools/check_compat.py).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = ROOT / "proto"
OUT_DIR = ROOT / "src" / "pkg_media" / "generated"
DESCRIPTOR = OUT_DIR / "descriptor_set.binpb"
BASELINE = PROTO_DIR / "baseline.binpb"

_GENERATED_ROOT = "pkg_media.generated"

# `from fritill.media.v1 import assets_pb2 as x` (package with several segments).
_FROM_IMPORT = re.compile(r"^from (fritill[\w.]*) import (\w+_pb2) as (\w+)$", re.MULTILINE)
# `import fritill_pb2 as x` (a proto at the root package).
_PLAIN_IMPORT = re.compile(r"^import (fritill[\w.]*_pb2) as (\w+)$", re.MULTILINE)
# mypy-protobuf's `.pyi` spelling: `import fritill.media.v1.assets_pb2`.
_DOTTED_IMPORT = re.compile(r"^import (fritill\.[\w.]*_pb2)$", re.MULTILINE)

# The mypy-protobuf plugins, found beside the running interpreter so the script
# works however it is started (the stubs-current test spawns it directly).
_PLUGINS = ("mypy", "mypy_grpc")

# Every generated file the import rewrite must reach.
_REWRITABLE = ("*_pb2.py", "*_pb2.pyi", "*_pb2_grpc.py", "*_pb2_grpc.pyi")


def _plugin_flags() -> list[str]:
    bin_dir = Path(sys.executable).parent
    flags = []
    for name in _PLUGINS:
        executable = bin_dir / f"protoc-gen-{name}"
        if executable.exists():
            flags.append(f"--plugin=protoc-gen-{name}={executable}")
    return flags


def proto_files() -> list[Path]:
    """Every `.proto` under `proto/`, sorted so the descriptor set is byte-stable."""
    return sorted(PROTO_DIR.rglob("*.proto"))


def generate() -> None:
    from grpc_tools import protoc  # codegen-only dependency

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    sources = proto_files()
    if not sources:
        raise SystemExit("no .proto files under proto/")

    well_known = Path(protoc.__file__).parent / "_proto"
    argv = [
        "protoc",
        f"--proto_path={PROTO_DIR}",
        f"--proto_path={well_known}",
        *_plugin_flags(),
        f"--python_out={OUT_DIR}",
        f"--grpc_python_out={OUT_DIR}",
        # mypy-protobuf rather than protoc's --pyi_out: it also types the
        # `_pb2_grpc` stubs and servicers, which --pyi_out leaves untyped.
        f"--mypy_out=quiet:{OUT_DIR}",
        f"--mypy_grpc_out=quiet:{OUT_DIR}",
        # Read by tools/check_compat.py. No --include_source_info: comments and
        # line numbers would make every reformat look like a wire change.
        f"--descriptor_set_out={DESCRIPTOR}",
        "--include_imports",
        *(str(path.relative_to(PROTO_DIR)) for path in sources),
    ]
    code = protoc.main(argv)
    if code != 0:
        raise SystemExit(f"protoc failed with exit code {code}")

    _rewrite_imports()
    _write_package_inits()
    print(f"generated {len(sources)} proto file(s) into {OUT_DIR.relative_to(ROOT)}")


def rewrite_imports(text: str) -> str:
    """Re-root every `fritill.…` stub import under `pkg_media.generated`."""
    text = _FROM_IMPORT.sub(
        lambda m: f"from {_GENERATED_ROOT}.{m.group(1)} import {m.group(2)} as {m.group(3)}",
        text,
    )
    text = _PLAIN_IMPORT.sub(
        lambda m: f"from {_GENERATED_ROOT} import {m.group(1)} as {m.group(2)}",
        text,
    )
    return _DOTTED_IMPORT.sub(lambda m: f"import {_GENERATED_ROOT}.{m.group(1)}", text)


def _rewrite_imports() -> None:
    for pattern in _REWRITABLE:
        for stub in OUT_DIR.rglob(pattern):
            text = stub.read_text()
            rewritten = rewrite_imports(text)
            if rewritten != text:
                stub.write_text(rewritten)


def _write_package_inits() -> None:
    for directory in [OUT_DIR, *(p for p in OUT_DIR.rglob("*") if p.is_dir())]:
        init = directory / "__init__.py"
        if not init.exists():
            init.write_text('"""Generated by tools/generate.py from proto/. Do not edit by hand."""\n')


def refresh_baseline() -> None:
    """Move `proto/baseline.binpb` forward to the current descriptor set.

    A deliberate act, separate from generating: moving the baseline is what
    makes a breaking change stop being reported. The release workflow does it
    after a green check; by hand only for a signed-off major.
    """
    shutil.copyfile(DESCRIPTOR, BASELINE)
    print(f"baseline moved forward to {BASELINE.relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", action="store_true", help="also move proto/baseline.binpb forward")
    args = parser.parse_args(argv)
    generate()
    if args.baseline:
        refresh_baseline()
    return 0


if __name__ == "__main__":
    sys.exit(main())
