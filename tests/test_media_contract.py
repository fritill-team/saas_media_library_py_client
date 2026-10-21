"""``fritill.media.v1`` says what stories 7.7 and AD-10/AD-11 say.

Read from the descriptors wherever they can answer (what protoc built is what
both sides get); the `.proto` text is read only for claims about its comments:
the refusal tables and the Read-only/Mutating notes.
"""

from __future__ import annotations

import re
from pathlib import Path

import grpc
import pytest
from google.protobuf.descriptor import Descriptor, FieldDescriptor, ServiceDescriptor

from pkg_media import contract, methods, reasons

ROOT = Path(__file__).resolve().parents[1]
PROTO_DIR = ROOT / "proto" / "fritill" / "media" / "v1"

SERVICE_FILES = {
    "Assets": contract.assets_pb2,
    "Uploads": contract.uploads_pb2,
    "Grants": contract.grants_pb2,
    "Permalinks": contract.permalinks_pb2,
    "Delivery": contract.delivery_pb2,
    "Processing": contract.processing_pb2,
}


def _services() -> list[ServiceDescriptor]:
    found = []
    for module in SERVICE_FILES.values():
        found.extend(module.DESCRIPTOR.services_by_name.values())
    return found


SERVICES = _services()


def test_the_six_services_and_nothing_else() -> None:
    assert sorted(s.full_name for s in SERVICES) == sorted(
        f"fritill.media.v1.{name}" for name in SERVICE_FILES
    )
    assert not contract.common_pb2.DESCRIPTOR.services_by_name


@pytest.mark.parametrize("service", SERVICES, ids=lambda s: s.name)
def test_no_method_streams(service: ServiceDescriptor) -> None:
    streaming = [m.name for m in service.methods if m.client_streaming or m.server_streaming]
    assert not streaming, f"unary only: {streaming}"


@pytest.mark.parametrize("service", SERVICES, ids=lambda s: s.name)
def test_messages_are_named_after_their_method(service: ServiceDescriptor) -> None:
    for method in service.methods:
        assert method.input_type.name == f"{method.name}Request"
        assert method.output_type.name == f"{method.name}Response"


def test_every_wire_path_is_a_constant_and_every_constant_a_method() -> None:
    wire = {f"/{s.full_name}/{m.name}" for s in SERVICES for m in s.methods}
    assert methods.ALL_METHODS == wire
    for service_class in methods.SERVICES:
        service = next(s for s in SERVICES if s.full_name == service_class.SERVICE)
        own = {v for k, v in vars(service_class).items() if k.isupper() and k != "SERVICE"}
        assert own == {f"/{service.full_name}/{m.name}" for m in service.methods}


def test_mutating_and_read_only_split_every_method() -> None:
    assert methods.MUTATING_METHODS <= methods.ALL_METHODS
    assert methods.MUTATING_METHODS | methods.READ_ONLY_METHODS == methods.ALL_METHODS
    assert not methods.MUTATING_METHODS & methods.READ_ONLY_METHODS


_RPC = re.compile(r"((?:^\s*//.*\n)+)\s*rpc (\w+)\(", re.MULTILINE)


@pytest.mark.parametrize("name", SERVICE_FILES)
def test_each_method_says_whether_it_mutates(name: str) -> None:
    """The proto tells a server author which methods need an idempotency key."""
    text = (PROTO_DIR / f"{name.lower()}.proto").read_text()
    documented = {method: comment for comment, method in _RPC.findall(text)}
    for method in SERVICE_FILES[name].DESCRIPTOR.services_by_name[name].methods:
        comment = documented.get(method.name, "")
        path = f"/fritill.media.v1.{name}/{method.name}"
        expected = "Mutating" if path in methods.MUTATING_METHODS else "Read-only"
        assert expected in comment, f"{path} is not documented as {expected}"


def _fields(message: Descriptor, seen: set[str]) -> list[FieldDescriptor]:
    if message.full_name in seen:
        return []
    seen.add(message.full_name)
    found = list(message.fields)
    for field in message.fields:
        if field.type == FieldDescriptor.TYPE_MESSAGE and field.message_type.full_name.startswith("fritill."):
            found.extend(_fields(field.message_type, seen))
    return found


