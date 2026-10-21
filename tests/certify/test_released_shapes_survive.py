"""`tools/check_compat.py` reports each breaking change, accepts compatible
ones, and the real tree is compatible with its baseline."""

from __future__ import annotations

import sys
from pathlib import Path

from google.protobuf import descriptor_pb2

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import check_compat  # noqa: E402

F = descriptor_pb2.FieldDescriptorProto


def _set(*, fields=(("nonce", 1, F.TYPE_STRING, F.LABEL_OPTIONAL),), reserved=(), methods=("Ping",),
         enum_values=(("A", 0), ("B", 1))) -> descriptor_pb2.FileDescriptorSet:
    fds = descriptor_pb2.FileDescriptorSet()
    file = fds.file.add(name="t.proto", package="fritill.test.v1", syntax="proto3")
    message = file.message_type.add(name="Req")
    for name, number, type_, label in fields:
        message.field.add(name=name, number=number, type=type_, label=label)
    for number in reserved:
        message.reserved_range.add(start=number, end=number + 1)
    enum = file.enum_type.add(name="E")
    for name, number in enum_values:
        enum.value.add(name=name, number=number)
    service = file.service.add(name="Svc")
    for method in methods:
        service.method.add(name=method, input_type=".fritill.test.v1.Req", output_type=".fritill.test.v1.Req")
    return fds


def test_identical_trees_are_compatible() -> None:
    assert check_compat.incompatibilities(_set(), _set()) == []


def test_additions_are_compatible() -> None:
    current = _set(
        fields=(("nonce", 1, F.TYPE_STRING, F.LABEL_OPTIONAL), ("extra", 2, F.TYPE_INT32, F.LABEL_OPTIONAL)),
        methods=("Ping", "Pong"),
        enum_values=(("A", 0), ("B", 1), ("C", 2)),
    )
    assert check_compat.incompatibilities(_set(), current) == []


def test_a_renamed_field_is_compatible() -> None:
    current = _set(fields=(("token", 1, F.TYPE_STRING, F.LABEL_OPTIONAL),))
    assert check_compat.incompatibilities(_set(), current) == []


def test_a_removed_field_needs_a_reservation() -> None:
    assert any("field removed" in f for f in check_compat.incompatibilities(_set(), _set(fields=())))
    assert check_compat.incompatibilities(_set(), _set(fields=(), reserved=(1,))) == []


def test_a_changed_type_or_label_is_reported() -> None:
    retyped = _set(fields=(("nonce", 1, F.TYPE_BYTES, F.LABEL_OPTIONAL),))
    relabelled = _set(fields=(("nonce", 1, F.TYPE_STRING, F.LABEL_REPEATED),))
    assert any("type changed" in f for f in check_compat.incompatibilities(_set(), retyped))
    assert any("label changed" in f for f in check_compat.incompatibilities(_set(), relabelled))


def test_a_removed_method_or_renumbered_enum_value_is_reported() -> None:
    assert any("method removed" in f for f in check_compat.incompatibilities(_set(), _set(methods=())))
    renumbered = _set(enum_values=(("A", 0), ("B", 2)))
    assert any("renumbered" in f for f in check_compat.incompatibilities(_set(), renumbered))


def test_a_dropped_reservation_is_reported() -> None:
    baseline = _set(fields=(), reserved=(1,))
    assert any("reservation dropped" in f for f in check_compat.incompatibilities(baseline, _set(fields=())))


def test_the_real_tree_matches_its_baseline() -> None:
    check_compat.check()
