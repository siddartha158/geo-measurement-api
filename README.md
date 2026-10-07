# Geospatial File Measurement API

A FastAPI backend that accepts a **zipped Shapefile** or a **KML** file, extracts every feature
(ID, geometry type, geometry, CRS, properties) and computes **area** (polygons) and **length**
(lines) in metres — never in degrees.

## Setup

Requirements: Python 3.10+ (developed/tested on 3.12). No GDAL install needed — all parsing is pure Python.

```bash
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# macOS / Linux:         source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

* API: http://127.0.0.1:8000  ·  Interactive docs (Swagger): http://127.0.0.1:8000/docs
* Run tests: `python -m pytest -q`
* Sample files: `samples/sample.kml`; generate a shapefile zip with `python scripts/make_sample_shapefile.py`

Optional environment variables: `DATABASE_URL` (default: SQLite file `geo_api.db`),
`MAX_UPLOAD_MB` (50), `MAX_UNCOMPRESSED_MB` (200).

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/files/` | Upload + process a `.zip` (Shapefile) or `.kml` (multipart field `file`) |
| GET | `/api/files/{id}/` | File info |
| GET | `/api/files/{id}/features/?limit=&offset=` | Feature ID, geometry type, GeoJSON geometry, CRS, properties |
| GET | `/api/files/{id}/measurements/?limit=&offset=` | Per-feature measurements + file-wide summary |
| GET | `/health` | Liveness check |

### Upload
```bash
curl -X POST http://127.0.0.1:8000/api/files/ -F "file=@samples/sample.kml"
```
```json
{
  "id": "e782926139de41799164e724add6e10d",
  "filename": "sample.kml",
  "file_type": "KML",
  "feature_count": 5,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error_message": null,
  "created_at": "2026-10-07T08:44:36.789680Z"
}
```
Errors: `400` unsupported/empty file · `413` too large · `422` file could not be parsed
(body contains `detail` and the stored `file` record with `status: "FAILED"`) · `404` unknown id ·
`409` measurements requested for a file that is not `COMPLETED`.

### Measurements
`GET /api/files/{id}/measurements/` (output trimmed)
```json
{
  "file_id": "e782926139de41799164e724add6e10d",
  "crs": "EPSG:4326",
  "feature_count": 5,
  "summary": {
    "total_area_sq_m": 4713668.292,
    "total_length_m": 3072.04,
    "status_counts": {"MEASURED": 3, "NOT_REQUIRED": 1, "UNSUPPORTED": 1}
  },
  "limit": 100,
  "offset": 0,
  "measurements": [
    {
      "feature_id": 0, "geometry_type": "Polygon", "status": "MEASURED",
      "measurements": {"area_sq_m": 1178440.097, "area_hectares": 117.84401, "perimeter_m": 4343.756},
      "projected_crs": {"area": "LAEA(lat_0=17.01, lon_0=77.0)", "length": "EPSG:32643"},
      "message": null
    },
    {
      "feature_id": 2, "geometry_type": "LineString", "status": "MEASURED",
      "measurements": {"length_m": 3072.04},
      "projected_crs": {"length": "EPSG:32643"}, "message": null
    },
    {"feature_id": 3, "geometry_type": "Point", "status": "NOT_REQUIRED", "measurements": {},
     "projected_crs": null, "message": "No measurement is defined for points."},
    {"feature_id": 4, "geometry_type": "None", "status": "UNSUPPORTED", "measurements": {},
     "projected_crs": null, "message": "Placemark has no supported geometry."}
  ]
}
```
Per-feature `status`: `MEASURED`, `NOT_REQUIRED` (points), `UNSUPPORTED` (empty/unknown geometry),
`SKIPPED` (CRS undefined), `ERROR` (measurement failed). One bad feature never fails the whole file.

## Architecture

