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

## Driving the panel, the controls and the dispatcher

`verify_in_qgis.py` never builds a widget. `verify_qgis_end_to_end.py` does: it
constructs the companion panel, constructs the plugin against a real dock and a
real map canvas, presses its controls, opens the History dialog modally and
walks it through four states, and then sends a simulated compose response for
**every** action the plugin advertises to the server.

```
set QT_QPA_PLATFORM=offscreen
"C:/Program Files/QGIS 4.0.2/bin/python-qgis.bat" verify_qgis_end_to_end.py
```

Same staging rules as above: stage `mapdex_qgis/` and the fixture beside the
script, run from a native Windows path, and let the script make its own copy of
the GeoPackage. `MAPDEX_VERIFY_TRACE=1` prints tracebacks. It exits non-zero on
any failure and prints the dispatch sweep in full.

Two boundaries are stubbed and only two — the HTTP client and the modal question
box, because a script cannot answer a network call or a blocking prompt. Every
widget, enum, layer, renderer and capability executor is the real one. Two
consequences worth remembering:

- **A stub that is missing a method reports a working feature as broken.**
  `apply_visualization` calls `iface.layerTreeView().refreshLayerSymbology(...)`
  unguarded, so a fake interface without it turns every successful restyle into
  a reported `AttributeError`. The harness therefore hands the plugin a real
  `QgsLayerTreeView` over the real project tree.
- **A `QgsMapCanvas` records no extent history offscreen.** Verified directly:
  two `setExtent` calls followed by `zoomToPreviousExtent` leave the canvas
  exactly where it was, with the canvas shown and the event loop pumped. So
  forward/back navigation cannot be told from a no-op here, and
  `qgis:next_extent@1` is reported as *not run* rather than as a defect.

**Measured 2026-08-17, QGIS 4.0.2-Norrköping / Qt 6.11.0 / PyQt 6.11.0,
offscreen: 41 checks ran — 38 passed, 2 failed, 1 not run.** The dispatch sweep
covered all 53 advertised actions (18 legacy `qgis:*` plus 35 bound
capabilities): 51 executed with an observable effect or a stated result, 1 was
not verifiable offscreen, and **none was silently dropped**. Two runs
back to back produced byte-identical output apart from QGIS's generated layer
id. What it proved, and the two things it disproved:

| | |
| --- | --- |
| Panel | Nivo header carries the title, the context line, History and New chat in one row; the composer owns none of them; the transcript is a `QScrollArea` over a widget tree, and a `<b>` in replayed text renders as those characters through the plugin's own renderer |
| Controls | New chat clears the transcript, drops the thread and says where the old one went — and refuses while a request is in flight; History is disabled without a session, and reaches loading, empty, error-with-retry and loaded; Delete asks first, No means no, Yes deletes and reloads; opening a conversation replays it |
| Answers | 24 features, EPSG:32635, 250,000 m² measured off the geometry by `analytics.geometry@1` (planar, EPSG:32635) **and** stated in `alan_m2`, mean 10,416.667, largest P-013 at 20,000, `nufus` sum 9,125 over 23 values with mean 396.739 — not 380.2 |
| Effects | select-all really selects 24, select-by-ids 3; categorized styling really produces a `QgsCategorizedSymbolRenderer` with 3 categories; labels really turn on from `parsel_no`; opacity really moves 0.55 → 0.40; zoom really moves the canvas, and into the canvas CRS rather than raw UTM metres pasted onto degrees; a preview filter really cuts the layer to 7 and clearing restores 24; export really writes a file |
| Gates | `field.calculate@1` asks before writing and writes nothing when declined; a Processing operation asks before it runs; `system.execute_code@1`, `system.execute_sql@1` and `postgis.write@1` are refused by the registry and never advertised |
| **Failed** | **the `kullanim` breakdown does not separate the null from the empty string**, and **the geometry-area answer states no number in the transcript** |

### The two failures

**1. Null and empty string are merged.** `analytics.categorical_summary`
collapses `None` and `""` into a single `nulls` count. Against this fixture it
reports `count=24, usable=22, nulls=2` and three category buckets, and the
transcript line reads *"3 distinct values in 22 · tarım (8), konut (7), ticari
(7) · 2 empty"*.

Nothing is dropped and the arithmetic is right, so this is milder than the
failure the fixture was built to catch. But the two values are distinct in the
file on purpose — `make_fixture.py` writes `SetFieldNull` on parcel 8 and `""`
on parcel 16 — and "no value recorded" and "recorded as blank" are different
facts about a cadastre. The table above asks for five buckets; this build can
express three plus a merged count.

**2. A correct measurement nobody is told.** `analytics.geometry@1` measures the
area off the geometry and gets it exactly right — 250,000.0 m², planar, in
EPSG:32635, confirmed by calling `QGISRuntime.measure_geometry` directly. The
line the user actually reads is *"verification · geometry measurement"*:
`RESULT_DESCRIBERS` in `plugin.py` has no entry for this result kind, so
`describe_capability_result` falls back to the kind name. The fallback is
deliberately honest — its comment says an unrecognised kind must not be dressed
up as a measurement — so this is a missing describer, not a wrong answer. It is
still the whole point of the capability going undelivered. Measured against the
working tree on the day `analytics.geometry@1` landed, so check whether the
describer arrived with a later commit before treating it as open.

The canonical payloads in the sweep are derived from the registry rather than
listed, so a capability added tomorrow is dispatched here without anyone
extending a table. If it declares a required parameter the script has no value
for, that row is reported as **not run** and names the parameter to add.

### What this cannot cover

The harness is offscreen and has no server, so a human still has to check: that
the dock is legible and correctly sized at real dock widths and on a real
theme; that the canvas visibly redraws (it asserts renderer and extent state,
not pixels); forward/back map navigation; that a Processing algorithm confirmed
with **Yes** produces the right output layer; and the whole compose round trip
against a live Mapdex — the sweep supplies the server's responses rather than
receiving them.
