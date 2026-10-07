"""Orchestrates: parse -> measure -> persist."""
import logging
from typing import Optional

from shapely.geometry import mapping
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Feature, UploadedFile
from app.services.measurements import UNSUPPORTED, Measurer, MeasurementResult
from app.services.parsers import ParseError, ParsedFile, parse_kml, parse_shapefile_zip

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".zip": "SHAPEFILE", ".kml": "KML"}


def _parse(file_type: str, content: bytes) -> ParsedFile:
    if file_type == "SHAPEFILE":
        return parse_shapefile_zip(content, settings.max_uncompressed_bytes)
    return parse_kml(content)


def _mark_failed(db: Session, file_id: str, message: str) -> UploadedFile:
    db.rollback()
    record = db.get(UploadedFile, file_id)
    record.status = "FAILED"
    record.error_message = message
    db.commit()
    return record


def process_upload(db: Session, filename: str, extension: str, content: bytes) -> UploadedFile:
    file_type = SUPPORTED_EXTENSIONS[extension]
    record = UploadedFile(filename=filename, file_type=file_type, status="PROCESSING")
    db.add(record)
    db.commit()
    file_id = record.id

    try:
        parsed = _parse(file_type, content)
        measurer = Measurer(parsed.crs)

        rows = []
        for pf in parsed.features:
            if pf.geometry is None:
                result: Optional[MeasurementResult] = MeasurementResult(UNSUPPORTED, message=pf.error)
            else:
                result = measurer.measure(pf.geometry)
            rows.append(
                Feature(
                    file_id=file_id,
                    feature_index=pf.index,
                    geometry_type=pf.geometry_type,
                    geometry=mapping(pf.geometry) if pf.geometry is not None else None,
                    crs=parsed.crs_label,
                    properties=pf.properties,
                    measurement_status=result.status,
                    measurements=result.measurements,
                    projected_crs=result.projected_crs,
                    message=result.message,
                )
            )

        record.crs = parsed.crs_label
        record.feature_count = len(rows)
        record.status = "COMPLETED"
        db.add_all(rows)
        db.commit()
        return record
    except ParseError as exc:
        return _mark_failed(db, file_id, str(exc))
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error while processing file %s", file_id)
        return _mark_failed(db, file_id, "Unexpected error while processing the file.")