```
app/
  main.py               FastAPI app, startup (creates tables), /health
  config.py             env-based settings
  database.py           SQLAlchemy engine/session
  models.py             UploadedFile 1──* Feature
  schemas.py            Pydantic response models
  api/files.py          HTTP layer only (validation, status codes)
  services/parsers.py   Shapefile (pyshp) and KML (defusedxml) -> shapely geometries
  services/measurements.py   CRS handling + area/length
  services/processor.py parse -> measure -> persist orchestration
tests/                  pytest suite (compares results with pyproj's geodesic calculations)
```

**File-processing flow:** upload is size-limited and extension-checked → a `PROCESSING` record is
created → the matching parser reads the file (zip is read in memory, never extracted to disk) →
each feature becomes a shapely geometry + JSON-safe properties (bad features are flagged, not fatal) →
features are measured → rows are saved → the file is marked `COMPLETED` (or `FAILED` with a message).

**Measurement flow:** flatten Multi*/collections → polygons get area + perimeter, lines get length,
points are `NOT_REQUIRED`, anything else is `UNSUPPORTED` → results are stored with the projected
CRS used, so every number is traceable.

**CRS handling:** KML is always EPSG:4326 by specification. A Shapefile's CRS comes from its `.prj`
(parsed with pyproj). Every geometry is first converted to WGS84 lon/lat (`always_xy=True` to avoid
axis-order bugs), then projected to a *local* metric CRS chosen from the feature's bounding-box centre:
* **Area → Lambert Azimuthal Equal-Area** centred on the feature (equal-area, so area is not distorted).
* **Length/perimeter → the feature's UTM zone** (conformal; scale error about 0.04 %); UPS near the poles.

If a Shapefile has no readable `.prj`, its features are `SKIPPED` with an explanation rather than guessing.
Coordinates outside lon/lat ranges after transformation return `ERROR` (CRS is probably wrong).

## Design Decisions

* **FastAPI over Django/DRF** — small, API-only service; automatic validation and OpenAPI docs.
* **Pure-Python parsing (pyshp + stdlib XML) instead of GeoPandas/Fiona/GDAL** — GDAL wheels/installs are
  the most common setup failure on Windows; KML support in GDAL also depends on how it was built.
  Trade-off: only the formats required here are supported.
* **Local projection per feature instead of one global CRS** — a single UTM zone is wrong for data that spans
  zones; Web Mercator (EPSG:3857) badly inflates areas away from the equator. Equal-area for area and UTM for
  length keep errors small for any location. Alternative considered: pyproj `Geod` ellipsoidal calculations
  (very accurate) — not used because the brief asks for a projected-CRS approach, but the tests use it as ground truth.
* **Always reproject to WGS84 first, even for already-projected files** — handles feet-based CRSs and
  distorted CRSs uniformly, at a small CPU cost.
* **Synchronous processing** — files up to the size limit parse in well under a few seconds, and the client gets
  the result immediately. The `status` field is already in the API so moving to background jobs won't break clients.
* **SQLite + SQLAlchemy** — zero setup locally; switch to PostgreSQL by setting `DATABASE_URL`.
  Measurements are computed once at upload and stored, so GET requests are cheap.
* **Security** — `defusedxml` blocks XML entity-expansion attacks; zip contents are read in memory (no zip-slip);
  total uncompressed size and upload size are capped.
* **Failures are data, not crashes** — bad files return `422` and are recorded as `FAILED`; bad features get a status + message.

## Learnings

* Axis order is the classic CRS trap (EPSG:4326 is officially lat/lon, files are lon/lat) — `always_xy=True` fixes it.
* A projection is only "right" for one property: equal-area for area, conformal for length. Choosing per metric beats one CRS.
* Testing against an independent ellipsoidal calculation caught-in-advance whether the projected results are trustworthy.

## Future Scope

* Background processing (Celery/RQ) for very large files, with progress reporting.
* More formats: KMZ, GeoJSON, GeoPackage, and multi-layer files.
* Store originals in object storage (S3) and support re-processing.
* PostGIS storage for spatial queries; vector tiles / map preview.
* Authentication, rate limiting, per-user files, and file deletion endpoints.
* Optional geodesic (ellipsoidal) mode; user-selectable target CRS; antimeridian-crossing geometries.
* Dockerfile + CI (GitHub Actions) and Alembic migrations.
