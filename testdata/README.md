# Test parcels, with the answers

`mapdex-test-parcels.gpkg` exists so a QGIS verification pass can tell whether
Mapdex was **right**, not whether it felt right. Every geometry is a rectangle
on a round-numbered grid, so the correct answer to each question below is
arithmetic rather than opinion.

Regenerate with `make_fixture.py` (needs GDAL; the worker image has it).

## The layer

24 parcels, **EPSG:32635 (UTM 35N, metres)**. The projected CRS is deliberate:
the measure tool converts to WGS84 before measuring, so on a WGS84 project a
conversion bug is invisible, and on this one it produces a plausible wrong
number.

Fields: `parsel_no`, `alan_m2`, `kullanim`, `mahalle`, `nufus`. The values carry
Turkish characters (`tarım`, `Şişli`, `Üsküdar`, `Beşiktaş`) because that is
where an encoding fault shows up first.

## The answers

Measured from the file itself, not asserted:

| Question | Correct answer |
| --- | --- |
| How many parcels | **24** |
| Total area | **250,000 m²** |
| Mean area | **10,416.667 m²** |
| Largest parcel | **20,000 m²** (P-013, the only one that is 200 × 100) |
| Breakdown by `kullanim` | konut **7**, ticari **7**, tarım **8**, one null, one empty string |
| Sum of `nufus` | **9,125** across the 23 parcels that have one |
| Mean `nufus` | **396.739** (23 values, not 24) |
| Parcels with no `nufus` | **1** |

Two of these are traps worth watching:

- **Mean `nufus`.** One parcel has no value. 9,125 ÷ 23 = 396.739 is right;
  9,125 ÷ 24 = 380.2 means a null was silently counted as zero, which is a
  different and wrong claim about the data.
- **The `kullanim` breakdown.** There are five buckets, not three: a null and an
  empty string are distinct from each other and from a category. An answer that
  reports only three has quietly decided something on your behalf.

## Distances you can check

Parcel P-001 occupies exactly x 500000–500100, y 4540000–4540100.

- Its bottom-left to bottom-right corner is **exactly 100 m**.
- Its bottom-left to top-right corner is **141.421 m** (100√2).
- P-001's bottom-left to P-002's bottom-left is **exactly 120 m** (100 m parcel
  plus a 20 m gap).

Click those corners with the measure tool. If a number comes back wrong while
looking reasonable, the WGS84 conversion is the first place to look. Compare
against QGIS's own measure tool as a second opinion.

## Field calculator

`alan_m2 / 10000` should produce 1.0 for every standard parcel and 2.0 for
P-013. Then try these, which must all be **refused**:

- a name that already exists (`alan_m2`) — it must not overwrite a column
- `__import__('os')` — not a field of this layer
- `nufus * 2` written into a field named with Turkish characters — this one must
  *work*, and the column name must survive intact

## Running the runtime against a real QGIS

`verify_in_qgis.py` loads the plugin against the actual QGIS libraries and
checks each answer against the table above. Unit tests here run against stubs,
and a stub cannot reproduce the failure that matters most: PyQt6 removed the
flat enum forms PyQt5 accepted, so a wrong one raises `AttributeError` the first
time a code path runs and never before.

On Windows:

```
set QT_QPA_PLATFORM=offscreen
"C:/Program Files/QGIS 4.0.2/bin/python-qgis.bat" verify_in_qgis.py
```

Point `sys.path` at the plugin source you want to test; copying
`mapdex_qgis/` next to the script is the simplest way. Run it from a native
path: `cmd.exe` refuses a UNC working directory, so a WSL share silently
resolves to the Windows directory instead.

The script copies the fixture before touching it. The field calculator writes
real columns into a real GeoPackage, so without that a second run trips its own
refuse-to-overwrite guard and reports a working refusal as a failure.

**Measured 2026-08-17, QGIS 4.0.2 / Qt 6.11 / PyQt6: 16 of 16 checks pass.**
That run found one defect no unit test could: `export_layer` called
`self._require_layer`, a method defined nowhere, so export raised
`AttributeError` on first real use. Every export test asserted against
`validate_request` and none ran the executor body.
