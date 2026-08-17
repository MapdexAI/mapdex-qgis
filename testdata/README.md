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
