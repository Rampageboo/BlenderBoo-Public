"""Dry-run-first, budgeted BMesh operation chains with one-shot writeback."""

from __future__ import annotations

import math
from heapq import heappop, heappush
from typing import Any, Callable

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .destructive import prepare_destructive_action
from .mesh_topology import append_new_islands, capture_component_summary, fragment_small_ratio
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token


MAX_OPERATIONS = 100
MAX_MEASURE_QUERIES = 100
DEFAULT_ELEMENT_BUDGET = 500_000
MAX_ELEMENT_BUDGET = 5_000_000
OPERATION_KINDS = frozenset(
    {
        "select", "transform", "extrude", "inset", "bevel",
        "subdivide", "loop_cut", "edge_slide", "merge", "delete", "recalculate_normals",
        "dissolve", "fill", "grid_fill", "bridge", "connect", "poke", "bisect", "triangulate",
        "symmetrize", "spin", "align", "distribute", "snap", "workplane", "mark_edges",
    }
)
DESTRUCTIVE_KINDS = frozenset(
    {
        "extrude", "inset", "bevel", "subdivide", "loop_cut", "merge", "delete",
        "dissolve", "fill", "grid_fill", "bridge", "connect", "poke", "bisect", "triangulate",
        "symmetrize", "spin",
    }
)
SELECTION_PREDICATE_KINDS = frozenset(
    {
        "ALL", "INDICES", "BOUNDS", "MATERIAL", "NORMAL", "LOOSE",
        "BOUNDARY", "NON_MANIFOLD", "CONNECTED", "SHARP", "COPLANAR",
        "EDGE_LENGTH", "FACE_AREA", "VALENCE", "EDGE_LOOP", "EDGE_RING",
        "SHORTEST_PATH", "REGION",
    }
)
MEASURE_KINDS = frozenset(
    {
        "VERTEX_DISTANCE", "ANGLE", "EDGE_LENGTH", "FACE_AREA",
        "SELECTION_BOUNDS", "VOLUME",
    }
)


def _unknown(params: dict[str, Any], allowed: set[str]) -> None:
    names = sorted(set(params) - allowed)
    if names:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(names)}")


def _op_unknown(operation: dict[str, Any], allowed: set[str]) -> None:
    names = sorted(set(operation) - allowed)
    if names:
        raise InvalidParamsError(f"Unknown {operation.get('op', 'mesh')} operation fields: {', '.join(names)}")


def _query_unknown(query: dict[str, Any], allowed: set[str]) -> None:
    names = sorted(set(query) - allowed)
    if names:
        raise InvalidParamsError(
            f"Unknown {query.get('type', 'mesh')} measurement fields: {', '.join(names)}"
        )


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidParamsError(f"{label} must be a non-empty string")
    return value


