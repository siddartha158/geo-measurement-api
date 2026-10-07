"""Creates samples/sample_shapefile.zip (WGS84 polygon + attributes) for manual testing.

Run from the project root:  python scripts/make_sample_shapefile.py
"""
import io
import zipfile
from pathlib import Path

import shapefile  # pyshp

WGS84_PRJ = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)


def build_zip() -> bytes:
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON) as w:
        w.field("name", "C", size=40)
        w.field("crop", "C", size=20)
        # Outer rings must be clockwise in the shapefile spec.
        w.poly([[(77.0, 17.0), (77.0, 17.01), (77.01, 17.01), (77.01, 17.0), (77.0, 17.0)]])
        w.record("Field 1", "Rice")
        w.poly([[(77.02, 17.0), (77.02, 17.02), (77.04, 17.02), (77.04, 17.0), (77.02, 17.0)]])
        w.record("Field 2", "Cotton")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("fields.shp", shp.getvalue())
        zf.writestr("fields.shx", shx.getvalue())
        zf.writestr("fields.dbf", dbf.getvalue())
        zf.writestr("fields.prj", WGS84_PRJ)
    return buf.getvalue()


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "samples" / "sample_shapefile.zip"
    out.write_bytes(build_zip())
    print(f"Wrote {out}")
