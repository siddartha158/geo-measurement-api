"""Pydantic response schemas."""
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator


class FileInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    file_type: str
    feature_count: int
    crs: Optional[str]
    status: str
    error_message: Optional[str] = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _assume_utc(cls, value: datetime) -> datetime:
        # SQLite drops tzinfo on read; timestamps are always stored in UTC.
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class FeatureOut(BaseModel):
    feature_id: int
    geometry_type: str
    geometry: Optional[dict[str, Any]]
    crs: Optional[str]
    properties: dict[str, Any]


class FeaturesResponse(BaseModel):
    file_id: str
    total: int
    limit: int
    offset: int
    features: list[FeatureOut]


class MeasurementItem(BaseModel):
    feature_id: int
    geometry_type: str
    status: str
    measurements: dict[str, float]
    projected_crs: Optional[dict[str, str]] = None
    message: Optional[str] = None


class MeasurementSummary(BaseModel):
    total_area_sq_m: float
    total_length_m: float
    status_counts: dict[str, int]


class MeasurementsResponse(BaseModel):
    file_id: str
    crs: Optional[str]
    feature_count: int
    summary: MeasurementSummary
    limit: int
    offset: int
    measurements: list[MeasurementItem]