def _strict_bool(value: Any, label: str, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{label} must be a boolean")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidParamsError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidParamsError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise InvalidParamsError(f"{label} must be a finite number from {minimum} to {maximum}")
    return result


def _vector3(value: Any, label: str, minimum: float = -1_000_000.0, maximum: float = 1_000_000.0) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise InvalidParamsError(f"{label} must be a three-number array")
    return [_number(item, label, minimum, maximum) for item in value]


def _enum(value: Any, label: str, allowed: set[str]) -> str:
    result = _name(value, label).upper()
    if result not in allowed:
        raise InvalidParamsError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return result


def _operations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_OPERATIONS:
        raise InvalidParamsError(f"operations must be a non-empty list of at most {MAX_OPERATIONS} objects")
    result = []
    for index, operation in enumerate(value):
        if not isinstance(operation, dict):
            raise InvalidParamsError(f"operations[{index}] must be an object")
        kind = operation.get("op")
        if kind not in OPERATION_KINDS:
            raise InvalidParamsError(
                f"operations[{index}].op must be one of: {', '.join(sorted(OPERATION_KINDS))}"
            )
        result.append(dict(operation))
    return result


def _queries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_MEASURE_QUERIES:
        raise InvalidParamsError(
            f"queries must be a non-empty list of at most {MAX_MEASURE_QUERIES} objects"
        )
    result = []
    for index, query in enumerate(value):
        if not isinstance(query, dict):
            raise InvalidParamsError(f"queries[{index}] must be an object")
        kind = query.get("type")
        if kind not in MEASURE_KINDS:
            raise InvalidParamsError(
                f"queries[{index}].type must be one of: {', '.join(sorted(MEASURE_KINDS))}"
            )
        result.append(dict(query))
    return result


def _mesh_object(bpy: Any, name: str) -> Any:
    obj = bpy.context.scene.objects.get(name)
    if obj is None:
        raise NotFoundError(f"Object not found in current scene: {name}")
    if obj.type != "MESH" or obj.data is None:
        raise InvalidStateError(f"Object is not a mesh: {name}")
    return obj


def _bm_counts(bm: Any) -> dict[str, int]:
    return {
        "vertices": len(bm.verts),
        "edges": len(bm.edges),
        "faces": len(bm.faces),
        "loops": sum(len(face.loops) for face in bm.faces),
    }


def _element_total(counts: dict[str, int]) -> int:
    return counts["vertices"] + counts["edges"] + counts["faces"] + counts["loops"]


def _ensure_budget(counts: dict[str, int], budget: int, label: str) -> None:
    total = _element_total(counts)
    if total > budget:
        raise InvalidStateError(f"{label} mesh has {total} total elements, above max_elements {budget}")


def _spin_preflight(
    source_counts: dict[str, int],
    selected_counts: dict[str, int],
    steps: int,
    use_duplicate: bool,
    budget: int,
) -> dict[str, int]:
    """Return a conservative BMesh spin size bound before allocating topology."""
    if use_duplicate:
        per_step = _element_total(selected_counts)
    else:
        per_step = (
            2 * selected_counts["vertices"]
            + 6 * selected_counts["edges"]
            + selected_counts["faces"]
            + selected_counts["loops"]
        )
    projected_total = _element_total(source_counts) + steps * per_step
    if projected_total > budget:
        raise InvalidStateError(
            f"spin projected upper bound {projected_total} exceeds max_elements {budget}"
        )
    return {"per_step": per_step, "projected_total": projected_total}


def _domain_elements(bm: Any, domain: str) -> list[Any]:
    if domain == "VERT":
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()
        return list(bm.verts)
    if domain == "EDGE":
        bm.edges.ensure_lookup_table()
        bm.edges.index_update()
        return list(bm.edges)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    return list(bm.faces)


def _clear_selection(bm: Any) -> None:
    for sequence in (bm.verts, bm.edges, bm.faces):
        for element in sequence:
            element.select_set(False)


def _element_center(element: Any, domain: str) -> Any:
    if domain == "VERT":
        return element.co
    if domain == "EDGE":
        return (element.verts[0].co + element.verts[1].co) * 0.5
    return element.calc_center_median()


def _index_list(
    value: Any,
    label: str,
    maximum_index: int,
    *,
    minimum_items: int = 1,
    maximum_items: int = DEFAULT_ELEMENT_BUDGET,
) -> list[int]:
    if (
        not isinstance(value, list)
        or not minimum_items <= len(value) <= maximum_items
    ):
        raise InvalidParamsError(
            f"{label} must contain from {minimum_items} to {maximum_items} integer indices"
        )
    result = []
    seen = set()
    for item in value:
        index = _integer(item, f"{label} index", 0, maximum_index)
        if index not in seen:
            seen.add(index)
            result.append(index)
    if len(result) < minimum_items:
        raise InvalidParamsError(f"{label} must contain at least {minimum_items} distinct indices")
    return result


def _bounded_walk(
    seeds: list[Any],
    neighbors: Callable[[Any], list[Any]],
    max_steps: int,
    label: str,
) -> set[Any]:
    """Return a deterministic bounded graph walk; kept Blender-independent for unit tests."""
    visited = set(seeds)
    if len(visited) > max_steps:
        raise InvalidStateError(f"{label} has more seeds than max_steps {max_steps}")
    pending = list(reversed(seeds))
    while pending:
        current = pending.pop()
        for neighbor in neighbors(current):
            if neighbor in visited:
                continue
            if len(visited) >= max_steps:
                raise InvalidStateError(f"{label} exceeded max_steps {max_steps}")
            visited.add(neighbor)
            pending.append(neighbor)
    return visited


def _bounded_shortest_path(
    start: Any,
    end: Any,
    neighbors: Callable[[Any], list[Any]],
    weight: Callable[[Any, Any], float],
    max_steps: int,
    label: str,
    key: Callable[[Any], int],
) -> list[Any]:
    """Return one deterministic bounded Dijkstra path without Blender dependencies."""
    best: dict[Any, tuple[float, int]] = {start: (0.0, 0)}
    previous: dict[Any, Any] = {}
    pending: list[tuple[float, int, int, Any]] = [(0.0, 0, key(start), start)]
    visited: set[Any] = set()
    while pending:
        distance, hops, _item_key, current = heappop(pending)
        if current in visited or best.get(current) != (distance, hops):
            continue
        if len(visited) >= max_steps:
            raise InvalidStateError(f"{label} exceeded max_steps {max_steps}")
        visited.add(current)
        if current == end:
            break
        for neighbor in sorted(set(neighbors(current)), key=key):
            if neighbor in visited:
                continue
            step = float(weight(current, neighbor))
            if not math.isfinite(step) or step < 0.0:
                raise InvalidStateError(f"{label} encountered an invalid edge weight")
            candidate = (distance + step, hops + 1)
            if neighbor in best and candidate >= best[neighbor]:
                continue
            best[neighbor] = candidate
            previous[neighbor] = current
            heappush(pending, (candidate[0], candidate[1], key(neighbor), neighbor))
    if end not in visited:
        raise InvalidStateError(f"{label} could not find a path between the requested elements")
    path = [end]
    while path[-1] != start:
        path.append(previous[path[-1]])
    path.reverse()
    return path


def _bounded_region(
    seeds: list[Any],
    neighbors: Callable[[Any], list[Any]],
    steps: int,
    max_elements: int,
    label: str,
    key: Callable[[Any], int],
    *,
    allowed: set[Any] | None = None,
) -> list[Any]:
    """Grow deterministic topological layers while enforcing a result budget."""
    ordered_seeds = sorted(set(seeds), key=key)
    if allowed is not None and any(seed not in allowed for seed in ordered_seeds):
        raise InvalidStateError(f"{label} seed is outside the candidate set")
    if len(ordered_seeds) > max_elements:
        raise InvalidStateError(f"{label} has more seeds than max_elements {max_elements}")
    visited = set(ordered_seeds)
    frontier = ordered_seeds
    for _depth in range(steps):
        next_frontier: list[Any] = []
        candidates = sorted(
            {
                neighbor
                for current in frontier
                for neighbor in neighbors(current)
                if neighbor not in visited and (allowed is None or neighbor in allowed)
            },
            key=key,
        )
        for candidate in candidates:
            if len(visited) >= max_elements:
                raise InvalidStateError(f"{label} exceeded max_elements {max_elements}")
            visited.add(candidate)
            next_frontier.append(candidate)
        if not next_frontier:
            break
        frontier = next_frontier
    return sorted(visited, key=key)


def _adjacent_elements(element: Any, domain: str) -> list[Any]:
    if domain == "VERT":
        return [edge.other_vert(element) for edge in element.link_edges]
    if domain == "EDGE":
        return [
            linked
            for vert in element.verts
            for linked in vert.link_edges
            if linked is not element
        ]
    return [
        linked
        for edge in element.edges
        for linked in edge.link_faces
        if linked is not element
    ]


def _seeded_walk(
    bm: Any,
    domain: str,
    indices: Any,
    max_steps: int,
    *,
    allowed: set[Any] | None = None,
    label: str = "CONNECTED selection",
) -> list[Any]:
    elements = _domain_elements(bm, domain)
    if not elements:
        raise InvalidStateError(f"Cannot walk an empty {domain.lower()} domain")
    normalized = _index_list(indices, "seed_indices", len(elements) - 1)
    seeds = [elements[index] for index in normalized]
    if allowed is not None and any(seed not in allowed for seed in seeds):
        raise InvalidStateError(f"{label} seed is outside the candidate set")

    def neighbors(element: Any) -> list[Any]:
        candidates = _adjacent_elements(element, domain)
        if allowed is not None:
            candidates = [candidate for candidate in candidates if candidate in allowed]
        return sorted(set(candidates), key=lambda item: int(item.index))

    walked = _bounded_walk(seeds, neighbors, max_steps, label)
    return sorted(walked, key=lambda item: int(item.index))


def _edge_loop_elements(seed: Any, max_steps: int) -> list[Any]:
    def neighbors(edge: Any) -> list[Any]:
        result = []
        linked_faces = set(edge.link_faces)
        for vert in edge.verts:
            candidates = [
                candidate
                for candidate in vert.link_edges
                if candidate is not edge
                and not linked_faces.intersection(candidate.link_faces)
            ]
            if len(candidates) == 1:
                result.append(candidates[0])
        return sorted(set(result), key=lambda item: int(item.index))

    walked = _bounded_walk([seed], neighbors, max_steps, "EDGE_LOOP selection")
    return sorted(walked, key=lambda item: int(item.index))


def _edge_ring_elements(seed: Any, max_steps: int) -> list[Any]:
    def neighbors(edge: Any) -> list[Any]:
        result = []
        edge_verts = set(edge.verts)
        for face in edge.link_faces:
            if len(face.edges) != 4:
                continue
            opposite = [candidate for candidate in face.edges if not edge_verts.intersection(candidate.verts)]
            if len(opposite) == 1:
                result.append(opposite[0])
        return sorted(set(result), key=lambda item: int(item.index))

    walked = _bounded_walk([seed], neighbors, max_steps, "EDGE_RING selection")
    return sorted(walked, key=lambda item: int(item.index))


def _constraint_elements(bm: Any, domain: str, value: Any, label: str) -> set[Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidParamsError(f"{label} must be a selection predicate object")
    kind = str(value.get("kind", "")).upper()
    if kind in {"SHORTEST_PATH", "REGION"}:
        raise InvalidParamsError(f"{label} cannot recursively use {kind}")
    return set(_predicate_elements(bm, domain, value))


def _predicate_elements(bm: Any, domain: str, predicate: Any) -> list[Any]:
    if not isinstance(predicate, dict):
        raise InvalidParamsError("selection.predicate must be an object")
    kind = _enum(predicate.get("kind"), "selection.predicate.kind", set(SELECTION_PREDICATE_KINDS))
    elements = _domain_elements(bm, domain)
    if kind == "ALL":
        _unknown(predicate, {"kind"})
        return elements
    if kind == "INDICES":
        _unknown(predicate, {"kind", "indices"})
        if not elements:
            raise InvalidStateError(f"Cannot select indices from an empty {domain.lower()} domain")
        normalized = _index_list(predicate.get("indices"), "selection indices", len(elements) - 1)
        return [elements[index] for index in normalized]
    if kind == "BOUNDS":
        _unknown(predicate, {"kind", "min", "max"})
        minimum = _vector3(predicate.get("min"), "selection bounds min")
        maximum = _vector3(predicate.get("max"), "selection bounds max")
        if any(minimum[index] > maximum[index] for index in range(3)):
            raise InvalidParamsError("selection bounds min must not exceed max")
        return [
            element for element in elements
            if all(minimum[index] <= float(_element_center(element, domain)[index]) <= maximum[index] for index in range(3))
        ]
    if kind == "MATERIAL":
        _unknown(predicate, {"kind", "material_index"})
        if domain != "FACE":
            raise InvalidParamsError("MATERIAL predicate requires FACE domain")
        material_index = _integer(predicate.get("material_index"), "material_index", 0, 32767)
        return [face for face in elements if int(face.material_index) == material_index]
    if kind == "NORMAL":
        _unknown(predicate, {"kind", "direction", "min_dot"})
        if domain != "FACE":
            raise InvalidParamsError("NORMAL predicate requires FACE domain")
        from mathutils import Vector

        direction = Vector(_vector3(predicate.get("direction"), "normal direction", -1.0, 1.0))
        if direction.length_squared <= 1e-20:
            raise InvalidParamsError("normal direction must be non-zero")
        direction.normalize()
        min_dot = _number(predicate.get("min_dot", 0.0), "min_dot", -1.0, 1.0)
        bm.normal_update()
        return [face for face in elements if float(face.normal.dot(direction)) >= min_dot]
    if kind in {"BOUNDARY", "NON_MANIFOLD"}:
        _unknown(predicate, {"kind"})
        expected = 1 if kind == "BOUNDARY" else None

        def edge_matches(edge: Any) -> bool:
            face_count = len(edge.link_faces)
            return face_count == expected if expected is not None else face_count != 2

        if domain == "EDGE":
            return [edge for edge in elements if edge_matches(edge)]
        if domain == "VERT":
            return [vert for vert in elements if any(edge_matches(edge) for edge in vert.link_edges)]
        return [face for face in elements if any(edge_matches(edge) for edge in face.edges)]
    if kind == "CONNECTED":
        _unknown(predicate, {"kind", "seed_indices", "max_steps"})
        max_steps = _integer(
            predicate.get("max_steps", min(DEFAULT_ELEMENT_BUDGET, max(1, len(elements)))),
            "max_steps",
            1,
            DEFAULT_ELEMENT_BUDGET,
        )
        return _seeded_walk(bm, domain, predicate.get("seed_indices"), max_steps)
    if kind == "SHORTEST_PATH":
        _unknown(
            predicate,
            {"kind", "start_index", "end_index", "metric", "max_steps", "within"},
        )
        if not elements:
            raise InvalidStateError(f"Cannot select a shortest path from an empty {domain.lower()} domain")
        start_index = _integer(predicate.get("start_index"), "start_index", 0, len(elements) - 1)
        end_index = _integer(predicate.get("end_index"), "end_index", 0, len(elements) - 1)
        metric = _enum(
            predicate.get("metric", "TOPOLOGY"),
            "metric",
            {"TOPOLOGY", "DISTANCE"},
        )
        max_steps = _integer(
            predicate.get("max_steps", min(DEFAULT_ELEMENT_BUDGET, max(1, len(elements)))),
            "max_steps",
            1,
            DEFAULT_ELEMENT_BUDGET,
        )
        allowed = _constraint_elements(bm, domain, predicate.get("within"), "within")
        start = elements[start_index]
        end = elements[end_index]
        if allowed is not None and (start not in allowed or end not in allowed):
            raise InvalidStateError("SHORTEST_PATH endpoints must be inside the within candidate set")

        def path_neighbors(element: Any) -> list[Any]:
            candidates = _adjacent_elements(element, domain)
            return candidates if allowed is None else [item for item in candidates if item in allowed]

        def path_weight(left: Any, right: Any) -> float:
            if metric == "TOPOLOGY":
                return 1.0
            return float((_element_center(right, domain) - _element_center(left, domain)).length)

        return _bounded_shortest_path(
            start,
            end,
            path_neighbors,
            path_weight,
            max_steps,
            "SHORTEST_PATH selection",
            lambda item: int(item.index),
        )
    if kind == "REGION":
        _unknown(predicate, {"kind", "seed_indices", "steps", "max_elements", "within"})
        if not elements:
            raise InvalidStateError(f"Cannot grow a region from an empty {domain.lower()} domain")
        seed_indices = _index_list(predicate.get("seed_indices"), "seed_indices", len(elements) - 1)
        steps = _integer(predicate.get("steps", 1), "steps", 0, 100_000)
        max_elements = _integer(
            predicate.get("max_elements", min(DEFAULT_ELEMENT_BUDGET, len(elements))),
            "max_elements",
            1,
            DEFAULT_ELEMENT_BUDGET,
        )
        allowed = _constraint_elements(bm, domain, predicate.get("within"), "within")
        return _bounded_region(
            [elements[index] for index in seed_indices],
            lambda element: _adjacent_elements(element, domain),
            steps,
            max_elements,
            "REGION selection",
            lambda item: int(item.index),
            allowed=allowed,
        )
    if kind == "SHARP":
        _unknown(predicate, {"kind", "min_angle", "include_boundary"})
        if domain != "EDGE":
            raise InvalidParamsError("SHARP predicate requires EDGE domain")
        min_angle = _number(predicate.get("min_angle", math.radians(30.0)), "min_angle", 0.0, math.pi)
        include_boundary = _strict_bool(predicate.get("include_boundary"), "include_boundary", False)
        bm.normal_update()
        result = []
        for edge in elements:
            if len(edge.link_faces) == 2 and float(edge.calc_face_angle(0.0)) >= min_angle:
                result.append(edge)
            elif include_boundary and len(edge.link_faces) == 1:
                result.append(edge)
        return result
    if kind == "COPLANAR":
        _unknown(
            predicate,
            {"kind", "seed_index", "max_angle", "max_distance", "allow_flipped", "connected", "max_steps"},
        )
        if domain != "FACE":
            raise InvalidParamsError("COPLANAR predicate requires FACE domain")
        if not elements:
            raise InvalidStateError("Cannot select a coplanar region from an empty face domain")
        seed_index = _integer(predicate.get("seed_index"), "seed_index", 0, len(elements) - 1)
        max_angle = _number(predicate.get("max_angle", math.radians(1.0)), "max_angle", 0.0, math.pi)
        max_distance = _number(predicate.get("max_distance", 1e-5), "max_distance", 0.0, 1_000_000.0)
        allow_flipped = _strict_bool(predicate.get("allow_flipped"), "allow_flipped", False)
        connected = _strict_bool(predicate.get("connected"), "connected", True)
        bm.normal_update()
        seed = elements[seed_index]
        if seed.normal.length_squared <= 1e-20:
            raise InvalidStateError("COPLANAR seed face has a degenerate normal")
        seed_normal = seed.normal.normalized()
        seed_center = seed.calc_center_median()
        min_dot = math.cos(max_angle)
        candidates = set()
        for face in elements:
            if face.normal.length_squared <= 1e-20:
                continue
            dot = float(face.normal.normalized().dot(seed_normal))
            if allow_flipped:
                dot = abs(dot)
            distance = abs(float((face.calc_center_median() - seed_center).dot(seed_normal)))
            if dot >= min_dot and distance <= max_distance:
                candidates.add(face)
        if not connected:
            return sorted(candidates, key=lambda item: int(item.index))
        max_steps = _integer(
            predicate.get("max_steps", min(DEFAULT_ELEMENT_BUDGET, max(1, len(elements)))),
            "max_steps",
            1,
            DEFAULT_ELEMENT_BUDGET,
        )
        return _seeded_walk(
            bm,
            "FACE",
            [seed_index],
            max_steps,
            allowed=candidates,
            label="COPLANAR connected selection",
        )
    if kind == "EDGE_LENGTH":
        _unknown(predicate, {"kind", "min", "max"})
        if domain != "EDGE":
            raise InvalidParamsError("EDGE_LENGTH predicate requires EDGE domain")
        minimum = _number(predicate.get("min", 0.0), "edge length min", 0.0, 1_000_000.0)
        maximum = _number(predicate.get("max", 1_000_000.0), "edge length max", 0.0, 1_000_000.0)
        if minimum > maximum:
            raise InvalidParamsError("edge length min must not exceed max")
        return [edge for edge in elements if minimum <= float(edge.calc_length()) <= maximum]
    if kind == "FACE_AREA":
        _unknown(predicate, {"kind", "min", "max"})
        if domain != "FACE":
            raise InvalidParamsError("FACE_AREA predicate requires FACE domain")
        minimum = _number(predicate.get("min", 0.0), "face area min", 0.0, 1e12)
        maximum = _number(predicate.get("max", 1e12), "face area max", 0.0, 1e12)
        if minimum > maximum:
            raise InvalidParamsError("face area min must not exceed max")
        return [face for face in elements if minimum <= float(face.calc_area()) <= maximum]
    if kind == "VALENCE":
        _unknown(predicate, {"kind", "min", "max"})
        if domain != "VERT":
            raise InvalidParamsError("VALENCE predicate requires VERT domain")
        minimum = _integer(predicate.get("min", 0), "valence min", 0, 100_000)
        maximum = _integer(predicate.get("max", 100_000), "valence max", 0, 100_000)
        if minimum > maximum:
            raise InvalidParamsError("valence min must not exceed max")
        return [vert for vert in elements if minimum <= len(vert.link_edges) <= maximum]
    if kind in {"EDGE_LOOP", "EDGE_RING"}:
        _unknown(predicate, {"kind", "seed_index", "max_steps"})
        if domain != "EDGE":
            raise InvalidParamsError(f"{kind} predicate requires EDGE domain")
        if not elements:
            raise InvalidStateError(f"Cannot select {kind} from an empty edge domain")
        seed_index = _integer(predicate.get("seed_index"), "seed_index", 0, len(elements) - 1)
        max_steps = _integer(
            predicate.get("max_steps", min(100_000, max(1, len(elements)))),
            "max_steps",
            1,
            100_000,
        )
        if kind == "EDGE_LOOP":
            return _edge_loop_elements(elements[seed_index], max_steps)
        return _edge_ring_elements(elements[seed_index], max_steps)
    _unknown(predicate, {"kind"})
    if domain == "VERT":
        return [vert for vert in elements if not vert.link_edges]
    if domain == "EDGE":
        return [edge for edge in elements if not edge.link_faces]
    raise InvalidParamsError("LOOSE predicate supports only VERT or EDGE domain")


def _apply_selection(bm: Any, selection: Any) -> dict[str, Any]:
    if not isinstance(selection, dict):
        raise InvalidParamsError("selection must be an object")
    _unknown(selection, {"domain", "action", "predicate"})
    domain = _enum(selection.get("domain"), "selection.domain", {"VERT", "EDGE", "FACE"})
    action = _enum(selection.get("action", "SET"), "selection.action", {"SET", "ADD", "SUBTRACT"})
    elements = _domain_elements(bm, domain)
    matches = _predicate_elements(bm, domain, selection.get("predicate"))
    if action == "SET":
        for element in elements:
            element.select_set(False)
    selected = action != "SUBTRACT"
    for element in matches:
        element.select_set(selected)
    return {
        "domain": domain,
        "action": action,
        "matched": len(matches),
        "selected": sum(1 for element in elements if element.select),
    }


def _selected(bm: Any, domain: str, label: str) -> list[Any]:
    elements = [element for element in _domain_elements(bm, domain) if element.select]
    if not elements:
        raise InvalidStateError(f"{label} requires at least one selected {domain.lower()} element")
    return elements


def _selection_if_present(bm: Any, operation: dict[str, Any]) -> dict[str, Any] | None:
    return _apply_selection(bm, operation["selection"]) if "selection" in operation else None


def _op_transform(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection", "translation", "rotation_euler", "scale", "pivot", "pivot_point", "matrix"})
    selection = _selection_if_present(bm, operation)
    if not any(name in operation for name in ("translation", "rotation_euler", "scale", "matrix")):
        raise InvalidParamsError("transform requires translation, rotation_euler, scale, or matrix")
    verts = _selected(bm, "VERT", "transform")
    from mathutils import Euler, Matrix, Vector

    if "matrix" in operation:
        if any(name in operation for name in ("translation", "rotation_euler", "scale", "pivot", "pivot_point")):
            raise InvalidParamsError("matrix cannot be combined with other transform fields")
        rows = operation["matrix"]
        if not isinstance(rows, (list, tuple)) or len(rows) != 4 or any(not isinstance(row, (list, tuple)) or len(row) != 4 for row in rows):
            raise InvalidParamsError("matrix must be a 4x4 numeric array")
        values = [[_number(value, "matrix", -1_000_000.0, 1_000_000.0) for value in row] for row in rows]
        if any(abs(left - right) > 1e-9 for left, right in zip(values[3], (0.0, 0.0, 0.0, 1.0))):
            raise InvalidParamsError("matrix must be affine (last row [0, 0, 0, 1])")
        matrix = Matrix(values)
        if matrix == Matrix.Identity(4):
            return 0, selection
        bmesh.ops.transform(bm, verts=verts, matrix=matrix)
        return len(verts), selection

    translation = Vector(_vector3(operation.get("translation", [0, 0, 0]), "translation"))
    rotation_values = _vector3(
        operation.get("rotation_euler", [0, 0, 0]),
        "rotation_euler",
        -math.tau * 1000,
        math.tau * 1000,
    )
    rotation = Euler(rotation_values).to_matrix().to_4x4()
    scale_values = _vector3(operation.get("scale", [1, 1, 1]), "scale", -1_000_000.0, 1_000_000.0)
    scale = Matrix.Diagonal((*scale_values, 1.0))
    pivot_mode = _enum(operation.get("pivot", "MEDIAN"), "pivot", {"ORIGIN", "MEDIAN"})
    pivot = Vector((0.0, 0.0, 0.0))
    if "pivot_point" in operation:
        if "pivot" in operation:
            raise InvalidParamsError("pivot_point cannot be combined with pivot")
        pivot = Vector(_vector3(operation["pivot_point"], "pivot_point"))
    elif pivot_mode == "MEDIAN":
        pivot = sum((vert.co for vert in verts), Vector()) / len(verts)
    if (
        not any(translation)
        and all(abs(value) <= 1e-15 for value in rotation_values)
        and all(abs(value - 1.0) <= 1e-15 for value in scale_values)
    ):
        return 0, selection
    matrix = Matrix.Translation(translation) @ Matrix.Translation(pivot) @ rotation @ scale @ Matrix.Translation(-pivot)
    bmesh.ops.transform(bm, verts=verts, matrix=matrix)
    return len(verts), selection


def _op_mark_edges(_bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection", "seam", "sharp", "crease", "bevel_weight"})
    selection = _selection_if_present(bm, operation)
    if selection is not None and selection["domain"] != "EDGE":
        raise InvalidParamsError("mark_edges selection must use EDGE domain")
    fields = set(operation) & {"seam", "sharp", "crease", "bevel_weight"}
    if not fields:
        raise InvalidParamsError("mark_edges requires at least one edge field")
    edge_indices = [int(edge.index) for edge in _selected(bm, "EDGE", "mark_edges")]
    seam = _strict_bool(operation["seam"], "seam") if "seam" in fields else None
    sharp = _strict_bool(operation["sharp"], "sharp") if "sharp" in fields else None
    crease = _number(operation["crease"], "crease", 0.0, 1.0) if "crease" in fields else None
    bevel_weight = _number(operation["bevel_weight"], "bevel_weight", 0.0, 1.0) if "bevel_weight" in fields else None
    if crease is not None and bm.edges.layers.float.get("crease_edge") is None:
        bm.edges.layers.float.new("crease_edge")
    if bevel_weight is not None and bm.edges.layers.float.get("bevel_weight_edge") is None:
        bm.edges.layers.float.new("bevel_weight_edge")
    # Adding a custom-data layer can invalidate both BMEdge and layer wrappers.
    crease_layer = bm.edges.layers.float.get("crease_edge") if crease is not None else None
    bevel_layer = bm.edges.layers.float.get("bevel_weight_edge") if bevel_weight is not None else None
    bm.edges.ensure_lookup_table()
    edges = [bm.edges[index] for index in edge_indices]
    changed = 0
    for edge in edges:
        before = (edge.seam, edge.smooth,
                  edge[crease_layer] if crease_layer is not None else None,
                  edge[bevel_layer] if bevel_layer is not None else None)
        if seam is not None:
            edge.seam = seam
        if sharp is not None:
            edge.smooth = not sharp
        if crease is not None:
            edge[crease_layer] = crease
        if bevel_weight is not None:
            edge[bevel_layer] = bevel_weight
        after = (edge.seam, edge.smooth,
                 edge[crease_layer] if crease_layer is not None else None,
                 edge[bevel_layer] if bevel_layer is not None else None)
        changed += before != after
    return changed, selection


def _axis_index(value: Any, label: str = "axis") -> int:
    return {"X": 0, "Y": 1, "Z": 2}[_enum(value, label, {"X", "Y", "Z"})]


def _axes(value: Any, label: str = "axes") -> list[int]:
    raw = ["X", "Y", "Z"] if value is None else value
    if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
        raise InvalidParamsError(f"{label} must contain from 1 to 3 axis names")
    names = [_enum(item, label, {"X", "Y", "Z"}) for item in raw]
    if len(names) != len(set(names)):
        raise InvalidParamsError(f"{label} must not contain duplicate axes")
    return [{"X": 0, "Y": 1, "Z": 2}[name] for name in names]


def _op_align(_bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection", "axis", "mode", "value"})
    selection = _selection_if_present(bm, operation)
    verts = _selected(bm, "VERT", "align")
    axis = _axis_index(operation.get("axis"))
    mode = _enum(operation.get("mode", "MEAN"), "mode", {"MIN", "MAX", "MEAN", "MEDIAN", "VALUE"})
    if mode == "VALUE":
        if "value" not in operation:
            raise InvalidParamsError("align mode VALUE requires value")
        target = _number(operation["value"], "value", -1_000_000.0, 1_000_000.0)
    else:
        if "value" in operation:
            raise InvalidParamsError("align value is only valid for mode VALUE")
        coordinates = sorted(float(vert.co[axis]) for vert in verts)
        if mode == "MIN":
            target = coordinates[0]
        elif mode == "MAX":
            target = coordinates[-1]
        elif mode == "MEAN":
            target = math.fsum(coordinates) / len(coordinates)
        else:
            middle = len(coordinates) // 2
            target = (
                coordinates[middle]
                if len(coordinates) % 2
                else (coordinates[middle - 1] + coordinates[middle]) * 0.5
            )
    changed = 0
    for vert in verts:
        if abs(float(vert.co[axis]) - target) > 1e-12:
            vert.co[axis] = target
            changed += 1
    return changed, selection


def _op_distribute(_bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection", "axis", "start", "end"})
    selection = _selection_if_present(bm, operation)
    verts = _selected(bm, "VERT", "distribute")
    if len(verts) < 2:
        raise InvalidStateError("distribute requires at least two selected vertices")
    axis = _axis_index(operation.get("axis"))
    has_start = "start" in operation
    has_end = "end" in operation
    if has_start != has_end:
        raise InvalidParamsError("distribute start and end must be supplied together")
    ordered = sorted(verts, key=lambda vert: (float(vert.co[axis]), int(vert.index)))
    if has_start:
        start = _number(operation["start"], "start", -1_000_000.0, 1_000_000.0)
        end = _number(operation["end"], "end", -1_000_000.0, 1_000_000.0)
    else:
        start = float(ordered[0].co[axis])
        end = float(ordered[-1].co[axis])
    step = (end - start) / (len(ordered) - 1)
    changed = 0
    for index, vert in enumerate(ordered):
        target = start + step * index
        if abs(float(vert.co[axis]) - target) > 1e-12:
            vert.co[axis] = target
            changed += 1
    return changed, selection


def _op_snap(_bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    mode = _enum(operation.get("mode", "GRID"), "mode", {"GRID", "POINT", "PLANE"})
    selection = _selection_if_present(bm, operation)
    verts = _selected(bm, "VERT", "snap")
    if mode == "GRID":
        _op_unknown(operation, {"op", "selection", "mode", "axes", "increment", "offset"})
        axes = _axes(operation.get("axes"))
        increments = _vector3(operation.get("increment", [1.0, 1.0, 1.0]), "increment", 0.0, 1_000_000.0)
        offsets = _vector3(operation.get("offset", [0.0, 0.0, 0.0]), "offset")
        if any(increments[axis] <= 0.0 for axis in axes):
            raise InvalidParamsError("snap GRID increment must be positive on every selected axis")

        def snapped(coordinate: Any) -> Any:
            result = coordinate.copy()
            for axis in axes:
                result[axis] = round((float(coordinate[axis]) - offsets[axis]) / increments[axis]) * increments[axis] + offsets[axis]
            return result

    elif mode == "POINT":
        _op_unknown(operation, {"op", "selection", "mode", "axes", "point"})
        axes = _axes(operation.get("axes"))
        point = _vector3(operation.get("point"), "point")

        def snapped(coordinate: Any) -> Any:
            result = coordinate.copy()
            for axis in axes:
                result[axis] = point[axis]
            return result

    else:
        _op_unknown(operation, {"op", "selection", "mode", "point", "normal"})
        from mathutils import Vector

        point = Vector(_vector3(operation.get("point"), "point"))
        normal = Vector(_vector3(operation.get("normal"), "normal", -1.0, 1.0))
        if normal.length_squared <= 1e-20:
            raise InvalidParamsError("snap PLANE normal must be non-zero")
        normal.normalize()

        def snapped(coordinate: Any) -> Any:
            return coordinate - normal * float((coordinate - point).dot(normal))

    changed = 0
    for vert in verts:
        target = snapped(vert.co)
        if (target - vert.co).length_squared > 1e-24:
            vert.co = target
            changed += 1
    return changed, selection


def _workplane_axis_indices(value: Any, label: str = "axes") -> list[int]:
    raw = ["N"] if value is None else value
    if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
        raise InvalidParamsError(f"{label} must contain from 1 to 3 axis names")
    names = [_enum(item, label, {"U", "V", "N"}) for item in raw]
    if len(names) != len(set(names)):
        raise InvalidParamsError(f"{label} must not contain duplicate axes")
    return [{"U": 0, "V": 1, "N": 2}[name] for name in names]


def _workplane_frame(bm: Any, value: Any) -> tuple[Any, Any, Any, Any, dict[str, Any]]:
    if not isinstance(value, dict):
        raise InvalidParamsError("plane must be an object")
    source = _enum(value.get("source"), "plane.source", {"EXPLICIT", "FACE"})
    from mathutils import Vector

    if source == "EXPLICIT":
        _unknown(value, {"source", "origin", "normal", "u_axis"})
        origin = Vector(_vector3(value.get("origin"), "plane.origin"))
        normal = Vector(_vector3(value.get("normal"), "plane.normal", -1.0, 1.0))
        u_axis = Vector(_vector3(value.get("u_axis"), "plane.u_axis", -1.0, 1.0))
        reference = {"source": source}
    else:
        _unknown(value, {"source", "face_index", "edge_index"})
        faces = _domain_elements(bm, "FACE")
        edges = _domain_elements(bm, "EDGE")
        if not faces or not edges:
            raise InvalidStateError("FACE workplane requires a mesh with faces and edges")
        face_index = _integer(value.get("face_index"), "plane.face_index", 0, len(faces) - 1)
        edge_index = _integer(value.get("edge_index"), "plane.edge_index", 0, len(edges) - 1)
        face = faces[face_index]
        edge = edges[edge_index]
        if edge not in face.edges:
            raise InvalidStateError("plane.edge_index must identify an edge of plane.face_index")
        bm.normal_update()
        origin = face.calc_center_median()
        normal = face.normal.copy()
        first, second = sorted(edge.verts, key=lambda vert: int(vert.index))
        u_axis = second.co - first.co
        reference = {"source": source, "face_index": face_index, "edge_index": edge_index}

    if normal.length_squared <= 1e-20:
        raise InvalidStateError("workplane normal is degenerate")
    normal.normalize()
    u_axis = u_axis - normal * float(u_axis.dot(normal))
    if u_axis.length_squared <= 1e-20:
        raise InvalidStateError("workplane U axis must not be parallel to its normal")
    u_axis.normalize()
    v_axis = normal.cross(u_axis)
    if v_axis.length_squared <= 1e-20:
        raise InvalidStateError("workplane could not form an orthogonal V axis")
    v_axis.normalize()
    details = {
        **reference,
        "origin": [float(value) for value in origin],
        "u_axis": [float(value) for value in u_axis],
        "v_axis": [float(value) for value in v_axis],
        "normal": [float(value) for value in normal],
    }
    return origin, u_axis, v_axis, normal, details


def _op_workplane(_bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    mode = _enum(operation.get("mode", "PROJECT"), "mode", {"PROJECT", "ALIGN", "GRID"})
    common = {"op", "selection", "plane", "mode", "axes"}
    allowed = {
        "PROJECT": common | {"distance"},
        "ALIGN": common | {"values"},
        "GRID": common | {"increment", "offset"},
    }[mode]
    _op_unknown(operation, allowed)
    selection = _selection_if_present(bm, operation)
    if selection is not None and selection["domain"] != "VERT":
        raise InvalidParamsError("workplane selection must use the VERT domain")
    verts = _selected(bm, "VERT", "workplane")
    origin, u_axis, v_axis, normal, details = _workplane_frame(bm, operation.get("plane"))
    basis = (u_axis, v_axis, normal)

    if mode == "PROJECT":
        if "axes" in operation:
            raise InvalidParamsError("workplane PROJECT does not accept axes; it always projects along N")
        axes = [2]
        values = [0.0, 0.0, _number(operation.get("distance", 0.0), "distance", -1_000_000.0, 1_000_000.0)]
        increments = None
        offsets = None
    elif mode == "ALIGN":
        axes = _workplane_axis_indices(operation.get("axes"))
        values = _vector3(operation.get("values", [0.0, 0.0, 0.0]), "values")
        increments = None
        offsets = None
    else:
        axes = _workplane_axis_indices(operation.get("axes", ["U", "V"]))
        values = None
        increments = _vector3(operation.get("increment", [1.0, 1.0, 1.0]), "increment", 0.0, 1_000_000.0)
        offsets = _vector3(operation.get("offset", [0.0, 0.0, 0.0]), "offset")
        if any(increments[axis] <= 0.0 for axis in axes):
            raise InvalidParamsError("workplane GRID increment must be positive on every selected axis")

    changed = 0
    for vert in verts:
        delta = vert.co - origin
        coordinates = [float(delta.dot(axis)) for axis in basis]
        for axis in axes:
            if mode in {"PROJECT", "ALIGN"}:
                coordinates[axis] = values[axis]
            else:
                coordinates[axis] = (
                    round((coordinates[axis] - offsets[axis]) / increments[axis])
                    * increments[axis]
                    + offsets[axis]
                )
        target = origin + sum(
            (basis[index] * coordinates[index] for index in range(3)),
            origin.copy() * 0.0,
        )
        if (target - vert.co).length_squared > 1e-24:
            vert.co = target
            changed += 1

    operation_details = dict(selection or {})
    operation_details.update({"mode": mode, "workplane": details})
    return changed, operation_details


def _op_extrude(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection", "offset"})
    selection = _selection_if_present(bm, operation)
    faces = _selected(bm, "FACE", "extrude")
    result = bmesh.ops.extrude_face_region(bm, geom=faces, use_select_history=False)
    new_verts = [
        element for element in result.get("geom", ())
        if isinstance(element, bmesh.types.BMVert)
    ]
    offset = _vector3(operation.get("offset", [0, 0, 0]), "offset")
    if new_verts and any(offset):
        from mathutils import Vector

        bmesh.ops.translate(bm, verts=new_verts, vec=Vector(offset))
    _clear_selection(bm)
    for element in result.get("geom", ()):
        if hasattr(element, "select_set"):
            element.select_set(True)
    return len(faces), selection


def _op_inset(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection", "thickness", "depth", "use_even_offset", "use_boundary"})
    selection = _selection_if_present(bm, operation)
    faces = _selected(bm, "FACE", "inset")
    bmesh.ops.inset_region(
        bm,
        faces=faces,
        thickness=_number(operation.get("thickness"), "thickness", 0.0, 1_000_000.0),
        depth=_number(operation.get("depth", 0.0), "depth", -1_000_000.0, 1_000_000.0),
        use_even_offset=_strict_bool(operation.get("use_even_offset"), "use_even_offset", True),
        use_boundary=_strict_bool(operation.get("use_boundary"), "use_boundary", True),
    )
    return len(faces), selection


def _op_bevel(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection", "affect", "offset", "segments", "clamp_overlap"})
    selection = _selection_if_present(bm, operation)
    affect = _enum(operation.get("affect", "EDGES"), "affect", {"EDGES", "VERTICES"})
    domain = "EDGE" if affect == "EDGES" else "VERT"
    geometry = _selected(bm, domain, "bevel")
    bmesh.ops.bevel(
        bm,
        geom=geometry,
        offset=_number(operation.get("offset"), "offset", 0.0, 1_000_000.0),
        segments=_integer(operation.get("segments", 1), "segments", 1, 100),
        affect=affect,
        clamp_overlap=_strict_bool(operation.get("clamp_overlap"), "clamp_overlap", True),
    )
    return len(geometry), selection


def _op_subdivide(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection", "cuts", "smooth", "use_grid_fill"})
    selection = _selection_if_present(bm, operation)
    edges = _selected(bm, "EDGE", "subdivide")
    bmesh.ops.subdivide_edges(
        bm,
        edges=edges,
        cuts=_integer(operation.get("cuts", 1), "cuts", 1, 100),
        smooth=_number(operation.get("smooth", 0.0), "smooth", -1.0, 1.0),
        use_grid_fill=_strict_bool(operation.get("use_grid_fill"), "use_grid_fill", True),
    )
    return len(edges), selection


def _op_loop_cut(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    """Subdivide exactly one complete quad edge ring without viewport context."""
    _op_unknown(operation, {"op", "selection", "cuts"})
    selection = _selection_if_present(bm, operation)
    if selection is not None and selection["domain"] != "EDGE":
        raise InvalidParamsError("loop_cut selection must use the EDGE domain")
    edges = _selected(bm, "EDGE", "loop_cut")
    expected_ring = _edge_ring_elements(
        min(edges, key=lambda edge: int(edge.index)),
        max(1, len(bm.edges)),
    )
    if len(expected_ring) < 2 or set(edges) != set(expected_ring):
        raise InvalidStateError(
            "loop_cut requires exactly one complete edge ring connected through quad faces"
        )
    cuts = _integer(operation.get("cuts", 1), "cuts", 1, 100)
    existing_verts = set(bm.verts)
    bmesh.ops.subdivide_edgering(
        bm,
        edges=edges,
        interp_mode="LINEAR",
        smooth=0.0,
        cuts=cuts,
    )
    new_verts = [vert for vert in bm.verts if vert not in existing_verts]
    if len(new_verts) != len(edges) * cuts:
        raise InvalidStateError(
            "loop_cut produced an unexpected vertex count; the edge ring may not be consistently quad-based"
        )
    _select_result_geometry(bm, new_verts)
    return len(new_verts), selection


def _slide_chain_neighbors(verts: list[Any]) -> dict[Any, list[Any]]:
    """Validate one selected open/cyclic vertex chain and return its selected neighbors."""
    if len(verts) < 2:
        raise InvalidStateError("edge_slide requires at least two selected vertices")
    selected = set(verts)
    neighbors = {
        vert: sorted(
            {
                edge.other_vert(vert)
                for edge in vert.link_edges
                if edge.other_vert(vert) in selected
            },
            key=lambda item: int(item.index),
        )
        for vert in verts
    }
    degrees = [len(neighbors[vert]) for vert in verts]
    if any(degree not in {1, 2} for degree in degrees) or degrees.count(1) not in {0, 2}:
        raise InvalidStateError(
            "edge_slide selected vertices must form one non-branching open or cyclic edge chain"
        )
    visited = set()
    pending = [min(verts, key=lambda vert: int(vert.index))]
    while pending:
        vert = pending.pop()
        if vert in visited:
            continue
        visited.add(vert)
        pending.extend(neighbor for neighbor in neighbors[vert] if neighbor not in visited)
    if visited != selected:
        raise InvalidStateError("edge_slide selected vertices must form one connected edge chain")
    return neighbors


def _op_edge_slide(_bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    """Slide a selected edge loop along an unambiguous connected side in local space."""
    _op_unknown(operation, {"op", "selection", "direction", "factor"})
    selection = _selection_if_present(bm, operation)
    if selection is not None and selection["domain"] != "VERT":
        raise InvalidParamsError("edge_slide selection must use the VERT domain")
    verts = _selected(bm, "VERT", "edge_slide")
    selected = set(verts)
    _slide_chain_neighbors(verts)

    from mathutils import Vector

    direction = Vector(_vector3(operation.get("direction"), "direction", -1.0, 1.0))
    if direction.length_squared <= 1e-20:
        raise InvalidParamsError("edge_slide direction must be non-zero")
    direction.normalize()
    factor = _number(operation.get("factor"), "factor", -0.999999, 0.999999)
    effective_direction = direction if factor >= 0.0 else -direction

    targets = []
    for vert in sorted(verts, key=lambda item: int(item.index)):
        candidates = sorted(
            {
                edge.other_vert(vert)
                for edge in vert.link_edges
                if edge.other_vert(vert) not in selected
            },
            key=lambda item: int(item.index),
        )
        if len(candidates) != 2:
            raise InvalidStateError(
                f"edge_slide vertex {int(vert.index)} must have exactly two unselected side neighbors"
            )
        scored = []
        for candidate in candidates:
            offset = candidate.co - vert.co
            if offset.length_squared <= 1e-20:
                raise InvalidStateError(
                    f"edge_slide vertex {int(vert.index)} has a zero-length side edge"
                )
            scored.append((float(offset.normalized().dot(effective_direction)), candidate))
        scored.sort(key=lambda item: (-item[0], int(item[1].index)))
        if scored[0][0] <= 1e-4 or scored[0][0] - scored[1][0] <= 1e-4:
            raise InvalidStateError(
                f"edge_slide direction is ambiguous at vertex {int(vert.index)}"
            )
        targets.append((vert, scored[0][1].co.copy()))

    if abs(factor) <= 1e-15:
        return 0, selection
    amount = abs(factor)
    for vert, target in targets:
        vert.co = vert.co.lerp(target, amount)
    return len(verts), selection


def _op_merge(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection"})
    selection = _selection_if_present(bm, operation)
    verts = _selected(bm, "VERT", "merge")
    if len(verts) < 2:
        raise InvalidStateError("merge requires at least two selected vertices")
    from mathutils import Vector

    center = sum((vert.co for vert in verts), Vector()) / len(verts)
    bmesh.ops.pointmerge(bm, verts=verts, merge_co=center)
    return len(verts), selection


def _op_delete(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection", "domain"})
    selection = _selection_if_present(bm, operation)
    domain = _enum(operation.get("domain"), "domain", {"VERT", "EDGE", "FACE"})
    geometry = _selected(bm, domain, "delete")
    context = {"VERT": "VERTS", "EDGE": "EDGES", "FACE": "FACES"}[domain]
    bmesh.ops.delete(bm, geom=geometry, context=context)
    return len(geometry), selection


def _op_recalculate_normals(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection", "all"})
    selection = _selection_if_present(bm, operation)
    use_all = _strict_bool(operation.get("all"), "all", False)
    faces = list(bm.faces) if use_all else _selected(bm, "FACE", "recalculate_normals")
    if not faces:
        raise InvalidStateError("recalculate_normals requires at least one face")
    bmesh.ops.recalc_face_normals(bm, faces=faces)
    return len(faces), selection


def _op_dissolve(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    domain = _enum(operation.get("domain"), "domain", {"VERT", "EDGE", "FACE"})
    common = {"op", "selection", "domain"}
    allowed = {
        "VERT": common | {"use_face_split", "use_boundary_tear"},
        "EDGE": common | {"use_verts", "use_face_split", "angle_threshold", "use_preserve_quads"},
        "FACE": common | {"use_verts"},
    }[domain]
    _op_unknown(operation, allowed)
    selection = _selection_if_present(bm, operation)
    geometry = _selected(bm, domain, "dissolve")
    if domain == "VERT":
        bmesh.ops.dissolve_verts(
            bm,
            verts=geometry,
            use_face_split=_strict_bool(operation.get("use_face_split"), "use_face_split", False),
            use_boundary_tear=_strict_bool(operation.get("use_boundary_tear"), "use_boundary_tear", False),
        )
    elif domain == "EDGE":
        bmesh.ops.dissolve_edges(
            bm,
            edges=geometry,
            use_verts=_strict_bool(operation.get("use_verts"), "use_verts", False),
            use_face_split=_strict_bool(operation.get("use_face_split"), "use_face_split", False),
            angle_threshold=_number(operation.get("angle_threshold", 0.0), "angle_threshold", 0.0, math.pi),
            use_preserve_quads=_strict_bool(operation.get("use_preserve_quads"), "use_preserve_quads", False),
        )
    else:
        bmesh.ops.dissolve_faces(
            bm,
            faces=geometry,
            use_verts=_strict_bool(operation.get("use_verts"), "use_verts", False),
        )
    return len(geometry), selection


def _select_result_geometry(bm: Any, geometry: list[Any]) -> None:
    _clear_selection(bm)
    for element in geometry:
        if getattr(element, "is_valid", True) and hasattr(element, "select_set"):
            element.select_set(True)


def _op_fill(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection", "sides"})
    selection = _selection_if_present(bm, operation)
    edges = _selected(bm, "EDGE", "fill")
    invalid = [edge for edge in edges if len(edge.link_faces) > 1]
    if invalid:
        raise InvalidStateError("fill requires boundary or loose edges with at most one linked face")
    result = bmesh.ops.holes_fill(
        bm,
        edges=edges,
        sides=_integer(operation.get("sides", 0), "sides", 0, 100_000),
    )
    faces = list(result.get("faces", ()))
    _select_result_geometry(bm, faces)
    return len(faces), selection


def _open_edge_chain_components(edges: list[Any]) -> list[list[Any]]:
    """Return connected selected-edge components after proving each is an open chain."""
    adjacency: dict[Any, list[Any]] = {}
    for edge in edges:
        for vert in edge.verts:
            adjacency.setdefault(vert, []).append(edge)
    if any(len(linked) not in {1, 2} for linked in adjacency.values()):
        raise InvalidStateError(
            "grid_fill selected edges must form non-branching open chains"
        )

    remaining = set(edges)
    components: list[list[Any]] = []
    while remaining:
        seed = remaining.pop()
        component = [seed]
        stack = [seed]
        while stack:
            current = stack.pop()
            for vert in current.verts:
                for neighbor in adjacency[vert]:
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        component.append(neighbor)
                        stack.append(neighbor)
        component_verts = {vert for edge in component for vert in edge.verts}
        endpoints = sum(1 for vert in component_verts if len(adjacency[vert]) == 1)
        if endpoints != 2:
            raise InvalidStateError(
                "grid_fill selected edges must form non-branching open chains"
            )
        components.append(component)
    return components


def _op_grid_fill(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    """Fill between two selected open boundary chains using unselected edges as rails."""
    _op_unknown(
        operation,
        {"op", "selection", "material_index", "use_smooth", "use_interp_simple"},
    )
    selection = _selection_if_present(bm, operation)
    if selection is not None and selection["domain"] != "EDGE":
        raise InvalidParamsError("grid_fill selection must use the EDGE domain")
    edges = _selected(bm, "EDGE", "grid_fill")
    if any(len(edge.link_faces) > 1 for edge in edges):
        raise InvalidStateError(
            "grid_fill requires boundary or loose edges with at most one linked face"
        )
    components = _open_edge_chain_components(edges)
    if len(components) != 2:
        raise InvalidStateError(
            "grid_fill requires exactly two disjoint, non-branching open edge chains"
        )
    result = bmesh.ops.grid_fill(
        bm,
        edges=edges,
        mat_nr=_integer(operation.get("material_index", 0), "material_index", 0, 32_767),
        use_smooth=_strict_bool(operation.get("use_smooth"), "use_smooth", False),
        use_interp_simple=_strict_bool(
            operation.get("use_interp_simple"), "use_interp_simple", False
        ),
    )
    faces = list(result.get("faces", ()))
    if not faces:
        raise InvalidStateError(
            "grid_fill could not resolve two compatible rails between the selected open chains"
        )
    _select_result_geometry(bm, faces)
    return len(faces), selection


def _op_bridge(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(
        operation,
        {"op", "selection", "use_pairs", "use_cyclic", "use_merge", "merge_factor", "twist_offset"},
    )
    selection = _selection_if_present(bm, operation)
    edges = _selected(bm, "EDGE", "bridge")
    result = bmesh.ops.bridge_loops(
        bm,
        edges=edges,
        use_pairs=_strict_bool(operation.get("use_pairs"), "use_pairs", False),
        use_cyclic=_strict_bool(operation.get("use_cyclic"), "use_cyclic", False),
        use_merge=_strict_bool(operation.get("use_merge"), "use_merge", False),
        merge_factor=_number(operation.get("merge_factor", 0.0), "merge_factor", 0.0, 1.0),
        twist_offset=_integer(operation.get("twist_offset", 0), "twist_offset", -100_000, 100_000),
    )
    created = list(result.get("faces", ())) + list(result.get("edges", ()))
    _select_result_geometry(bm, created)
    return len(created), selection


def _op_connect(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    """Split one shared face by connecting exactly two non-adjacent selected vertices."""
    _op_unknown(operation, {"op", "selection"})
    selection = _selection_if_present(bm, operation)
    if selection is not None and selection["domain"] != "VERT":
        raise InvalidParamsError("connect selection must use the VERT domain")
    verts = _selected(bm, "VERT", "connect")
    if len(verts) != 2:
        raise InvalidStateError("connect requires exactly two selected vertices")
    if bm.edges.get((verts[0], verts[1])) is not None:
        raise InvalidStateError("connect selected vertices already share an edge")
    shared_faces = set(verts[0].link_faces).intersection(verts[1].link_faces)
    if len(shared_faces) != 1:
        raise InvalidStateError(
            "connect requires two non-adjacent vertices that share exactly one face"
        )
    result = bmesh.ops.connect_vert_pair(bm, verts=verts)
    edges = list(result.get("edges", ()))
    if len(edges) != 1:
        raise InvalidStateError("connect did not produce exactly one edge")
    _select_result_geometry(bm, edges)
    return 1, selection


def _op_poke(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    """Create one center vertex per selected face and triangulate around it."""
    _op_unknown(
        operation,
        {"op", "selection", "offset", "center_mode", "use_relative_offset"},
    )
    selection = _selection_if_present(bm, operation)
    if selection is not None and selection["domain"] != "FACE":
        raise InvalidParamsError("poke selection must use the FACE domain")
    faces = _selected(bm, "FACE", "poke")
    result = bmesh.ops.poke(
        bm,
        faces=faces,
        offset=_number(operation.get("offset", 0.0), "offset", -1_000_000.0, 1_000_000.0),
        center_mode=_enum(
            operation.get("center_mode", "MEAN_WEIGHTED"),
            "center_mode",
            {"MEAN_WEIGHTED", "MEAN", "BOUNDS"},
        ),
        use_relative_offset=_strict_bool(
            operation.get("use_relative_offset"), "use_relative_offset", False
        ),
    )
    created_verts = list(result.get("verts", ()))
    created_faces = list(result.get("faces", ()))
    if len(created_verts) != len(faces) or not created_faces:
        raise InvalidStateError("poke produced an unexpected center-vertex or face count")
    created = created_verts + created_faces
    _select_result_geometry(bm, created)
    return len(created), selection


def _op_bisect(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(
        operation,
        {
            "op", "selection", "plane_co", "plane_no", "distance",
            "use_snap_center", "clear_outer", "clear_inner",
        },
    )
    selection = _selection_if_present(bm, operation)
    if selection is None:
        geometry = list(bm.verts) + list(bm.edges) + list(bm.faces)
    else:
        geometry = [
            element
            for sequence in (bm.verts, bm.edges, bm.faces)
            for element in sequence
            if element.select
        ]
    if not geometry:
        raise InvalidStateError("bisect requires selected geometry or a non-empty mesh")
    from mathutils import Vector

    plane_no = Vector(_vector3(operation.get("plane_no"), "plane_no", -1.0, 1.0))
    if plane_no.length_squared <= 1e-20:
        raise InvalidParamsError("plane_no must be non-zero")
    plane_no.normalize()
    clear_outer = _strict_bool(operation.get("clear_outer"), "clear_outer", False)
    clear_inner = _strict_bool(operation.get("clear_inner"), "clear_inner", False)
    if clear_outer and clear_inner:
        raise InvalidParamsError("clear_outer and clear_inner cannot both be true")
    result = bmesh.ops.bisect_plane(
        bm,
        geom=geometry,
        dist=_number(operation.get("distance", 1e-6), "distance", 0.0, 1_000_000.0),
        plane_co=Vector(_vector3(operation.get("plane_co"), "plane_co")),
        plane_no=plane_no,
        use_snap_center=_strict_bool(operation.get("use_snap_center"), "use_snap_center", False),
        clear_outer=clear_outer,
        clear_inner=clear_inner,
    )
    cut = list(result.get("geom_cut", ()))
    _select_result_geometry(bm, cut)
    return len(cut), selection


def _op_triangulate(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection", "quad_method", "ngon_method"})
    selection = _selection_if_present(bm, operation)
    faces = [face for face in _selected(bm, "FACE", "triangulate") if len(face.verts) > 3]
    if not faces:
        return 0, selection
    result = bmesh.ops.triangulate(
        bm,
        faces=faces,
        quad_method=_enum(
            operation.get("quad_method", "BEAUTY"),
            "quad_method",
            {"BEAUTY", "FIXED", "ALTERNATE", "SHORT_EDGE", "LONG_EDGE"},
        ),
        ngon_method=_enum(
            operation.get("ngon_method", "BEAUTY"),
            "ngon_method",
            {"BEAUTY", "EAR_CLIP"},
        ),
    )
    created = list(result.get("faces", ())) + list(result.get("edges", ()))
    _select_result_geometry(bm, created)
    return len(created), selection


def _op_symmetrize(bmesh: Any, bm: Any, operation: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    _op_unknown(operation, {"op", "selection", "direction", "distance"})
    selection = _selection_if_present(bm, operation)
    if selection is None:
        geometry = list(bm.verts) + list(bm.edges) + list(bm.faces)
    else:
        geometry = [
            element
            for sequence in (bm.verts, bm.edges, bm.faces)
            for element in sequence
            if element.select
        ]
    if not geometry:
        raise InvalidStateError("symmetrize requires selected geometry or a non-empty mesh")
    result = bmesh.ops.symmetrize(
        bm,
        input=geometry,
        direction=_enum(operation.get("direction"), "direction", {"-X", "X", "-Y", "Y", "-Z", "Z"}),
        dist=_number(operation.get("distance", 1e-4), "distance", 0.0, 1_000_000.0),
        use_shapekey=False,
    )
    output = list(result.get("geom", ()))
    _select_result_geometry(bm, output)
    return len(output), selection


def _spin_selected_geometry(
    bm: Any, selection: dict[str, Any] | None
) -> tuple[list[Any], list[Any], list[Any]]:
    domain = selection["domain"] if selection is not None else None
    if domain == "VERT":
        verts = [vert for vert in _domain_elements(bm, "VERT") if vert.select]
        return verts, [], []
    if domain == "EDGE":
        edges = [edge for edge in _domain_elements(bm, "EDGE") if edge.select]
        verts = {vert for edge in edges for vert in edge.verts}
        return sorted(verts, key=lambda vert: vert.index), edges, []
    if domain == "FACE":
        faces = [face for face in _domain_elements(bm, "FACE") if face.select]
        edges = {edge for face in faces for edge in face.edges}
        verts = {vert for face in faces for vert in face.verts}
        return (
            sorted(verts, key=lambda vert: vert.index),
            sorted(edges, key=lambda edge: edge.index),
            faces,
        )
    faces = [face for face in _domain_elements(bm, "FACE") if face.select]
    edges = {edge for edge in _domain_elements(bm, "EDGE") if edge.select}
    edges.update(edge for face in faces for edge in face.edges)
    verts = {vert for vert in _domain_elements(bm, "VERT") if vert.select}
    verts.update(vert for edge in edges for vert in edge.verts)
    verts.update(vert for face in faces for vert in face.verts)
    return (
        sorted(verts, key=lambda vert: vert.index),
        sorted(edges, key=lambda edge: edge.index),
        faces,
    )


def _op_spin(
    bmesh: Any,
    bm: Any,
    operation: dict[str, Any],
    budget: int,
) -> tuple[int, dict[str, Any] | None, dict[str, Any]]:
    """Extrude or duplicate selected geometry around a local axis with a bounded screw offset."""
    _op_unknown(
        operation,
        {
            "op", "selection", "center", "axis", "angle", "steps", "screw_offset",
            "use_duplicate", "use_merge", "normal_flip", "axis_merge_distance",
        },
    )
    selection = _selection_if_present(bm, operation)
    verts, edges, faces = _spin_selected_geometry(bm, selection)
    geometry = [*verts, *edges, *faces]
    if not geometry or not verts:
        raise InvalidStateError("spin requires selected mesh geometry with at least one vertex")

    from mathutils import Matrix, Vector

    center = Vector(_vector3(operation.get("center", [0.0, 0.0, 0.0]), "center"))
    axis = Vector(_vector3(operation.get("axis", [0.0, 0.0, 1.0]), "axis", -1.0, 1.0))
    if axis.length_squared <= 1e-20:
        raise InvalidParamsError("spin axis must be non-zero")
    axis.normalize()
    angle = _number(
        operation.get("angle"), "angle", -math.tau * 1000.0, math.tau * 1000.0
    )
    if abs(angle) <= 1e-8:
        raise InvalidParamsError("spin angle must be non-zero")
    steps = _integer(operation.get("steps"), "steps", 1, 10_000)
    screw_offset = _number(
        operation.get("screw_offset", 0.0),
        "screw_offset",
        -1_000_000.0,
        1_000_000.0,
    )
    use_duplicate = _strict_bool(operation.get("use_duplicate"), "use_duplicate", False)
    use_merge = _strict_bool(operation.get("use_merge"), "use_merge", False)
    normal_flip = _strict_bool(operation.get("normal_flip"), "normal_flip", False)
    axis_merge_distance = _number(
        operation.get("axis_merge_distance", 1e-6),
        "axis_merge_distance",
        0.0,
        1.0,
    )
    if use_merge:
        if use_duplicate:
            raise InvalidParamsError("spin use_merge cannot be combined with use_duplicate")
        if steps < 3:
            raise InvalidParamsError("spin use_merge requires at least 3 steps")
        if abs(screw_offset) > 1e-12:
            raise InvalidParamsError("spin use_merge requires zero screw_offset")
        if not math.isclose(abs(angle), math.tau, rel_tol=1e-9, abs_tol=1e-6):
            raise InvalidParamsError("spin use_merge requires exactly one full revolution")
        angle = math.copysign(math.tau, angle)
        screw_offset = 0.0

    selected_counts = {
        "vertices": len(verts),
        "edges": len(edges),
        "faces": len(faces),
        "loops": sum(len(face.loops) for face in faces),
    }
    projection = _spin_preflight(
        _bm_counts(bm), selected_counts, steps, use_duplicate, budget
    )
    before_counts = _bm_counts(bm)
    before_verts = set(bm.verts)
    per_step_translation = axis * (screw_offset / steps)
    result = bmesh.ops.spin(
        bm,
        geom=geometry,
        cent=center,
        axis=axis,
        dvec=per_step_translation,
        angle=angle,
        space=Matrix.Identity(4),
        steps=steps,
        use_merge=use_merge,
        use_normal_flip=normal_flip,
        use_duplicate=use_duplicate,
    )
    last_geometry = list(result.get("geom_last", ()))
    axis_welded_vertices = 0
    if not use_duplicate and abs(screw_offset) <= 1e-12:
        candidates = []
        for vert in [*verts, *(vert for vert in bm.verts if vert not in before_verts)]:
            delta = vert.co - center
            radial = delta - axis * delta.dot(axis)
            if radial.length <= axis_merge_distance:
                candidates.append(vert)
        if len(candidates) > 1:
            verts_before_weld = len(bm.verts)
            bmesh.ops.remove_doubles(bm, verts=candidates, dist=axis_merge_distance)
            axis_welded_vertices = verts_before_weld - len(bm.verts)
            last_geometry.extend(
                vert for vert in candidates if getattr(vert, "is_valid", False)
            )
    _select_result_geometry(bm, last_geometry)
    after_counts = _bm_counts(bm)
    affected = sum(
        max(0, after_counts[key] - before_counts[key])
        for key in ("vertices", "edges", "faces")
    )
    return affected, selection, {
        "mode": "SCREW" if abs(screw_offset) > 1e-12 else "SPIN",
        "center": [float(value) for value in center],
        "axis": [float(value) for value in axis],
        "angle": angle,
        "steps": steps,
        "screw_offset": screw_offset,
        "per_step_translation": [float(value) for value in per_step_translation],
        "use_duplicate": use_duplicate,
        "use_merge": use_merge,
        "normal_flip": normal_flip,
        "axis_merge_distance": axis_merge_distance,
        "axis_welded_vertices": axis_welded_vertices,
        "selected_counts": selected_counts,
        "projected_total_upper_bound": projection["projected_total"],
    }


def _apply_operation(
    bmesh: Any, bm: Any, operation: dict[str, Any], budget: int
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    kind = operation["op"]
    if kind == "select":
        _op_unknown(operation, {"op", "domain", "action", "predicate"})
        return 0, _apply_selection(
            bm,
            {name: operation[name] for name in ("domain", "action", "predicate") if name in operation},
        ), None
    if kind == "spin":
        return _op_spin(bmesh, bm, operation, budget)
    affected, selection = {
        "transform": _op_transform,
        "extrude": _op_extrude,
        "inset": _op_inset,
        "bevel": _op_bevel,
        "subdivide": _op_subdivide,
        "loop_cut": _op_loop_cut,
        "edge_slide": _op_edge_slide,
        "merge": _op_merge,
        "delete": _op_delete,
        "recalculate_normals": _op_recalculate_normals,
        "dissolve": _op_dissolve,
        "fill": _op_fill,
        "grid_fill": _op_grid_fill,
        "bridge": _op_bridge,
        "connect": _op_connect,
        "poke": _op_poke,
        "bisect": _op_bisect,
        "triangulate": _op_triangulate,
        "symmetrize": _op_symmetrize,
        "align": _op_align,
        "distribute": _op_distribute,
        "snap": _op_snap,
        "workplane": _op_workplane,
        "mark_edges": _op_mark_edges,
    }[kind](bmesh, bm, operation)
    return affected, selection, None


def _validate_bmesh(bm: Any, budget: int) -> dict[str, int]:
    counts = _bm_counts(bm)
    _ensure_budget(counts, budget, "Result")
    for vert in bm.verts:
        if not all(math.isfinite(float(value)) for value in vert.co):
            raise InvalidStateError("Mesh operation produced a non-finite vertex coordinate")
    bm.normal_update()
    return counts


def _space(value: Any) -> str:
    return _enum(value if value is not None else "LOCAL", "space", {"LOCAL", "WORLD"})


def _point_in_space(obj: Any, coordinate: Any, space: str) -> Any:
    return obj.matrix_world @ coordinate if space == "WORLD" else coordinate.copy()


def _edge_length(obj: Any, edge: Any, space: str) -> float:
    start = _point_in_space(obj, edge.verts[0].co, space)
    end = _point_in_space(obj, edge.verts[1].co, space)
    return float((end - start).length)


def _face_area(obj: Any, face: Any, space: str) -> float:
    if space == "LOCAL":
        return float(face.calc_area())
    points = [_point_in_space(obj, vert.co, space) for vert in face.verts]
    if len(points) < 3:
        return 0.0
    from mathutils.geometry import tessellate_polygon

    triangles = tessellate_polygon([points])
    area = 0.0
    for triangle in triangles:
        resolved = [points[item] if isinstance(item, int) else item for item in triangle]
        area += float((resolved[1] - resolved[0]).cross(resolved[2] - resolved[0]).length) * 0.5
    return area


def _aggregate(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "total": 0.0, "minimum": None, "maximum": None, "mean": None}
    total = math.fsum(values)
    return {
        "count": len(values),
        "total": total,
        "minimum": min(values),
        "maximum": max(values),
        "mean": total / len(values),
    }


def _query_elements(bm: Any, domain: str, query: dict[str, Any]) -> list[Any]:
    predicate = query.get("predicate", {"kind": "ALL"})
    return _predicate_elements(bm, domain, predicate)


def _measure_query(obj: Any, bm: Any, query: dict[str, Any], index: int) -> dict[str, Any]:
    kind = query["type"]
    result: dict[str, Any] = {"query_index": index, "type": kind}
    if kind == "VERTEX_DISTANCE":
        _query_unknown(query, {"type", "vertices", "space"})
        vertices = _domain_elements(bm, "VERT")
        if len(vertices) < 2:
            raise InvalidStateError("VERTEX_DISTANCE requires a mesh with at least two vertices")
        indices = _index_list(
            query.get("vertices"),
            "vertices",
            len(vertices) - 1,
            minimum_items=2,
            maximum_items=2,
        )
        resolved_space = _space(query.get("space"))
        points = [_point_in_space(obj, vertices[item].co, resolved_space) for item in indices]
        result.update(
            vertices=indices,
            space=resolved_space,
            points=[[float(value) for value in point] for point in points],
            distance=float((points[1] - points[0]).length),
        )
        return result
    if kind == "ANGLE":
        _query_unknown(query, {"type", "vertices", "space"})
        vertices = _domain_elements(bm, "VERT")
        if len(vertices) < 3:
            raise InvalidStateError("ANGLE requires a mesh with at least three vertices")
        indices = _index_list(
            query.get("vertices"),
            "vertices",
            len(vertices) - 1,
            minimum_items=3,
            maximum_items=3,
        )
        resolved_space = _space(query.get("space"))
        points = [_point_in_space(obj, vertices[item].co, resolved_space) for item in indices]
        first = points[0] - points[1]
        second = points[2] - points[1]
        if first.length_squared <= 1e-20 or second.length_squared <= 1e-20:
            raise InvalidStateError("ANGLE requires non-coincident vertex coordinates")
        cosine = max(-1.0, min(1.0, float(first.normalized().dot(second.normalized()))))
        radians = math.acos(cosine)
        result.update(
            vertices=indices,
            space=resolved_space,
            radians=radians,
            degrees=math.degrees(radians),
        )
        return result
    if kind == "EDGE_LENGTH":
        _query_unknown(query, {"type", "predicate", "space"})
        resolved_space = _space(query.get("space"))
        edges = _query_elements(bm, "EDGE", query)
        result.update(space=resolved_space, **_aggregate([_edge_length(obj, edge, resolved_space) for edge in edges]))
        return result
    if kind == "FACE_AREA":
        _query_unknown(query, {"type", "predicate", "space"})
        resolved_space = _space(query.get("space"))
        faces = _query_elements(bm, "FACE", query)
        result.update(space=resolved_space, **_aggregate([_face_area(obj, face, resolved_space) for face in faces]))
        return result
    if kind == "SELECTION_BOUNDS":
        _query_unknown(query, {"type", "domain", "predicate", "space"})
        domain = _enum(query.get("domain"), "domain", {"VERT", "EDGE", "FACE"})
        resolved_space = _space(query.get("space"))
        elements = _query_elements(bm, domain, query)
        vertices = set()
        if domain == "VERT":
            vertices.update(elements)
        else:
            for element in elements:
                vertices.update(element.verts)
        points = [_point_in_space(obj, vert.co, resolved_space) for vert in vertices]
        bounds = None
        if points:
            minimum = [min(float(point[axis]) for point in points) for axis in range(3)]
            maximum = [max(float(point[axis]) for point in points) for axis in range(3)]
            bounds = {
                "min": minimum,
                "max": maximum,
                "dimensions": [maximum[axis] - minimum[axis] for axis in range(3)],
                "center": [(maximum[axis] + minimum[axis]) * 0.5 for axis in range(3)],
            }
        result.update(
            domain=domain,
            space=resolved_space,
            matched_elements=len(elements),
            vertex_count=len(vertices),
            bounds=bounds,
        )
        return result
    _query_unknown(query, {"type", "space", "signed", "require_manifold"})
    resolved_space = _space(query.get("space"))
    signed = _strict_bool(query.get("signed"), "signed", False)
    require_manifold = _strict_bool(query.get("require_manifold"), "require_manifold", True)
    manifold = bool(bm.faces) and all(len(edge.link_faces) == 2 for edge in bm.edges)
    if require_manifold and not manifold:
        raise InvalidStateError("VOLUME requires a closed manifold mesh unless require_manifold is false")
    volume = float(bm.calc_volume(signed=signed))
    if resolved_space == "WORLD":
        determinant = float(obj.matrix_world.to_3x3().determinant())
        volume *= determinant if signed else abs(determinant)
    result.update(space=resolved_space, signed=signed, manifold=manifold, volume=volume)
    return result


def measure_mesh(params: dict[str, Any]) -> ToolResult:
    _unknown(params, {"object_name", "queries", "max_elements"})
    object_name = _name(params.get("object_name"), "object_name")
    queries = _queries(params.get("queries"))
    max_elements = _integer(
        params.get("max_elements", DEFAULT_ELEMENT_BUDGET),
        "max_elements",
        1,
        MAX_ELEMENT_BUDGET,
    )
    bpy = get_bpy()
    obj = _mesh_object(bpy, object_name)
    if getattr(obj, "mode", "OBJECT") == "EDIT":
        raise InvalidStateError("mesh.measure requires the target mesh object outside Edit mode")
    mesh = obj.data
    direct_counts = {
        "vertices": len(mesh.vertices),
        "edges": len(mesh.edges),
        "faces": len(mesh.polygons),
        "loops": len(mesh.loops),
    }
    _ensure_budget(direct_counts, max_elements, "Source")
    import bmesh

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.normal_update()
        counts = _bm_counts(bm)
        _ensure_budget(counts, max_elements, "Source")
        results = [_measure_query(obj, bm, query, index) for index, query in enumerate(queries)]
        return ToolResult(
            data={
                "object": obj.name,
                "mesh": mesh.name,
                "counts": counts,
                "query_count": len(results),
                "results": results,
                "max_elements": max_elements,
                "state_token": current_state_token(bpy),
            }
        )
    finally:
        bm.free()


def edit_mesh(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"object_name", "operations", "max_elements", "dry_run", "confirm", "expected_state",
                      "fragment_small_ratio"})
    object_name = _name(params.get("object_name"), "object_name")
    operations = _operations(params.get("operations"))
    max_elements = _integer(
        params.get("max_elements", DEFAULT_ELEMENT_BUDGET),
        "max_elements",
        1,
        MAX_ELEMENT_BUDGET,
    )
    _strict_bool(params.get("dry_run"), "dry_run", True)
    if "confirm" in params:
        _strict_bool(params["confirm"], "confirm")
    small_ratio = fragment_small_ratio(params.get("fragment_small_ratio", 0.01))
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, object_name)
    mesh = obj.data
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("mesh.edit requires Blender Object mode")
    if mesh.shape_keys is not None:
        raise InvalidStateError("mesh.edit refuses meshes with Shape Keys because topology/channel synchronization is not yet supported")
    if mesh.users > 1:
        raise InvalidStateError("mesh.edit refuses shared mesh data; make the object data single-user first")
    import bmesh

    bm = bmesh.new()
    backup = None
    transaction = None
    try:
        direct_counts = {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "faces": len(mesh.polygons),
            "loops": len(mesh.loops),
        }
        _ensure_budget(direct_counts, max_elements, "Source")
        bm.from_mesh(mesh)
        before_counts = _bm_counts(bm)
        _ensure_budget(before_counts, max_elements, "Source")
        _clear_selection(bm)
        journal = []
        changed = False
        for index, operation in enumerate(operations):
            counts_before = _bm_counts(bm)
            affected, selection, details = _apply_operation(
                bmesh, bm, operation, max_elements
            )
            counts_after = _bm_counts(bm)
            kind = operation["op"]
            operation_changed = kind != "select" and (affected > 0 or counts_before != counts_after)
            changed = changed or operation_changed
            entry = {
                "op": kind,
                "operation_index": index,
                "affected": affected,
                "selection": selection,
                "before": counts_before,
                "after": counts_after,
                "status": "planned" if operation_changed else "evaluated",
            }
            if details is not None:
                entry["details"] = details
            journal.append(entry)
            _ensure_budget(counts_after, max_elements, f"Operation {index}")
        after_counts = _validate_bmesh(bm, max_elements)
        destructive = any(operation["op"] in DESTRUCTIVE_KINDS for operation in operations)
        transaction, apply = prepare_destructive_action(
            action="mesh.edit",
            affected_datablocks=[{"type": "Object", "name": obj.name}, {"type": "Mesh", "name": mesh.name}],
            journal=journal,
            dry_run=params.get("dry_run"),
            confirm=params.get("confirm"),
            require_confirm=destructive,
            state_token=current_state_token(bpy),
            extra={
                "object": obj.name,
                "mesh": mesh.name,
                "before": before_counts,
                "after": after_counts,
                "max_elements": max_elements,
                "destructive_confirmation_required": destructive,
                "recovery": "Save a checkpoint before applying topology-changing operation chains.",
            },
        )
        if not apply or not changed:
            return transaction
        require_document_write_access(bpy)
        check_expected_state(params.get("expected_state"), bpy)
        before_components = capture_component_summary(transaction, mesh, obj, small_ratio)
        backup = mesh.copy()
        _clear_selection(bm)
        try:
            bm.to_mesh(mesh)
            mesh.update(calc_edges=True, calc_edges_loose=True)
        except Exception as exc:
            rollback_errors = []
            try:
                restore = bmesh.new()
                try:
                    restore.from_mesh(backup)
                    restore.to_mesh(mesh)
                    mesh.update(calc_edges=True, calc_edges_loose=True)
                finally:
                    restore.free()
            except Exception as rollback_exc:
                rollback_errors.append(f"restore mesh data: {rollback_exc}")
            transaction["changed"] = bool(rollback_errors)
            transaction["warnings"].extend(rollback_errors)
            if rollback_errors:
                bump_epoch()
            transaction["state_token"] = current_state_token(bpy)
            raise MutationApplyError(f"mesh.edit writeback failed: {exc}", transaction) from exc
        for entry in journal:
            if entry["status"] == "planned":
                entry["status"] = "applied"
        transaction["changed"] = True
        append_new_islands(transaction, before_components, mesh, obj, small_ratio)
        bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        return transaction
    finally:
        bm.free()
        if backup is not None and bpy.data.meshes.get(backup.name) is backup:
            try:
                bpy.data.meshes.remove(backup)
            except Exception as cleanup_exc:
                if transaction is not None:
                    transaction["warnings"].append(f"remove mesh.edit backup {backup.name}: {cleanup_exc}")
