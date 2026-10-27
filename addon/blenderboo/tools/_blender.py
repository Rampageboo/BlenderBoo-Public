"""Shared lazy Blender access and JSON conversion helpers."""

from __future__ import annotations

from typing import Any


def get_bpy():
    try:
        import bpy
    except ImportError as exc:
        raise RuntimeError("This tool requires execution inside Blender") from exc
    return bpy


def vector_values(value: Any) -> list[float]:
    return [float(component) for component in value]
