"""What reaches a consumer, and what must never.

The stubs are generated here and committed, so no consumer (the media server,
an app backend) carries a protoc toolchain. A codegen tool moved into
`dependencies` reads as one word in a diff and installs a compiler into every
service image.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())

CODEGEN_MARKERS = ("grpcio-tools", "grpc_tools", "grpcio_tools", "protoc", "mypy-protobuf", "mypy_protobuf")


def _offenders(deps: list[str]) -> list[str]:
    return [dep for dep in deps if any(marker in dep for marker in CODEGEN_MARKERS)]


def test_no_codegen_dependency_is_a_runtime_dependency() -> None:
    runtime = list(PYPROJECT["project"]["dependencies"])
    for extra in PYPROJECT["project"].get("optional-dependencies", {}).values():
        runtime.extend(extra)
    assert not _offenders(runtime), f"codegen reaches consumers: {_offenders(runtime)}"


def test_codegen_is_its_own_group_with_the_stack_versions() -> None:
    groups = PYPROJECT["dependency-groups"]
    assert "grpcio-tools==1.84.0" in groups["codegen"]
    assert "mypy-protobuf==5.1.0" in groups["codegen"]
    assert not _offenders(groups.get("dev", [])), "a codegen tool drifted into the `dev` group"


def test_the_distribution_is_pkg_media() -> None:
    assert PYPROJECT["project"]["name"] == "pkg-media"
    assert PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/pkg_media"]


def test_fastapi_is_an_optional_extra() -> None:
    assert not [d for d in PYPROJECT["project"]["dependencies"] if d.startswith("fastapi")]
    assert PYPROJECT["project"]["optional-dependencies"]["fastapi"]


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    out = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out), str(ROOT)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"`build` unavailable in this environment:\n{result.stderr[-800:]}")
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    with zipfile.ZipFile(wheels[0]) as wheel:
        return wheel.namelist()


def test_the_wheel_ships_the_stubs_and_the_descriptor_set(wheel_names: list[str]) -> None:
    assert "pkg_media/generated/descriptor_set.binpb" in wheel_names
    assert "pkg_media/generated/fritill/conformance/v1/ping_pb2.py" in wheel_names


def test_every_generated_module_ships_a_type_stub(wheel_names: list[str]) -> None:
    modules = {n for n in wheel_names if n.endswith(("_pb2.py", "_pb2_grpc.py"))}
    stubs = {n for n in wheel_names if n.endswith(".pyi")}
    assert modules
    assert not {m for m in modules if m + "i" not in stubs}


def test_the_typing_marker_ships(wheel_names: list[str]) -> None:
    assert "pkg_media/py.typed" in wheel_names


def test_the_baseline_and_tools_do_not_ship(wheel_names: list[str]) -> None:
    assert not [n for n in wheel_names if "baseline" in n or n.startswith(("tools/", "proto/"))]
