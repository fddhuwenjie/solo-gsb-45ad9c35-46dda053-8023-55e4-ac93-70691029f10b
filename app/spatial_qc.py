"""Spatial-effect quality control for a plate of numeric readings.

Pipeline:

1. **Group centering.** Every valid reading has its sample ``group``'s median
   subtracted, so genuine between-group differences cannot leak into the
   spatial terms (避免组间差异混入空间效应).
2. **Median polish.** Row medians are stripped and accumulated into a row
   effect, then column medians into a column effect; each sweep recentres
   the accumulated effects (the swept medians are folded into the overall
   effect) so that only one of the many row/column decompositions is
   reported. Sweeps repeat until the largest correction applied during the
   round is no greater than ``tolerance`` or ``max_iterations`` is reached.
3. **Permutation test.** Within every group the valid readings are shuffled
   over that group's wells (Fisher's exact-style spatial randomization,
   seeded for reproducibility); each permutation runs the identical
   centering + polish procedure. The statistic is the largest absolute
   row/column effect, and the p-value is the empirical, plus-one-corrected
   proportion ``(1 + #{T* >= T}) / (1 + B)``.

Missing readings (omitted wells or explicit ``null``) are simply skipped;
they never participate in a median. No partial result is returned when any
group carries fewer than two valid readings, the valid wells fail to cover
at least two rows *and* two columns, or the polish does not converge
(including during a permutation repeat).
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field

from .planner import InfeasibleArrangement, _resolve_dimensions
from .plates import parse_well, well_name
from .schemas import (
    VIOLATION_GROUP_MEMBERSHIP_INVALID,
    VIOLATION_LAYOUT_WELLS_DUPLICATED,
    VIOLATION_READING_COVERAGE_INSUFFICIENT,
    VIOLATION_READING_GROUP_INSUFFICIENT,
    VIOLATION_READING_MALFORMED,
    VIOLATION_SPATIAL_QC_DID_NOT_CONVERGE,
    VIOLATION_WELL_REFERENCE_INVALID,
    SpatialQCRequest,
)


class SpatialQCInvalid(Exception):
    """The spatial-QC request is invalid or the algorithm did not converge.

    Maps to HTTP 422; no partial conclusion is ever produced.
    """

    def __init__(self, categories: list[str], messages: list[str]):
        self.categories = categories
        self.messages = messages
        super().__init__("; ".join(messages) if messages else "invalid spatial-QC request")


@dataclass
class PolishResult:
    overall: float
    row_effects: list[float]
    col_effects: list[float]
    residuals: dict[tuple[int, int], float]
    iterations: int
    converged: bool
    trajectory: list[dict] = field(default_factory=list)

    @property
    def max_axis_effect(self) -> float:
        """Statistic T: largest absolute row/column effect."""
        return max(
            [abs(v) for v in self.row_effects] + [abs(v) for v in self.col_effects],
            default=0.0,
        )


def _median(values: list[float]) -> float:
    return float(statistics.median(values))


def _prepare(req: SpatialQCRequest, rows: int, cols: int):
    """Validate layout/groups/readings and build the per-well working table."""
    categories: list[str] = []
    messages: list[str] = []

    def add(category: str, msg: str) -> None:
        if category not in categories:
            categories.append(category)
        messages.append(msg)

    # --- layout (sample -> well) ------------------------------------------
    well_owner: dict[tuple[int, int], str] = {}
    sample_pos: dict[str, tuple[int, int]] = {}
    for sid, raw in req.layout.items():
        try:
            pos = parse_well(raw, rows, cols)
        except ValueError as exc:
            add(VIOLATION_WELL_REFERENCE_INVALID, str(exc))
            continue
        if pos in well_owner:
            add(
                VIOLATION_LAYOUT_WELLS_DUPLICATED,
                f"well {well_name(*pos)} assigned to both "
                f"'{well_owner[pos]}' and '{sid}'",
            )
        else:
            well_owner[pos] = sid
        sample_pos[sid] = pos

    # --- group membership (sample -> group) --------------------------------
    group_of_well: dict[tuple[int, int], str] = {}
    for sid, group in req.groups.items():
        if not group or not group.strip():
            add(VIOLATION_GROUP_MEMBERSHIP_INVALID, f"sample '{sid}' has an empty group")
            continue
        pos = sample_pos.get(sid)
        if pos is None and sid not in req.layout:
            add(
                VIOLATION_GROUP_MEMBERSHIP_INVALID,
                f"group membership references sample '{sid}' absent from layout",
            )
    for sid, pos in sample_pos.items():
        group = req.groups.get(sid)
        if group is None:
            add(
                VIOLATION_GROUP_MEMBERSHIP_INVALID,
                f"layout sample '{sid}' has no group membership",
            )
        elif group.strip():
            group_of_well[pos] = group

    # --- readings (well -> float|null) -------------------------------------
    raw_at: dict[tuple[int, int], float] = {}
    for raw_well, value in req.readings.items():
        try:
            pos = parse_well(raw_well, rows, cols)
        except ValueError as exc:
            add(VIOLATION_WELL_REFERENCE_INVALID, str(exc))
            continue
        if pos not in well_owner:
            add(
                VIOLATION_READING_MALFORMED,
                f"reading at {well_name(*pos)} does not correspond to a layout well",
            )
            continue
        if value is None:
            continue  # explicitly missing reading
        try:
            fvalue = float(value)
        except (TypeError, ValueError):
            add(
                VIOLATION_READING_MALFORMED,
                f"reading at {well_name(*pos)} is not numeric",
            )
            continue
        if not math.isfinite(fvalue):
            add(
                VIOLATION_READING_MALFORMED,
                f"reading at {well_name(*pos)} is not finite (got {value})",
            )
            continue
        raw_at[pos] = fvalue

    if categories:
        raise SpatialQCInvalid(categories, messages)

    # Valid wells = layout wells with a group and a present, finite reading.
    valid_wells = sorted(pos for pos in well_owner if pos in raw_at and pos in group_of_well)

    # Per-group minimum of two valid readings.
    members: dict[str, list[tuple[int, int]]] = {}
    for pos in valid_wells:
        members.setdefault(group_of_well[pos], []).append(pos)
    for group in sorted(members):
        if len(members[group]) < 2:
            add(
                VIOLATION_READING_GROUP_INSUFFICIENT,
                f"group '{group}' has only {len(members[group])} valid reading(s); at least 2 required",
            )
    if not members:
        add(VIOLATION_READING_GROUP_INSUFFICIENT, "no group has any valid reading")

    # Coverage: valid wells must span at least two rows and two columns.
    covered_rows = {r for r, _ in valid_wells}
    covered_cols = {c for _, c in valid_wells}
    if len(covered_rows) < 2 or len(covered_cols) < 2:
        add(
            VIOLATION_READING_COVERAGE_INSUFFICIENT,
            f"valid wells cover {len(covered_rows)} row(s) and {len(covered_cols)} "
            f"column(s); at least 2 of each are required",
        )

    if categories:
        raise SpatialQCInvalid(categories, messages)

    return well_owner, group_of_well, raw_at, valid_wells, members


def median_polish(values: dict[tuple[int, int], float],
                  rows: int, cols: int,
                  tolerance: float, max_iterations: int) -> PolishResult:
    """Centre-by-medians decomposition with alternating row/column sweeps.

    ``values`` maps the valid wells to group-centred readings. Each round
    strips row medians into the row effect, column medians into the column
    effect, and recentres both so the swept location lives in the overall
    effect. The round's correction is the largest absolute term applied;
    convergence is ``max_correction <= tolerance``.

    The hot loop runs over a flat array with precomputed row/column index
    groups (permutation repeats call this hundreds of times); the public
    inputs and residuals stay keyed by well coordinates.
    """
    positions = sorted(values)
    index_of = {pos: i for i, pos in enumerate(positions)}
    work = [values[pos] for pos in positions]
    row_groups: list[list[int]] = [[] for _ in range(rows)]
    col_groups: list[list[int]] = [[] for _ in range(cols)]
    for i, (r, c) in enumerate(positions):
        row_groups[r].append(i)
        col_groups[c].append(i)

    overall = 0.0
    row_eff = [0.0] * rows
    col_eff = [0.0] * cols
    active_rows = [r for r, grp in enumerate(row_groups) if grp]
    active_cols = [c for c, grp in enumerate(col_groups) if grp]
    trajectory: list[dict] = []
    converged = False

    for iteration in range(1, max_iterations + 1):
        # --- row sweep -----------------------------------------------------
        max_strip = 0.0
        for r in active_rows:
            med = _median([work[i] for i in row_groups[r]])
            if abs(med) > max_strip:
                max_strip = abs(med)
            for i in row_groups[r]:
                work[i] -= med
            row_eff[r] += med
        row_recentre = _median([row_eff[r] for r in active_rows])
        for r in active_rows:
            row_eff[r] -= row_recentre
        overall += row_recentre

        # --- column sweep --------------------------------------------------
        col_strip_max = 0.0
        for c in active_cols:
            med = _median([work[i] for i in col_groups[c]])
            if abs(med) > col_strip_max:
                col_strip_max = abs(med)
            for i in col_groups[c]:
                work[i] -= med
            col_eff[c] += med
        col_recentre = _median([col_eff[c] for c in active_cols])
        for c in active_cols:
            col_eff[c] -= col_recentre
        overall += col_recentre

        max_correction = max(
            max_strip, col_strip_max, abs(row_recentre), abs(col_recentre)
        )
        trajectory.append(
            {
                "iteration": iteration,
                "max_correction": round(max_correction, 12),
                "row_recentre": round(row_recentre, 12),
                "col_recentre": round(col_recentre, 12),
                "overall_effect": round(overall, 12),
                "max_row_effect": round(max((abs(row_eff[r]) for r in active_rows), default=0.0), 12),
                "max_col_effect": round(max((abs(col_eff[c]) for c in active_cols), default=0.0), 12),
            }
        )
        if max_correction <= tolerance:
            converged = True
            break

    residuals = {pos: work[index_of[pos]] for pos in positions}
    return PolishResult(
        overall=overall,
        row_effects=row_eff,
        col_effects=col_eff,
        residuals=residuals,
        iterations=len(trajectory),
        converged=converged,
        trajectory=trajectory,
    )


def _centre(raw_at: dict[tuple[int, int], float],
            members: dict[str, list[tuple[int, int]]]) -> tuple[
                dict[tuple[int, int], float], dict[str, float]]:
    """Subtract each group's median from its members' readings."""
    group_median: dict[str, float] = {}
    centred: dict[tuple[int, int], float] = {}
    for group, wells in members.items():
        med = _median([raw_at[p] for p in wells])
        group_median[group] = med
        for p in wells:
            centred[p] = raw_at[p] - med
    return centred, group_median


def _polish_layout(work: list[float], row_groups: list[list[int]],
                   col_groups: list[list[int]], active_rows: list[int],
                   active_cols: list[int],
                   tolerance: float, max_iterations: int) -> tuple[float, bool]:
    """Trajectory-free polish on a precomputed flat layout (permutation hot
    path). Returns (max absolute row/column effect, converged).

    Identical algorithm to :func:`median_polish`: the same row/column
    medians are stripped and accumulated, with the same recentring and
    ``max_correction <= tolerance`` stopping rule.
    """
    nrows, ncols = len(row_groups), len(col_groups)
    row_eff = [0.0] * nrows
    col_eff = [0.0] * ncols
    for _ in range(max_iterations):
        max_strip = 0.0
        for r in active_rows:
            grp = row_groups[r]
            med = _median([work[i] for i in grp])
            if abs(med) > max_strip:
                max_strip = abs(med)
            for i in grp:
                work[i] -= med
            row_eff[r] += med
        row_recentre = _median([row_eff[r] for r in active_rows])
        for r in active_rows:
            row_eff[r] -= row_recentre

        col_strip_max = 0.0
        for c in active_cols:
            grp = col_groups[c]
            med = _median([work[i] for i in grp])
            if abs(med) > col_strip_max:
                col_strip_max = abs(med)
            for i in grp:
                work[i] -= med
            col_eff[c] += med
        col_recentre = _median([col_eff[c] for c in active_cols])
        for c in active_cols:
            col_eff[c] -= col_recentre

        if max(max_strip, col_strip_max, abs(row_recentre), abs(col_recentre)) <= tolerance:
            stat = max(
                (abs(row_eff[r]) for r in active_rows),
                default=0.0,
            )
            for c in active_cols:
                v = abs(col_eff[c])
                if v > stat:
                    stat = v
            return stat, True

    stat = max((abs(row_eff[r]) for r in active_rows), default=0.0)
    for c in active_cols:
        v = abs(col_eff[c])
        if v > stat:
            stat = v
    return stat, False


def _flat_layout(raw_at: dict[tuple[int, int], float],
                 members: dict[str, list[tuple[int, int]]],
                 rows: int, cols: int):
    """Precompute the shared flat arrays used by every polish run."""
    positions = sorted(raw_at)
    flat_index = {pos: i for i, pos in enumerate(positions)}
    row_groups: list[list[int]] = [[] for _ in range(rows)]
    col_groups: list[list[int]] = [[] for _ in range(cols)]
    for i, (r, c) in enumerate(positions):
        row_groups[r].append(i)
        col_groups[c].append(i)
    active_rows = [r for r, grp in enumerate(row_groups) if grp]
    active_cols = [c for c, grp in enumerate(col_groups) if grp]
    member_flat = {
        group: [flat_index[p] for p in wells] for group, wells in members.items()
    }
    base_values = [raw_at[p] for p in positions]
    return (row_groups, col_groups, active_rows, active_cols,
            member_flat, base_values, flat_index)


def spatial_qc(req: SpatialQCRequest) -> dict:
    """Validate, polish, permute and assemble the spatial-QC response."""
    try:
        rows, cols = _resolve_dimensions(req.plate)
    except InfeasibleArrangement as exc:
        raise SpatialQCInvalid(exc.categories, exc.messages) from exc

    well_owner, group_of_well, raw_at, valid_wells, members = _prepare(req, rows, cols)

    centred, group_median = _centre(raw_at, members)
    polish = median_polish(centred, rows, cols, req.tolerance, req.max_iterations)
    if not polish.converged:
        raise SpatialQCInvalid(
            [VIOLATION_SPATIAL_QC_DID_NOT_CONVERGE],
            [
                f"median polish did not converge within {req.max_iterations} iterations "
                f"(last max correction {polish.trajectory[-1]['max_correction']} > tolerance {req.tolerance})"
            ],
        )

    # --- within-group well permutations ------------------------------------
    # Reuse one flat layout across all repeats: groups index into the same
    # value vector, so each permutation only shuffles values in place.
    (row_groups, col_groups, active_rows, active_cols,
     member_flat, base_values, _) = _flat_layout(raw_at, members, rows, cols)

    rng = random.Random(int(req.seed))
    observed_stat = round(polish.max_axis_effect, 10)
    exceedances = 0
    perm_stats: list[float] = []
    for _ in range(req.n_permutations):
        shuffled = base_values[:]
        for idxs in member_flat.values():
            vals = [shuffled[i] for i in idxs]
            rng.shuffle(vals)
            for i, v in zip(idxs, vals):
                shuffled[i] = v
        # Group-median centring on the shuffled vector.
        for idxs in member_flat.values():
            med = _median([shuffled[i] for i in idxs])
            for i in idxs:
                shuffled[i] -= med
        stat, converged = _polish_layout(
            shuffled, row_groups, col_groups, active_rows, active_cols,
            req.tolerance, req.max_iterations,
        )
        if not converged:
            raise SpatialQCInvalid(
                [VIOLATION_SPATIAL_QC_DID_NOT_CONVERGE],
                [
                    f"median polish did not converge on a permutation repeat within "
                    f"{req.max_iterations} iterations; no partial conclusion is returned"
                ],
            )
        # Compare at the same 10-decimal precision reported to the caller, so
        # floating-point noise cannot flip the tie rule (T* >= T).
        stat = round(stat, 10)
        perm_stats.append(stat)
        if stat >= observed_stat:
            exceedances += 1

    denom = req.n_permutations + 1
    p_value = (exceedances + 1) / denom

    # --- response assembly --------------------------------------------------
    # Corrected readings keep the group level: reading - overall - row - col.
    corrected: dict[str, float | None] = {}
    for pos in sorted(well_owner):
        if pos in polish.residuals:
            corrected[well_name(*pos)] = round(
                group_median[group_of_well[pos]] + polish.residuals[pos], 10
            )
        else:
            corrected[well_name(*pos)] = None

    group_rows = {g: sorted({r for r, _ in ws}) for g, ws in members.items()}
    group_cols = {g: sorted({c for _, c in ws}) for g, ws in members.items()}
    groups_summary = [
        {
            "group": g,
            "valid_readings": len(members[g]),
            "median": round(group_median[g], 10),
            "covered_rows": [well_name(r, 0)[0] for r in group_rows[g]],
            "covered_columns": [c + 1 for c in group_cols[g]],
        }
        for g in sorted(members)
    ]

    return {
        "seed": req.seed,
        "plate": {
            "type": req.plate.type,
            "rows": rows,
            "columns": cols,
        },
        "valid_wells": [well_name(*p) for p in valid_wells],
        "groups": groups_summary,
        "effects": {
            "overall_effect": round(polish.overall, 10),
            "row_effects": [round(v, 10) for v in polish.row_effects],
            "col_effects": [round(v, 10) for v in polish.col_effects],
            "max_row_effect": round(max((abs(v) for v in polish.row_effects), default=0.0), 10),
            "max_col_effect": round(max((abs(v) for v in polish.col_effects), default=0.0), 10),
            "max_axis_effect": round(observed_stat, 10),
            "row_labels": [well_name(r, 0)[0] for r in range(rows)],
            "column_labels": [c + 1 for c in range(cols)],
        },
        "corrected_readings": corrected,
        "convergence": {
            "converged": True,
            "iterations": polish.iterations,
            "max_iterations": req.max_iterations,
            "tolerance": req.tolerance,
            "final_max_correction": polish.trajectory[-1]["max_correction"],
            "trajectory": polish.trajectory,
        },
        "permutation_test": {
            "n_permutations": req.n_permutations,
            "statistic": "max(abs(row_effects) U abs(col_effects))",
            "observed_statistic": round(observed_stat, 10),
            "permutation_statistics": [round(s, 10) for s in perm_stats],
            "exceedances": exceedances,
            "p_value": round(p_value, 6),
            "p_value_method": "plus-one empirical: (1 + #{T* >= T}) / (1 + n_permutations)",
        },
    }
