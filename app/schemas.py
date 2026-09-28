"""Pydantic request/response models for the microplate arrangement API."""
from __future__ import annotations

import math
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Categories reported in 422 responses when no layout can exist.
# They identify the class of constraint that makes the instance infeasible.
VIOLATION_SAMPLE_IDS_DUPLICATED = "SAMPLE_IDS_DUPLICATED"
VIOLATION_GROUP_BATCH_INVALID = "GROUP_BATCH_INVALID"
VIOLATION_PLATE_DIMENSION_INVALID = "PLATE_DIMENSION_INVALID"
VIOLATION_WELL_REFERENCE_INVALID = "WELL_REFERENCE_INVALID"
VIOLATION_FIXED_CONFLICT = "FIXED_CONFLICT"
VIOLATION_FIXED_ON_BLOCKED = "FIXED_ON_BLOCKED"
VIOLATION_CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
VIOLATION_SOLVER_INFEASIBLE = "SOLVER_INFEASIBLE"

# Categories reported in 422 responses of the reconcile endpoint when the
# target layout and the scanned plate state are inconsistent.
VIOLATION_TARGET_WELLS_DUPLICATED = "TARGET_WELLS_DUPLICATED"
VIOLATION_TARGET_ON_BLOCKED = "TARGET_ON_BLOCKED"
VIOLATION_SCAN_WELLS_DUPLICATED = "SCAN_WELLS_DUPLICATED"
VIOLATION_SCAN_SAMPLES_DUPLICATED = "SCAN_SAMPLES_DUPLICATED"
VIOLATION_SAMPLES_MISSING = "SAMPLES_MISSING"
VIOLATION_UNPLANNED_SAMPLES = "UNPLANNED_SAMPLES"
VIOLATION_BLOCKED_WELL_OCCUPIED = "BLOCKED_WELL_OCCUPIED"

# Categories reported in 422 responses of the spatial QC endpoint.
VIOLATION_SPATIAL_SAMPLE_DUPLICATED = "SPATIAL_SAMPLE_DUPLICATED"
VIOLATION_SPATIAL_SAMPLE_UNKNOWN = "SPATIAL_SAMPLE_UNKNOWN"
VIOLATION_SPATIAL_LAYOUT_CONFLICT = "SPATIAL_LAYOUT_CONFLICT"
VIOLATION_SPATIAL_READING_INVALID = "SPATIAL_READING_INVALID"
VIOLATION_SPATIAL_READING_DUPLICATED = "SPATIAL_READING_DUPLICATED"
VIOLATION_SPATIAL_GROUP_INSUFFICIENT = "SPATIAL_GROUP_INSUFFICIENT"
VIOLATION_SPATIAL_GRID_INSUFFICIENT = "SPATIAL_GRID_INSUFFICIENT"
VIOLATION_SPATIAL_NOT_CONVERGED = "SPATIAL_NOT_CONVERGED"


class PlateSpec(BaseModel):
    """Plate geometry.

    ``type`` selects a standard footprint ("24"/"48"/"96"/"384") or
    "custom", in which case ``rows`` and ``columns`` must be supplied.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["24", "48", "96", "384", "custom"] = "96"
    rows: Optional[int] = Field(default=None, ge=1, le=26)
    columns: Optional[int] = Field(default=None, ge=1, le=50)


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    group: str = Field(min_length=1, description="实验组标识")
    batch: str = Field(min_length=1, description="批次标识")


class FixedAssignment(BaseModel):
    """A sample pinned to a specific well; the solver may never move it."""

    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(min_length=1)
    well: str = Field(min_length=1, description="孔位,如 'C5'")


class ArrangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    samples: list[Sample]
    fixed: list[FixedAssignment] = Field(default_factory=list)
    blocked_wells: list[str] = Field(default_factory=list)
    seed: int = Field(default=0, ge=0)

    @field_validator("samples")
    @classmethod
    def _nonempty_samples(cls, v: list[Sample]) -> list[Sample]:
        if not v:
            raise ValueError("samples must contain at least one sample")
        return v


class ReconcileRequest(BaseModel):
    """Reconcile a physically scanned plate against a planned layout.

    ``target`` is the sample → well mapping exactly as returned in the
    ``layout`` field of ``POST /api/v1/arrangements``. ``scan`` is the
    observed well → sample mapping from a full plate scan; empty wells are
    simply omitted. ``staging_well`` optionally names a free, non-target,
    non-blocked well that may be borrowed to break closed-loop
    misplacements; it is restored to empty by the final instruction.
    """

    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    target: dict[str, str] = Field(
        min_length=1, description="目标映射:样本 id → 目标孔位(原编排接口的 layout)"
    )
    scan: dict[str, str] = Field(
        default_factory=dict, description="扫描映射:孔位 → 样本 id(空孔不列出)"
    )
    blocked_wells: list[str] = Field(default_factory=list)
    staging_well: Optional[str] = Field(
        default=None, description="空闲且非目标的暂存孔,用于解开闭环错位"
    )


class SpatialSample(BaseModel):
    """A sample carrying an experiment group label for spatial QC."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    group: str = Field(min_length=1, description="实验组标识;中位数中心化与置换均在组内进行")


class SpatialQCRequest(BaseModel):
    """Spatial QC request.

    ``layout`` is the sample → well mapping exactly as returned in the
    ``layout`` field of ``POST /api/v1/arrangements``. ``readings`` maps each
    well to its numeric assay value; wells may be omitted or mapped to null
    to mark missing readings. Group medians are removed first, then row and
    column medians are stripped and accumulated by alternating median polish
    sweeps. An optional seed-controlled within-group permutation test yields
    an empirical, plus-one-corrected p-value for the maximal row/column
    effect.
    """

    model_config = ConfigDict(extra="forbid")

    plate: PlateSpec = Field(default_factory=PlateSpec)
    layout: dict[str, str] = Field(
        min_length=1, description="布局映射:样本 id → 孔位(编排接口响应中的 layout)"
    )
    samples: list[SpatialSample] = Field(min_length=1)
    readings: dict[str, Optional[float]] = Field(
        default_factory=dict,
        description="孔位 → 数值读数;省略孔位或显式 null 表示读数缺失",
    )
    seed: int = Field(default=0, ge=0, description="随机种子(≥0),控制组内置换")
    permutations: int = Field(
        default=1000, ge=0, le=1_000_000, description="组内置换次数(≥0,0 表示跳过置换)"
    )
    tolerance: float = Field(
        default=1e-6, gt=0, description="收敛阈值:本轮最大行列校正量 ≤ tolerance 即收敛"
    )
    max_iterations: int = Field(
        default=1000, ge=1, description="行列交替剥离的最大轮数"
    )

    @field_validator("tolerance")
    @classmethod
    def _tolerance_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("tolerance must be a finite number")
        return v

    @field_validator("readings")
    @classmethod
    def _readings_finite(cls, v: dict[str, Optional[float]]) -> dict[str, Optional[float]]:
        for well, value in v.items():
            if value is not None and not math.isfinite(value):
                raise ValueError(f"reading at well '{well}' must be a finite number or null")
        return v
