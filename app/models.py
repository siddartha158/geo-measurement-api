"""Database models: one UploadedFile has many Features."""
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _new_id() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(16))  # SHAPEFILE | KML
    status: Mapped[str] = mapped_column(String(16), default="PROCESSING")  # PROCESSING|COMPLETED|FAILED
    crs: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    feature_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    features: Mapped[list["Feature"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", order_by="Feature.feature_index"
    )


class Feature(Base):
    __tablename__ = "features"

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("uploaded_files.id", ondelete="CASCADE"), index=True)
    feature_index: Mapped[int] = mapped_column(Integer)  # 0-based position inside the file
    geometry_type: Mapped[str] = mapped_column(String(64))
    geometry: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)  # GeoJSON, source CRS
    crs: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # MEASURED | NOT_REQUIRED | UNSUPPORTED | SKIPPED | ERROR
    measurement_status: Mapped[str] = mapped_column(String(16))
    measurements: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    projected_crs: Mapped[Optional[dict[str, str]]] = mapped_column(JSON, nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    file: Mapped[UploadedFile] = relationship(back_populates="features")
