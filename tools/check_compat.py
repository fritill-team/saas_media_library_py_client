"""Refuse a `.proto` change that breaks a shape this repository already released.

    uv run --group codegen python tools/check_compat.py

Compares the descriptor set written by `tools/generate.py` with
`proto/baseline.binpb` (the descriptor set of the last release). Exit 0 means a
consumer or server pinned to an older tag can still talk to a peer built from
this tree. Adapted from the estate's proto packages (fri_proto_authz).

Breaking (reported):

| Change | Why it breaks a pinned peer |
| --- | --- |
| a message, enum or service removed | its descriptor is gone |
| a method removed, or its request/response type changed | the call fails at runtime |
| a field removed without `reserved` | the number can be reused with another type |
| a field's type, message type or label changed | the same bytes decode as another value |
| an enum value removed or renumbered | a stored or in-flight value changes meaning |
| a `reserved` number or name dropped | reserve-then-reuse would pass the next check |

Renaming a field is not reported (the wire keys by number). Adding messages,
fields, methods, services and enum values is compatible.

The baseline moves forward only after a green check: the release workflow
does it (`tools/generate.py --baseline`), and by hand only for a signed-off
major, in a commit that does nothing else. Never move it to make a red check
green; for `reservation dropped`, put the `reserved` declaration back.
"""

from __future__ import annotations

import sys
from pathlib import Path

from google.protobuf import descriptor_pb2

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "proto" / "baseline.binpb"
CURRENT = ROOT / "src" / "pkg_media" / "generated" / "descriptor_set.binpb"


class Incompatible(Exception):
    """Raised with every incompatibility found, one per line."""


def _load(path: Path) -> descriptor_pb2.FileDescriptorSet:
    if not path.is_file():
        raise Incompatible(f"no descriptor set at {path} — run tools/generate.py first")
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(path.read_bytes())
    return fds


def _messages(
    fds: descriptor_pb2.FileDescriptorSet,
) -> dict[str, descriptor_pb2.DescriptorProto]:
    """Every message in the set, keyed by fully-qualified name, nested types included.

    Nested types are walked rather than skipped: a message declared inside another is on the wire
    exactly like a top-level one, and a check that only sees top-level messages passes a change
    that deletes a nested field.
    """
    found: dict[str, descriptor_pb2.DescriptorProto] = {}

    def walk(prefix: str, message: descriptor_pb2.DescriptorProto) -> None:
        name = f"{prefix}.{message.name}"
        found[name] = message
        for nested in message.nested_type:
            walk(name, nested)

    for file in fds.file:
        for message in file.message_type:
            walk(file.package, message)
    return found


def _enums(fds: descriptor_pb2.FileDescriptorSet) -> dict[str, descriptor_pb2.EnumDescriptorProto]:
    found: dict[str, descriptor_pb2.EnumDescriptorProto] = {}

    def walk(prefix: str, message: descriptor_pb2.DescriptorProto) -> None:
        name = f"{prefix}.{message.name}"
        for enum in message.enum_type:
            found[f"{name}.{enum.name}"] = enum
        for nested in message.nested_type:
            walk(name, nested)

    for file in fds.file:
        for enum in file.enum_type:
            found[f"{file.package}.{enum.name}"] = enum
        for message in file.message_type:
            walk(file.package, message)
    return found


def _services(
    fds: descriptor_pb2.FileDescriptorSet,
) -> dict[str, descriptor_pb2.ServiceDescriptorProto]:
    return {
        f"{file.package}.{service.name}": service for file in fds.file for service in file.service
    }


def _reserved_numbers(message: descriptor_pb2.DescriptorProto) -> set[int]:
    """Field numbers this message has explicitly retired.

    `reserved 3;` is a promise never to reuse 3, which is exactly the promise removal needs. A
    removal WITH a reservation is compatible; a removal without one is the silent-corruption case.
    """
    numbers: set[int] = set()
    for span in message.reserved_range:
        numbers.update(range(span.start, span.end))
    return numbers


def _enum_reserved_numbers(enum: descriptor_pb2.EnumDescriptorProto) -> set[int]:
    """The same promise, for an enum — and the end of the range is INCLUSIVE here.

    `DescriptorProto.ReservedRange.end` is exclusive and `EnumDescriptorProto.EnumReservedRange.end`
    is inclusive; protobuf's own descriptor comments say so and nothing about the two field names
    hints at it. Sharing `_reserved_numbers` between them would silently drop the last value of
    every enum reservation, which is a hole exactly one value wide and invisible in a green run.
    """
    numbers: set[int] = set()
    for span in enum.reserved_range:
        numbers.update(range(span.start, span.end + 1))
    return numbers


