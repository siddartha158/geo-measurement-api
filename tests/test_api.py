import io
import zipfile
from pathlib import Path

import pytest
import shapefile
from pyproj import Geod
from shapely.geometry import LineString, Polygon

from scripts.make_sample_shapefile import WGS84_PRJ, build_zip

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
GEOD = Geod(ellps="WGS84")


def upload(client, name, data):
    return client.post("/api/files/", files={"file": (name, data)})


def geodesic_area(poly):
    return abs(GEOD.geometry_area_perimeter(poly)[0])


def test_kml_end_to_end(client):
    r = upload(client, "sample.kml", (SAMPLES / "sample.kml").read_bytes())
    assert r.status_code == 201
    info = r.json()
    assert info["status"] == "COMPLETED" and info["crs"] == "EPSG:4326" and info["feature_count"] == 5

    assert client.get(f"/api/files/{info['id']}/").json()["filename"] == "sample.kml"

    m = client.get(f"/api/files/{info['id']}/measurements/").json()
    by_id = {x["feature_id"]: x for x in m["measurements"]}

    plot_a = Polygon([(77, 17), (77.01, 17), (77.01, 17.01), (77, 17.01)])
    assert by_id[0]["status"] == "MEASURED"
    assert by_id[0]["measurements"]["area_sq_m"] == pytest.approx(geodesic_area(plot_a), rel=1e-3)

    # polygon with a hole: area = outer - inner
    outer = Polygon([(77.02, 17), (77.04, 17), (77.04, 17.02), (77.02, 17.02)])
    inner = Polygon([(77.025, 17.005), (77.035, 17.005), (77.035, 17.015), (77.025, 17.015)])
    assert by_id[1]["measurements"]["area_sq_m"] == pytest.approx(
        geodesic_area(outer) - geodesic_area(inner), rel=1e-3
    )

    road = LineString([(77, 17), (77.01, 17.01), (77.02, 17.02)])
    assert by_id[2]["measurements"]["length_m"] == pytest.approx(GEOD.geometry_length(road), rel=1e-3)

    assert by_id[3]["status"] == "NOT_REQUIRED"  # point
    assert by_id[4]["status"] == "UNSUPPORTED"  # placemark without geometry (handled gracefully)
    assert m["summary"]["status_counts"]["MEASURED"] == 3


def test_features_endpoint_returns_geometry_crs_properties(client):
    fid = upload(client, "sample.kml", (SAMPLES / "sample.kml").read_bytes()).json()["id"]
    body = client.get(f"/api/files/{fid}/features/?limit=2").json()
    assert body["total"] == 5 and len(body["features"]) == 2
    first = body["features"][0]
    assert first["geometry_type"] == "Polygon" and first["crs"] == "EPSG:4326"
    assert first["properties"]["name"] == "Plot A" and first["properties"]["owner"] == "Ravi"
    assert first["geometry"]["type"] == "Polygon"


def test_shapefile_zip(client):
    r = upload(client, "fields.zip", build_zip())
    assert r.status_code == 201
    fid = r.json()["id"]
    m = client.get(f"/api/files/{fid}/measurements/").json()
    expected = geodesic_area(Polygon([(77, 17), (77, 17.01), (77.01, 17.01), (77.01, 17)]))
    assert m["measurements"][0]["measurements"]["area_sq_m"] == pytest.approx(expected, rel=1e-3)
    feats = client.get(f"/api/files/{fid}/features/").json()["features"]
    assert feats[0]["properties"] == {"name": "Field 1", "crop": "Rice"}


def test_projected_shapefile_is_reprojected_before_measuring(client):
    """Write coordinates in UTM 43N (EPSG:32643) and check the area matches the geodesic truth."""
    from pyproj import CRS, Transformer

    t = Transformer.from_crs(4326, 32643, always_xy=True)
    ring_ll = [(77, 17), (77, 17.01), (77.01, 17.01), (77.01, 17), (77, 17)]
    ring = [t.transform(x, y) for x, y in ring_ll]
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON) as w:
        w.field("id", "N")
        w.poly([ring])
        w.record(1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("p.shp", shp.getvalue())
        zf.writestr("p.shx", shx.getvalue())
        zf.writestr("p.dbf", dbf.getvalue())
        zf.writestr("p.prj", CRS.from_epsg(32643).to_wkt(version="WKT1_ESRI"))
    r = upload(client, "p.zip", buf.getvalue())
    assert r.status_code == 201
    m = client.get(f"/api/files/{r.json()['id']}/measurements/").json()
    expected = geodesic_area(Polygon(ring_ll))
    assert m["measurements"][0]["measurements"]["area_sq_m"] == pytest.approx(expected, rel=1e-3)


def test_shapefile_without_prj_is_skipped_not_crashed(client):
    zbuf = io.BytesIO(build_zip())
    out = io.BytesIO()
    with zipfile.ZipFile(zbuf) as src, zipfile.ZipFile(out, "w") as dst:
        for n in src.namelist():
            if not n.endswith(".prj"):
                dst.writestr(n, src.read(n))
    r = upload(client, "noprj.zip", out.getvalue())
    assert r.status_code == 201 and r.json()["crs"] is None
    m = client.get(f"/api/files/{r.json()['id']}/measurements/").json()
    assert {x["status"] for x in m["measurements"]} == {"SKIPPED"}


def test_rejects_unsupported_extension(client):
    assert upload(client, "a.txt", b"hello").status_code == 400


def test_invalid_zip_and_invalid_kml_are_422_and_recorded_as_failed(client):
    r = upload(client, "bad.zip", b"not a zip")
    assert r.status_code == 422 and r.json()["file"]["status"] == "FAILED"
    r = upload(client, "bad.kml", b"<kml><oops>")
    assert r.status_code == 422
    fid = r.json()["file"]["id"]
    assert client.get(f"/api/files/{fid}/").json()["status"] == "FAILED"
    assert client.get(f"/api/files/{fid}/measurements/").status_code == 409


def test_xml_entity_attack_is_rejected(client):
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><kml>&a;</kml>'
    assert upload(client, "evil.kml", evil).status_code == 422


def test_unknown_id_is_404(client):
    assert client.get("/api/files/doesnotexist/").status_code == 404
    assert client.get("/api/files/doesnotexist/measurements/").status_code == 404
