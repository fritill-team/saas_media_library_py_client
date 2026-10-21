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


def test_the_version_lives_in_one_place() -> None:
    """release.yml rewrites __version__; a literal version beside it could disagree."""
    assert "version" not in PYPROJECT["project"]
    assert PYPROJECT["project"]["dynamic"] == ["version"]
    assert PYPROJECT["tool"]["hatch"]["version"]["path"] == "src/pkg_media/__init__.py"


def test_the_documented_install_line_names_real_extras() -> None:
    """`pkg-media[grpc,fastapi] @ git+https://…@vX.Y.Z` (README, RELEASING, Discord)."""
    extras = PYPROJECT["project"]["optional-dependencies"]
    assert {"grpc", "fastapi"} <= set(extras)
    assert "Private :: Do Not Upload" in PYPROJECT["project"]["classifiers"]


def test_the_base_install_is_light() -> None:
    base = " ".join(PYPROJECT["project"]["dependencies"])
    for heavy in ("grpc", "protobuf", "googleapis", "fastapi"):
        assert heavy not in base, f"{heavy} belongs in an extra, not the base install"


def test_the_http_client_imports_without_the_grpc_extra() -> None:
    """With grpc/protobuf absent, `import pkg_media` and the HTTP client work and
    `transport="grpc"` names the extra."""
    code = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"grpc", "grpc_status"} or name.startswith(("google.protobuf", "google.rpc")):
            raise ImportError(f"blocked {name}")
sys.meta_path.insert(0, Block())
import pkg_media
from pkg_media import MediaClient, AccessGate, UploadProfile, methods
MediaClient(base_url="https://media.test", token="t").close()
try:
    MediaClient(transport="grpc", target="x:1", token="t")
except ImportError as exc:
    assert "pkg-media[grpc]" in str(exc), exc
else:
    raise AssertionError("grpc transport without the extra")
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
