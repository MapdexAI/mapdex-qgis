#!/usr/bin/env python3
"""Build a small parcel layer whose every answer is known in advance.

The point is not to have data in QGIS. It is to be able to tell whether Mapdex
answered *correctly*, which needs a dataset where the right answer is arithmetic
rather than opinion. Every geometry here is a rectangle on a round-numbered
grid, so areas, counts and distances are exact and checkable by hand.

Deliberate choices, each aimed at something that would otherwise pass unnoticed:

* **A projected CRS (EPSG:32635, UTM 35N).** The measure tool converts to WGS84
  before measuring. On a WGS84 project a conversion bug is invisible; on this
  one it produces a plausible wrong number. This is the single most valuable
  property of the fixture.
* **Turkish field names and values** (`alan_m2`, `kullanim`, `mahalle`,
  `nüfus`). The PDF path once silently deleted these characters, and the field
  calculator writes columns. If encoding breaks anywhere, it breaks here.
* **A null and an empty-string value.** "Average area" and "break down by
  category" both have to decide what to do about missing data, and the honest
  answers differ from the careless ones.
* **One deliberately larger parcel**, so a top-N or outlier question has a
  single unambiguous right answer.

Run inside the worker container, which has GDAL:

    docker run --rm -v "$PWD:/out" -w /out mapdex-piri-extension-host \\
        python make_fixture.py
"""
from osgeo import ogr, osr

OUTPUT = "mapdex-test-parcels.gpkg"
LAYER = "parcels"

# UTM zone 35N: metres, covers Istanbul. A round origin keeps every derived
# number exact.
EPSG = 32635
ORIGIN_X, ORIGIN_Y = 500000, 4540000
SIDE = 100  # metres, so a standard parcel is exactly 10,000 m²

USE = ["konut", "ticari", "tarım"]
DISTRICT = ["Şişli", "Üsküdar", "Beşiktaş"]


def rectangle(x, y, width, height):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for px, py in [(x, y), (x + width, y), (x + width, y + height), (x, y + height), (x, y)]:
        ring.AddPoint_2D(px, py)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


def main():
    driver = ogr.GetDriverByName("GPKG")
    source = driver.CreateDataSource(OUTPUT)

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(EPSG)
    layer = source.CreateLayer(LAYER, srs, ogr.wkbPolygon)

    for name, kind, width in [
        ("parsel_no", ogr.OFTString, 16),
        ("alan_m2", ogr.OFTReal, 0),
        ("kullanim", ogr.OFTString, 16),
        ("mahalle", ogr.OFTString, 24),
        ("nufus", ogr.OFTInteger, 0),
    ]:
        field = ogr.FieldDefn(name, kind)
        if width:
            field.SetWidth(width)
        layer.CreateField(field)

    definition = layer.GetLayerDefn()
    index = 0
    for row in range(5):
        for column in range(5):
            if index >= 24:
                break
            # Parcel 12 is deliberately 200x100 so "the largest" has exactly one
            # answer and area statistics are not a flat line.
            width = SIDE * 2 if index == 12 else SIDE
            geometry = rectangle(
                ORIGIN_X + column * (SIDE + 20),
                ORIGIN_Y + row * (SIDE + 20),
                width, SIDE,
            )

            feature = ogr.Feature(definition)
            feature.SetGeometry(geometry)
            feature.SetField("parsel_no", "P-{:03d}".format(index + 1))
            feature.SetField("alan_m2", float(width * SIDE))
            feature.SetField("mahalle", DISTRICT[index % 3])

            # One null and one empty string, on purpose: a category breakdown
            # and an average both have to say something honest about them.
            if index == 7:
                feature.SetFieldNull("kullanim")
            elif index == 15:
                feature.SetField("kullanim", "")
            else:
                feature.SetField("kullanim", USE[index % 3])

            if index == 3:
                feature.SetFieldNull("nufus")
            else:
                feature.SetField("nufus", 100 + index * 25)

            layer.CreateFeature(feature)
            feature = None
            index += 1

    source = None
    print("wrote {} with {} parcels in EPSG:{}".format(OUTPUT, index, EPSG))


if __name__ == "__main__":
    main()