def _dropped_reservations(
    name: str,
    was_numbers: set[int],
    now_numbers: set[int],
    was_names: set[str],
    now_names: set[str],
    noun: str,
) -> list[str]:
    """Reservations the baseline holds and the tree no longer does (the reserve-then-reuse guard).

    A reservation is a promise never to reuse a retired number or name. The baseline is the LAST
    RELEASE, so without this the chain admits reserve-then-reuse: retire field 1 with `reserved 1;`
    in release n, drop the reservation and re-add field 1 with a different type in release n+1, and
    every individual step passes while a consumer pinned to release n-1 silently decodes the wrong
    value.

    Reported at the moment the reservation is DELETED rather than at the moment the number is
    reused, because by the reuse there is no longer anything in the baseline that says the number
    was ever spoken for.
    """
    findings: list[str] = []
    for number in sorted(was_numbers - now_numbers):
        findings.append(
            f"reservation dropped: {name} no longer reserves {noun} number {number} "
            f"(`reserved {number};` is a promise a released tag depends on)"
        )
    for dropped in sorted(was_names - now_names):
        findings.append(
            f'reservation dropped: {name} no longer reserves the {noun} name "{dropped}"'
        )
    return findings


def incompatibilities(
    baseline: descriptor_pb2.FileDescriptorSet,
    current: descriptor_pb2.FileDescriptorSet,
) -> list[str]:
    """Every wire-incompatible difference, as human-readable lines. Empty means compatible."""
    findings: list[str] = []

    was_messages, now_messages = _messages(baseline), _messages(current)
    for name, was in was_messages.items():
        now = now_messages.get(name)
        if now is None:
            findings.append(f"message removed: {name}")
            continue
        now_fields = {field.number: field for field in now.field}
        reserved = _reserved_numbers(now)
        for field in was.field:
            replacement = now_fields.get(field.number)
            if replacement is None:
                if field.number not in reserved:
                    findings.append(
                        f"field removed without `reserved {field.number};`: "
                        f"{name}.{field.name} (number {field.number})"
                    )
                continue
            if replacement.type != field.type:
                findings.append(
                    f"field type changed: {name}.{field.name} (number {field.number}) "
                    f"{descriptor_pb2.FieldDescriptorProto.Type.Name(field.type)} -> "
                    f"{descriptor_pb2.FieldDescriptorProto.Type.Name(replacement.type)}"
                )
            if replacement.label != field.label:
                findings.append(
                    f"field label changed: {name}.{field.name} (number {field.number}) "
                    f"{descriptor_pb2.FieldDescriptorProto.Label.Name(field.label)} -> "
                    f"{descriptor_pb2.FieldDescriptorProto.Label.Name(replacement.label)}"
                )
            if replacement.type_name != field.type_name:
                findings.append(
                    f"field message/enum type changed: {name}.{field.name} "
                    f"(number {field.number}) {field.type_name} -> {replacement.type_name}"
                )
        findings.extend(
            _dropped_reservations(
                name,
                _reserved_numbers(was),
                reserved,
                set(was.reserved_name),
                set(now.reserved_name),
                "field",
            )
        )

    was_enums, now_enums = _enums(baseline), _enums(current)
    for name, was_enum in was_enums.items():
        now_enum = now_enums.get(name)
        if now_enum is None:
            findings.append(f"enum removed: {name}")
            continue
        now_values = {value.name: value.number for value in now_enum.value}
        for value in was_enum.value:
            if value.name not in now_values:
                findings.append(f"enum value removed: {name}.{value.name} = {value.number}")
            elif now_values[value.name] != value.number:
                findings.append(
                    f"enum value renumbered: {name}.{value.name} "
                    f"{value.number} -> {now_values[value.name]}"
                )
        findings.extend(
            _dropped_reservations(
                name,
                _enum_reserved_numbers(was_enum),
                _enum_reserved_numbers(now_enum),
                set(was_enum.reserved_name),
                set(now_enum.reserved_name),
                "enum value",
            )
        )

    was_services, now_services = _services(baseline), _services(current)
    for name, was_service in was_services.items():
        now_service = now_services.get(name)
        if now_service is None:
            findings.append(f"service removed: {name}")
            continue
        now_methods = {method.name: method for method in now_service.method}
        for method in was_service.method:
            now_method = now_methods.get(method.name)
            if now_method is None:
                findings.append(f"method removed: {name}.{method.name}")
                continue
            if (now_method.input_type, now_method.output_type) != (
                method.input_type,
                method.output_type,
            ):
                findings.append(
                    f"method signature changed: {name}.{method.name} "
                    f"({method.input_type}) -> ({method.output_type})  becomes  "
                    f"({now_method.input_type}) -> ({now_method.output_type})"
                )

    return findings


def check() -> None:
    """Raise `Incompatible` listing every finding. Returns `None` silently when compatible."""
    findings = incompatibilities(_load(BASELINE), _load(CURRENT))
    if findings:
        raise Incompatible(
            "this tree breaks a shape already released from this repository:\n- "
            + "\n- ".join(findings)
            + "\n\nA released tag is immutable and a consumer may already have locked it. If the "
            "break is intended, it is a MAJOR version and the baseline moves forward in the same "
            "commit as the owner's sign-off: `tools/generate.py --baseline`. A `reservation "
            "dropped` line is never that: put the `reserved` declaration back. It costs nothing "
            "and it is what keeps a released number from being re-used under a different type."
        )


def main() -> int:
    try:
        check()
    except Incompatible as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    print("compatible with the released baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
