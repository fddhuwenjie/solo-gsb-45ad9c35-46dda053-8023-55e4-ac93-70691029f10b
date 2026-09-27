"""Pydantic request/response models for the microplate arrangement API."""
from __future__ import annotations

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
