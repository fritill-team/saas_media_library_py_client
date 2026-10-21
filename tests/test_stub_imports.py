"""The generated stubs import inside this distribution, never from a
top-level `fritill` package (which no consumer has installed)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "src" / "pkg_media" / "generated"
sys.path.insert(0, str(ROOT / "tools"))

import generate  # noqa: E402


def test_no_generated_file_imports_a_top_level_fritill_package() -> None:
    offenders = [
        f"{stub.relative_to(ROOT)}:{lineno}: {line.strip()}"
        for stub in sorted([*GENERATED.rglob("*.py"), *GENERATED.rglob("*.pyi")])
        for lineno, line in enumerate(stub.read_text().splitlines(), start=1)
        if line.strip().startswith(("import fritill", "from fritill"))
    ]
    assert not offenders, "the import rewrite in tools/generate.py did not fire:\n  " + "\n  ".join(offenders)


def test_the_three_import_spellings_are_rewritten() -> None:
    assert generate.rewrite_imports("from fritill.media.v1 import assets_pb2 as x\n") == (
        "from pkg_media.generated.fritill.media.v1 import assets_pb2 as x\n"
    )
    assert generate.rewrite_imports("import fritill_pb2 as x\n") == (
        "from pkg_media.generated import fritill_pb2 as x\n"
    )
    assert generate.rewrite_imports("import fritill.media.v1.assets_pb2\n") == (
        "import pkg_media.generated.fritill.media.v1.assets_pb2\n"
    )


def test_well_known_and_unrelated_imports_are_left_alone() -> None:
    source = (
        "import grpc\n"
        "from google.protobuf import timestamp_pb2 as google_dot_protobuf_dot_timestamp__pb2\n"
    )
    assert generate.rewrite_imports(source) == source


def test_every_generated_directory_is_a_real_package() -> None:
    missing = [
        str(d.relative_to(ROOT))
        for d in [GENERATED, *(p for p in GENERATED.rglob("*") if p.is_dir())]
        if "__pycache__" not in d.parts and not (d / "__init__.py").is_file()
    ]
    assert not missing, f"generated directories without __init__.py: {missing}"


def test_the_conformance_stubs_import() -> None:
    from pkg_media.generated.fritill.conformance.v1 import ping_pb2, ping_pb2_grpc

    assert ping_pb2.PingRequest(nonce="x").nonce == "x"
    assert hasattr(ping_pb2_grpc, "ConformanceStub")
    assert hasattr(ping_pb2_grpc, "ConformanceServicer")
