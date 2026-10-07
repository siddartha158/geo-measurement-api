"""Area / length calculation.

Rule: never measure in degrees. Every geometry is first brought to WGS84 lon/lat, then projected
to a *local* metric CRS chosen from the feature's own bounding-box centre:

* area   -> Lambert Azimuthal Equal-Area centred on the feature (equal-area => exact area)
* length -> the UTM zone of the feature (conformal, scale error <= ~0.04 %); polar fallback = UPS
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from pyproj import CRS, Transformer
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from shapely.validation import explain_validity

WGS84 = CRS.from_epsg(4326)

MEASURED = "MEASURED"
NOT_REQUIRED = "NOT_REQUIRED"
UNSUPPORTED = "UNSUPPORTED"
SKIPPED = "SKIPPED"
ERROR = "ERROR"


@dataclass
class MeasurementResult:
    status: str
    measurements: dict[str, float] = field(default_factory=dict)
    projected_crs: Optional[dict[str, str]] = None
    message: Optional[str] = None


def _flatten(geom: BaseGeometry) -> Iterator[BaseGeometry]:
    if hasattr(geom, "geoms"):
        for part in geom.geoms:
            yield from _flatten(part)
    else:
        yield geom


def utm_epsg(lon: float, lat: float) -> int:
    if lat >= 84:
        return 32661  # WGS 84 / UPS North
    if lat <= -80:
        return 32761  # WGS 84 / UPS South
    zone = min(max(int((lon + 180) // 6) + 1, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


class Measurer:
    """One instance per uploaded file (holds the source CRS and per-request transformer caches)."""

    def __init__(self, source_crs: Optional[CRS]):
        self._crs = source_crs
        self._to_wgs84: Optional[Transformer] = None
        if source_crs is not None and source_crs.to_epsg() != 4326:
            self._to_wgs84 = Transformer.from_crs(source_crs, WGS84, always_xy=True)
        self._laea_cache: dict[tuple[float, float], Transformer] = {}
        self._utm_cache: dict[int, Transformer] = {}

    # -- projections ------------------------------------------------------------------------
    def _laea(self, lon: float, lat: float) -> Transformer:
        key = (lon, lat)
        if key not in self._laea_cache:
            target = CRS.from_proj4(
                f"+proj=laea +lat_0={lat} +lon_0={lon} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
            )
            self._laea_cache[key] = Transformer.from_crs(WGS84, target, always_xy=True)
        return self._laea_cache[key]

    def _utm(self, epsg: int) -> Transformer:
        if epsg not in self._utm_cache:
            self._utm_cache[epsg] = Transformer.from_crs(WGS84, CRS.from_epsg(epsg), always_xy=True)
        return self._utm_cache[epsg]

    # -- public API -------------------------------------------------------------------------
    def measure(self, geom: Optional[BaseGeometry]) -> MeasurementResult:
        if geom is None or geom.is_empty:
            return MeasurementResult(UNSUPPORTED, message="Empty or missing geometry.")

        parts = list(_flatten(geom))
        has_poly = any(p.geom_type == "Polygon" for p in parts)
        has_line = any(p.geom_type in ("LineString", "LinearRing") for p in parts)

        if not has_poly and not has_line:
            if all(p.geom_type == "Point" for p in parts):
                return MeasurementResult(NOT_REQUIRED, message="No measurement is defined for points.")
            return MeasurementResult(UNSUPPORTED, message=f"Geometry type {geom.geom_type} is not supported.")

        if self._crs is None:
            return MeasurementResult(
                SKIPPED, message="Source CRS is undefined (missing/unreadable .prj); cannot measure safely."
            )

        try:
            geom_ll = geom if self._to_wgs84 is None else shapely_transform(self._to_wgs84.transform, geom)
            minx, miny, maxx, maxy = geom_ll.bounds
            if not all(map(math.isfinite, (minx, miny, maxx, maxy))) or not (
                -180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90
            ):
                return MeasurementResult(
                    ERROR, message="Coordinates fall outside valid lon/lat ranges; the file's CRS is probably wrong."
                )

            lon, lat = round((minx + maxx) / 2, 2), round((miny + maxy) / 2, 2)
            laea, utm_code = self._laea(lon, lat), utm_epsg(lon, lat)
            utm = self._utm(utm_code)
            ll_parts = list(_flatten(geom_ll))

            measurements: dict[str, float] = {}
            used: dict[str, str] = {}
            if has_poly:
                polys = [p for p in ll_parts if p.geom_type == "Polygon"]
                area = sum(shapely_transform(laea.transform, p).area for p in polys)
                perimeter = sum(shapely_transform(utm.transform, p).length for p in polys)
                measurements.update(
                    area_sq_m=round(area, 3), area_hectares=round(area / 10_000, 6), perimeter_m=round(perimeter, 3)
                )
                used["area"] = f"LAEA(lat_0={lat}, lon_0={lon})"
            if has_line:
                lines = [p for p in ll_parts if p.geom_type in ("LineString", "LinearRing")]
                length = sum(shapely_transform(utm.transform, p).length for p in lines)
                measurements["length_m"] = round(length, 3)
            used["length"] = f"EPSG:{utm_code}"

            message: Optional[str] = None
            if not geom_ll.is_valid:
                message = f"Geometry is invalid ({explain_validity(geom_ll)}); results may be inaccurate."
            return MeasurementResult(MEASURED, measurements, used, message)
        except Exception as exc:  # noqa: BLE001 - never crash the whole file for one feature
            return MeasurementResult(ERROR, message=f"Measurement failed: {exc}")
