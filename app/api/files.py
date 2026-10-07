"""HTTP endpoints under /api/files/."""
from pathlib import PurePath

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Feature, UploadedFile
from app.schemas import (
    FeatureOut,
    FeaturesResponse,
    FileInfo,
    MeasurementItem,
    MeasurementsResponse,
    MeasurementSummary,
)
from app.services.processor import SUPPORTED_EXTENSIONS, process_upload

router = APIRouter(prefix="/api/files", tags=["files"])


def _read_limited(upload: UploadFile) -> bytes:
    chunks, size = [], 0
    while chunk := upload.file.read(1024 * 1024):
        size += len(chunk)
        if size > settings.max_upload_bytes:
            raise HTTPException(413, f"File exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def _get_file_or_404(db: Session, file_id: str) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:
        raise HTTPException(404, "File not found.")
    return record


# Sync (def) endpoints run in FastAPI's threadpool, so CPU-bound parsing doesn't block the event loop.
@router.post("/", response_model=FileInfo, status_code=201)
def upload_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload a `.zip` (containing a Shapefile) or a `.kml`; it is parsed and measured immediately."""
    filename = PurePath(file.filename or "").name
    extension = PurePath(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, "Unsupported file type. Upload a .zip (Shapefile) or a .kml file.")

    content = _read_limited(file)
    if not content:
        raise HTTPException(400, "The uploaded file is empty.")

    record = process_upload(db, filename, extension, content)
    if record.status == "FAILED":
        return JSONResponse(
            status_code=422,
            content={"detail": record.error_message, "file": jsonable_encoder(FileInfo.model_validate(record))},
        )
    return record


@router.get("/{file_id}/", response_model=FileInfo)
def get_file(file_id: str, db: Session = Depends(get_db)):
    return _get_file_or_404(db, file_id)


@router.get("/{file_id}/features/", response_model=FeaturesResponse)
def get_features(
    file_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    record = _get_file_or_404(db, file_id)
    rows = db.scalars(
        select(Feature)
        .where(Feature.file_id == file_id)
        .order_by(Feature.feature_index)
        .limit(limit)
        .offset(offset)
    ).all()
    return FeaturesResponse(
        file_id=file_id,
        total=record.feature_count,
        limit=limit,
        offset=offset,
        features=[
            FeatureOut(
                feature_id=r.feature_index,
                geometry_type=r.geometry_type,
                geometry=r.geometry,
                crs=r.crs,
                properties=r.properties,
            )
            for r in rows
        ],
    )


@router.get("/{file_id}/measurements/", response_model=MeasurementsResponse)
def get_measurements(
    file_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    record = _get_file_or_404(db, file_id)
    if record.status != "COMPLETED":
        raise HTTPException(409, f"File is {record.status}; measurements are not available.")

    # Summary over the whole file (not just the requested page).
    status_counts = dict(
        db.execute(
            select(Feature.measurement_status, func.count()).where(Feature.file_id == file_id).group_by(
                Feature.measurement_status
            )
        ).all()
    )
    total_area = total_length = 0.0
    for (m,) in db.execute(select(Feature.measurements).where(Feature.file_id == file_id)):
        total_area += m.get("area_sq_m", 0.0)
        total_length += m.get("length_m", 0.0)

    rows = db.scalars(
        select(Feature)
        .where(Feature.file_id == file_id)
        .order_by(Feature.feature_index)
        .limit(limit)
        .offset(offset)
    ).all()
    return MeasurementsResponse(
        file_id=file_id,
        crs=record.crs,
        feature_count=record.feature_count,
        summary=MeasurementSummary(
            total_area_sq_m=round(total_area, 3),
            total_length_m=round(total_length, 3),
            status_counts=status_counts,
        ),
        limit=limit,
        offset=offset,
        measurements=[
            MeasurementItem(
                feature_id=r.feature_index,
                geometry_type=r.geometry_type,
                status=r.measurement_status,
                measurements=r.measurements,
                projected_crs=r.projected_crs,
                message=r.message,
            )
            for r in rows
        ],
    )
