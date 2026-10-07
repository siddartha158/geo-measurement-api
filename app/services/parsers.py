"""Readers that turn an uploaded file into a list of features.

Both readers are pure Python (pyshp + defusedxml + shapely), so no GDAL install is needed.
Every feature is parsed independently: a bad feature is reported, it never aborts the file.
"""
from __future__ import annotations

import codecs
import datetime
import decimal
import io
import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Optional

import shapefile  # pyshp
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely import force_2d
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    shape,
)
from shapely.geometry.base import BaseGeometry


class ParseError(Exception):
    """The whole file is unreadable (bad zip, missing .shp, malformed XML, ...)."""


@dataclass
class ParsedFeature:
    index: int
    geometry: Optional[BaseGeometry]
    geometry_type: str
    properties: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None  # set when the geometry could not be read


@dataclass
class ParsedFile:
    file_type: str
    crs: Optional[CRS]
    crs_label: Optional[str]
    features: list[ParsedFeature]


# --------------------------------------------------------------------------- helpers
def describe_crs(crs: CRS) -> str:
    epsg = crs.to_epsg()
    return f"EPSG:{epsg}" if epsg else (crs.name or "UNKNOWN")


def json_safe(value: Any) -> Any:
    """Convert DBF/XML values to something JSON can store."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    return str(value)


# --------------------------------------------------------------------------- shapefile
def _resolve_encoding(raw: Optional[bytes]) -> str:
    if not raw:
        return "utf-8"
    name = raw.decode("ascii", "ignore").strip()
    for candidate in (name, f"cp{name}"):
        try:
            return codecs.lookup(candidate).name
        except LookupError:
            continue
    return "utf-8"


def parse_shapefile_zip(content: bytes, max_uncompressed_bytes: int) -> ParsedFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ParseError("The uploaded file is not a valid .zip archive.") from exc

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > max_uncompressed_bytes:
            raise ParseError("The zip archive is too large when uncompressed.")

        # Files are read straight from memory, never extracted to disk (no zip-slip risk).
        members: dict[tuple[str, str], zipfile.ZipInfo] = {}
        for info in infos:
            path = PurePosixPath(info.filename.replace("\\", "/"))
            if "__MACOSX" in path.parts or path.name.startswith("._"):
                continue
            members[(path.with_suffix("").as_posix().lower(), path.suffix.lower())] = info

        shp_keys = sorted(k for k in members if k[1] == ".shp")
        if not shp_keys:
            raise ParseError("No .shp file found inside the zip archive.")
        if len(shp_keys) > 1:
            raise ParseError("The zip contains more than one shapefile; please upload one at a time.")
        base = shp_keys[0][0]

        def read(ext: str) -> Optional[bytes]:
            info = members.get((base, ext))
            return zf.read(info) if info else None

        shp_bytes, dbf_bytes = read(".shp"), read(".dbf")
        if dbf_bytes is None:
            raise ParseError("The shapefile is missing its .dbf attribute table.")
        shx_bytes, prj_bytes, cpg_bytes = read(".shx"), read(".prj"), read(".cpg")

    crs: Optional[CRS] = None
    if prj_bytes:
        try:
            crs = CRS.from_wkt(prj_bytes.decode("utf-8", "replace").strip())
        except CRSError:
            crs = None  # unreadable .prj is treated as "CRS undefined"

    reader_kwargs: dict[str, Any] = {
        "shp": io.BytesIO(shp_bytes),
        "dbf": io.BytesIO(dbf_bytes),
        "encoding": _resolve_encoding(cpg_bytes),
        "encodingErrors": "replace",
    }
    if shx_bytes:
        reader_kwargs["shx"] = io.BytesIO(shx_bytes)

    features: list[ParsedFeature] = []
    try:
        with shapefile.Reader(**reader_kwargs) as reader:
            for index, shape_record in enumerate(reader.iterShapeRecords()):
                features.append(_shapefile_feature(index, shape_record))
    except shapefile.ShapefileException as exc:
        raise ParseError(f"Could not read shapefile: {exc}") from exc

    return ParsedFile("SHAPEFILE", crs, describe_crs(crs) if crs else None, features)


def _shapefile_feature(index: int, shape_record: Any) -> ParsedFeature:
    shp = shape_record.shape
    properties = json_safe(shape_record.record.as_dict())
    type_name = shp.shapeTypeName  # e.g. POLYGON, POINTZ, MULTIPATCH

    if shp.shapeType == shapefile.NULL:
        return ParsedFeature(index, None, "NULL", properties, "Feature has a null shape.")
    try:
        geometry = force_2d(shape(shp.__geo_interface__))
        return ParsedFeature(index, geometry, geometry.geom_type, properties)
    except Exception as exc:  # noqa: BLE001 - isolate one bad feature from the rest
        return ParsedFeature(index, None, type_name, properties, f"Unsupported or invalid geometry: {exc}")


# --------------------------------------------------------------------------- KML
_GEOMETRY_TAGS = {"Point", "LineString", "LinearRing", "Polygon", "MultiGeometry"}
_UNSUPPORTED_GEOMETRY_TAGS = {"Track", "MultiTrack", "Model"}


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(el: Any, name: str) -> list[Any]:
    return [c for c in el if _local(c.tag) == name]


def _parse_coordinates(text: Optional[str]) -> list[tuple[float, float]]:
    """KML coordinates are 'lon,lat[,alt]' tuples separated by whitespace."""
    normalised = re.sub(r"\s*,\s*", ",", (text or "").strip())
    points: list[tuple[float, float]] = []
    for token in normalised.split():
        parts = token.split(",")
        if len(parts) < 2:
            raise ValueError(f"Invalid coordinate tuple: {token!r}")
        lon, lat = float(parts[0]), float(parts[1])
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise ValueError(f"Non-finite coordinate: {token!r}")
        points.append((lon, lat))
    return points


def _coordinates_of(el: Any) -> list[tuple[float, float]]:
    coords = _children(el, "coordinates")
    if not coords:
        raise ValueError(f"<{_local(el.tag)}> has no <coordinates>")
    return _parse_coordinates(coords[0].text)


def _ring_of(boundary: Any) -> list[tuple[float, float]]:
    rings = _children(boundary, "LinearRing")
    if not rings:
        raise ValueError("Polygon boundary has no <LinearRing>")
    return _coordinates_of(rings[0])


def _build_geometry(el: Any) -> BaseGeometry:
    name = _local(el.tag)
    if name == "Point":
        pts = _coordinates_of(el)
        if len(pts) != 1:
            raise ValueError("Point must have exactly one coordinate")
        return Point(pts[0])
    if name in ("LineString", "LinearRing"):
        return LineString(_coordinates_of(el))
    if name == "Polygon":
        outer = _children(el, "outerBoundaryIs")
        if not outer:
            raise ValueError("Polygon has no <outerBoundaryIs>")
        holes = [_ring_of(b) for b in _children(el, "innerBoundaryIs")]
        return Polygon(_ring_of(outer[0]), holes)
    if name == "MultiGeometry":
        parts = [_build_geometry(c) for c in el if _local(c.tag) in _GEOMETRY_TAGS]
        if not parts:
            raise ValueError("MultiGeometry contains no supported geometries")
        if len(parts) == 1:
            return parts[0]
        kinds = {p.geom_type for p in parts}
        if kinds == {"Polygon"}:
            return MultiPolygon(parts)
        if kinds == {"LineString"}:
            return MultiLineString(parts)
        if kinds == {"Point"}:
            return MultiPoint(parts)
        return GeometryCollection(parts)
    raise ValueError(f"Unsupported KML geometry <{name}>")


def _placemark_properties(placemark: Any) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for key in ("name", "description"):
        found = _children(placemark, key)
        if found and found[0].text and found[0].text.strip():
            props[key] = found[0].text.strip()
    for ext in _children(placemark, "ExtendedData"):
        for node in ext.iter():
            tag = _local(node.tag)
            if tag == "Data" and node.get("name"):
                value = _children(node, "value")
                props[node.get("name")] = value[0].text.strip() if value and value[0].text else None
            elif tag == "SimpleData" and node.get("name"):
                props[node.get("name")] = node.text.strip() if node.text else None
    return props


def parse_kml(content: bytes) -> ParsedFile:
    try:
        root = SafeET.fromstring(content)
    except (SafeET.ParseError, DefusedXmlException) as exc:
        raise ParseError(f"Invalid or unsafe KML/XML: {exc}") from exc
    if _local(root.tag).lower() != "kml":
        raise ParseError("The XML document is not a KML file (root element is not <kml>).")

    features: list[ParsedFeature] = []
    placemarks = [el for el in root.iter() if _local(el.tag) == "Placemark"]
    for index, placemark in enumerate(placemarks):
        props = json_safe(_placemark_properties(placemark))
        geometry_els = [c for c in placemark if _local(c.tag) in _GEOMETRY_TAGS]
        if not geometry_els:
            other = next((_local(c.tag) for c in placemark if _local(c.tag) in _UNSUPPORTED_GEOMETRY_TAGS), None)
            features.append(
                ParsedFeature(index, None, other or "None", props, "Placemark has no supported geometry.")
            )
            continue
        try:
            geometry = _build_geometry(geometry_els[0])
            features.append(ParsedFeature(index, geometry, geometry.geom_type, props))
        except Exception as exc:  # noqa: BLE001 - isolate one bad placemark
            features.append(
                ParsedFeature(index, None, _local(geometry_els[0].tag), props, f"Invalid geometry: {exc}")
            )

    # KML coordinates are always WGS84 longitude/latitude by specification.
    crs = CRS.from_epsg(4326)
    return ParsedFile("KML", crs, "EPSG:4326", features)