def test_no_request_carries_an_idempotency_key_or_a_credential() -> None:
    """Both travel as metadata (`idempotency-key`, `authorization`)."""
    for service in SERVICES:
        for method in service.methods:
            names = {f.name for f in _fields(method.input_type, set())}
            assert not names & {"idempotency_key", "authorization", "token", "credential"}, method.full_name


def test_the_error_domain_of_each_service_is_its_full_name() -> None:
    assert set(reasons.REFUSALS) == {s.full_name for s in SERVICES}
    assert set(contract.SERVICERS) == {s.full_name for s in SERVICES}
    for service in methods.SERVICES:
        assert service.SERVICE in reasons.REFUSALS


_TABLE_ROW = re.compile(r"^//\s*\|\s*([a-z_]+)\s*\|\s*([A-Z_]+)\s*\|\s*(permanent|transient)\s*\|\s*$", re.MULTILINE)


def _documented(path: Path) -> dict[str, grpc.StatusCode]:
    return {reason: grpc.StatusCode[status] for reason, status, _ in _TABLE_ROW.findall(path.read_text())}


@pytest.mark.parametrize("name", SERVICE_FILES)
def test_every_refusal_is_documented_in_the_proto(name: str) -> None:
    documented = {**_documented(PROTO_DIR / "common.proto"), **_documented(PROTO_DIR / f"{name.lower()}.proto")}
    declared = dict(reasons.REFUSALS[f"fritill.media.v1.{name}"])
    assert documented == declared
    assert f'ErrorInfo.domain = "fritill.media.v1.{name}"' in (PROTO_DIR / f"{name.lower()}.proto").read_text()


def test_the_common_refusals_are_the_common_table() -> None:
    assert _documented(PROTO_DIR / "common.proto") == dict(reasons.COMMON)


def test_declared_statuses_follow_the_http_map() -> None:
    allowed = set(reasons.GRPC_STATUS_FOR_HTTP.values()) | {grpc.StatusCode.INTERNAL}
    for table in reasons.REFUSALS.values():
        assert set(table.values()) <= allowed


@pytest.mark.parametrize(
    ("http", "expected"),
    [(400, "INVALID_ARGUMENT"), (422, "INVALID_ARGUMENT"), (401, "UNAUTHENTICATED"),
     (403, "PERMISSION_DENIED"), (404, "NOT_FOUND"), (409, "FAILED_PRECONDITION"),
     (410, "FAILED_PRECONDITION"), (429, "RESOURCE_EXHAUSTED"), (503, "UNAVAILABLE"),
     (500, "INTERNAL"), (418, "INTERNAL")],
)
def test_http_status_map(http: int, expected: str) -> None:
    assert reasons.grpc_status_for_http(http) is grpc.StatusCode[expected]


class _FailedCall(grpc.RpcError):
    def __init__(self, status: grpc.Status) -> None:
        self._metadata = status.trailing_metadata

    def trailing_metadata(self):  # noqa: ANN201 - grpc's signature
        return self._metadata


def test_a_refusal_carries_its_domain_and_reason() -> None:
    status = contract.refusal_status(methods.Assets.SERVICE, reasons.ASSET_NOT_FOUND, "Asset not found")
    assert status.code is grpc.StatusCode.NOT_FOUND
    assert status.details == "Asset not found"
    assert contract.error_info(_FailedCall(status)) == ("fritill.media.v1.Assets", "asset_not_found")


def test_an_undeclared_reason_cannot_be_sent() -> None:
    with pytest.raises(ValueError):
        contract.refusal_status(methods.Processing.SERVICE, reasons.ASSET_NOT_FOUND, "nope")


def test_a_call_without_error_info_has_none() -> None:
    class Bare(grpc.RpcError):
        def trailing_metadata(self):  # noqa: ANN201
            return ()

    assert contract.error_info(Bare()) is None


@pytest.mark.parametrize("full_name", sorted(contract.SERVICERS))
def test_every_service_has_an_exported_servicer_base_class(full_name: str) -> None:
    servicer, register = contract.SERVICERS[full_name]
    service = next(s for s in SERVICES if s.full_name == full_name)
    assert servicer.__name__ == f"{service.name}Servicer"
    for method in service.methods:
        assert callable(getattr(servicer, method.name))
    assert register.__name__ == f"add_{service.name}Servicer_to_server"
