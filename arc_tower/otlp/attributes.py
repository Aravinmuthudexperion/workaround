"""Conversions between Python values and OTLP AnyValue / KeyValue lists."""
from __future__ import annotations

from collections.abc import Iterable, MutableSequence
from typing import Any

from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue


def to_any_value(value: Any) -> AnyValue:
    if isinstance(value, bool):
        return AnyValue(bool_value=value)
    if isinstance(value, int):
        return AnyValue(int_value=value)
    if isinstance(value, float):
        return AnyValue(double_value=value)
    if isinstance(value, (list, tuple)):
        return AnyValue(array_value=ArrayValue(values=[to_any_value(v) for v in value]))
    return AnyValue(string_value="" if value is None else str(value))


def from_any_value(value: AnyValue) -> Any:
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [from_any_value(v) for v in value.array_value.values]
    if kind == "kvlist_value":
        return {kv.key: from_any_value(kv.value) for kv in value.kvlist_value.values}
    return getattr(value, kind)


def key_values(attrs: dict[str, Any]) -> list[KeyValue]:
    return [KeyValue(key=k, value=to_any_value(v)) for k, v in attrs.items() if v is not None]


def as_dict(attrs: Iterable[KeyValue]) -> dict[str, Any]:
    return {kv.key: from_any_value(kv.value) for kv in attrs}


def get(attrs: Iterable[KeyValue], key: str, default: Any = None) -> Any:
    for kv in attrs:
        if kv.key == key:
            return from_any_value(kv.value)
    return default


def upsert(attrs: MutableSequence[KeyValue], key: str, value: Any) -> None:
    for kv in attrs:
        if kv.key == key:
            kv.value.CopyFrom(to_any_value(value))
            return
    attrs.append(KeyValue(key=key, value=to_any_value(value)))


def remove_prefix(attrs: MutableSequence[KeyValue], prefix: str) -> int:
    """Delete every attribute whose key starts with prefix. Returns how many were removed."""
    keep = [kv for kv in attrs if not kv.key.startswith(prefix)]
    removed = len(attrs) - len(keep)
    if removed:
        del attrs[:]
        attrs.extend(keep)
    return removed
