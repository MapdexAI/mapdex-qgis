"""What Nivo says about the open project before anybody asks it anything.

The plugin had no state that explained itself. A stranger who found it by
searching the QGIS plugin repository opened a panel showing two URL fields, a
Connect button, and an empty chat box whose placeholder read "Ask Nivo about
this layer or map view". A hundred registered capabilities, and no way to
discover one of them. So they typed "hello", got "hello" back, and closed a GIS
agent believing it was a chat toy.

The fix is not a welcome screen and it is not a brochure listing the four
workflows by their internal names. Nobody buys a pipeline from a dropdown. What
a GIS person recognises is a correct, specific statement about the file already
on their screen, and the useful thing about this product is that everything up
to the paid step can be measured locally, for free, with no account, no model
call and no network:

    cadastre_1943.tif - 2759x2590 - not georeferenced
    This layer does not sit anywhere on the map, so nothing can be
    overlaid on it.

That sentence is free to produce and it is the whole sales argument, because
the reader already has the problem. The paid step is then named as the next
move on a measured finding rather than described in the abstract.

The ordering rule, which is what keeps this honest:

* a finding states something that was MEASURED, never something inferred from
  a file name or an extension;
* free work is offered first and runs here, so the demonstration happens before
  the ask;
* work that needs a Mapdex account is named as such, with what it would do,
  and is never disguised as a local action.

Reading cost is a design constraint, not an afterthought. Everything a finding
needs is O(1) layer metadata that QGIS already holds - type, CRS, feature
count, extent validity - so the reading is produced on panel open and on every
layer change without touching a feature. Anything that has to walk geometry
(validity, field profiles) is offered as a one-click free action instead, so a
million-feature layer cannot make the panel hang while introducing itself.

Nothing here imports Qt or QGIS.
"""
from __future__ import annotations

from typing import Any, Mapping

# What an action costs the reader, which is the only distinction that matters
# to somebody deciding whether to press it.
LOCAL = "local"      # runs here, now, free, no account
ASK = "ask"          # sends a question to whichever engine is configured
MAPDEX = "mapdex"    # needs a Mapdex account; named, never disguised

BLOCKING = "blocking"
WARNING = "warning"
INFO = "info"

# Severity order for presentation. A raster that is nowhere outranks a styling
# nicety, and a reading that buries the blocking finding under three tidy ones
# has wasted the only attention it gets.
_RANK = {BLOCKING: 0, WARNING: 1, INFO: 2}


def _action(label: str, kind: str, **extra: Any) -> dict[str, Any]:
    action = {"label": label, "kind": kind}
    action.update(extra)
    return action


def _finding(identifier: str, severity: str, headline: str, detail: str, actions) -> dict[str, Any]:
    return {
        "id": identifier,
        "severity": severity,
        "headline": headline,
        "detail": detail,
        "actions": list(actions),
    }


def _empty_project() -> list[dict[str, Any]]:
    return [
        _finding(
            "no_layers",
            INFO,
            "Nothing is open yet.",
            "Open a layer or a scanned map and Nivo reads it here. Nivo can also "
            "measure, style, analyse and run QGIS processing on whatever you load.",
            [_action("Add an OpenStreetMap basemap", LOCAL, capability="map.basemap@1")],
        )
    ]


# The one QGIS raster provider that reads a file this user brought. Everything
# else - wms, wmts, xyz, arcgismapserver, vectortile - is somebody's tile
# service, and a service is a backdrop rather than a source.
#
# A whitelist rather than a list of services, deliberately, because the two
# mistakes are not equal: an unrecognised provider treated as a backdrop costs
# a suggestion, and an unrecognised provider treated as a sheet offers to
# digitize parcels out of a map server. That is exactly what shipped - Nivo
# added an OpenStreetMap basemap and then offered to extract parcels from it,
# because the layer has a valid CRS and a real extent and every measurement
# said "placed raster".
FILE_BACKED_RASTER_PROVIDERS = frozenset({"gdal"})


def _is_service(layer: Mapping[str, Any]) -> bool:
    """Is this raster a tile service rather than a file on disk?

    An absent provider is treated as file-backed: `profile_layer` has always
    reported rasters without one, and reclassifying every one of those as a
    backdrop would silently withdraw the georeference offer from real scans.
    """
    provider = str(layer.get("provider") or "").strip().lower()
    return bool(provider) and provider not in FILE_BACKED_RASTER_PROVIDERS


def _raster_findings(layer: Mapping[str, Any]) -> list[dict[str, Any]]:
    name = str(layer.get("name") or "This raster")
    size = ""
    width = int(layer.get("width") or 0)
    height = int(layer.get("height") or 0)
    if width > 0 and height > 0:
        size = " - {}x{} px".format(width, height)

    if _is_service(layer):
        # No sheet findings at all. A backdrop is not a source, and offering
        # to place or digitize one is an offer that cannot be honoured.
        return [
            _finding(
                "basemap_added",
                INFO,
                "{} is a backdrop.".format(name),
                "It draws under your data and is not something Mapdex reads.",
                [],
            )
        ]

    if not layer.get("georeferenced"):
        # The finding this whole surface exists for. It is measured
        # (`QgsRasterLayer.crs().isValid()` plus a non-empty extent), it is the
        # documented prerequisite of extraction, and it is the one problem a
        # scanned map always has and its owner can never fix by hand quickly.
        return [
            _finding(
                "raster_not_georeferenced",
                BLOCKING,
                "{}{} is not georeferenced.".format(name, size),
                "It does not sit anywhere on the map, so nothing can be overlaid on "
                "it, measured from it or exported from it in real-world coordinates.",
                [
                    _action(
                        "Georeference it with Mapdex",
                        MAPDEX,
                        workflow="georeference",
                        promise="Mapdex reads the sheet's own grid and corner coordinates and "
                                "places it, then you check the result before anything is kept.",
                    ),
                    _action("Why does this matter?", ASK,
                            prompt="Why can't I use a raster that is not georeferenced?"),
                ],
            )
        ]

    return [
        _finding(
            "raster_placed",
            INFO,
            "{}{} is placed on the map.".format(name, size),
            "The lines printed on it are still pixels. Turning the parcel boundaries "
            "into real geometry is the digitize step.",
            [
                _action("Zoom to it", LOCAL, capability="map.zoom_layer@1"),
                _action(
                    "Digitize parcels with Mapdex",
                    MAPDEX,
                    workflow="digitize_parcels",
                    promise="Mapdex traces the parcel boundaries into a vector layer and "
                            "flags every feature it is unsure about for your review.",
                ),
            ],
        )
    ]


def _vector_findings(layer: Mapping[str, Any]) -> list[dict[str, Any]]:
    name = str(layer.get("name") or "This layer")
    count = int(layer.get("feature_count") or 0)
    findings: list[dict[str, Any]] = []

    if not str(layer.get("crs") or ""):
        findings.append(
            _finding(
                "vector_no_crs",
                BLOCKING,
                "{} has no coordinate reference system.".format(name),
                "Every distance and area measured from it is meaningless until it "
                "declares one, and QGIS will not say so on its own.",
                [_action("Which CRS should this be?", ASK,
                         prompt="This layer has no CRS. How do I work out which one it should be?")],
            )
        )

    findings.append(
        _finding(
            "vector_ready",
            INFO,
            "{} - {} feature(s).".format(name, count) if count else "{} is empty.".format(name),
            "Checking geometry runs here in QGIS and costs nothing. It is also the "
            "measurement that says whether this is fit to hand to anybody.",
            [
                _action("Check geometry validity", LOCAL, capability="geoprocessing.validate@1"),
                _action("Profile the fields", LOCAL, capability="analytics.profile@1"),
                _action(
                    "Validate and deliver with Mapdex",
                    MAPDEX,
                    workflow="validate_deliver",
                    promise="Mapdex repairs what it can, states what it could not, and packages "
                            "the result with a report that says whether it is fit to send on.",
                ),
            ],
        )
    )
    return findings


def _crs_mismatch(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Layers whose CRS differs from the project's.

    QGIS reprojects them on the fly and says nothing, so the map looks correct
    while every measurement crossing those layers is not. It is the most common
    silent defect in a working project and it is free to detect.
    """
    names = [str(item) for item in (state.get("crs_mismatch") or []) if str(item)]
    if not names:
        return []
    listed = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
    return [
        _finding(
            "crs_mismatch",
            WARNING,
            "{} layer(s) are in a different CRS from the project.".format(len(names)),
            "QGIS draws them correctly and reprojects on the fly, so this is invisible "
            "on screen: {}.".format(listed),
            [_action("Explain the risk here", ASK,
                     prompt="Some layers are in a different CRS from the project. "
                            "What can go wrong and what should I do?")],
        )
    ]


def opening_reading(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Everything Nivo can say for free about what is currently open.

    ``state`` is measured QGIS metadata, never a guess from a file name:

        layer          the active layer: name, kind, crs, feature_count,
                       width, height, georeferenced
        crs_mismatch   names of project layers whose CRS differs from the
                       project's
        layer_count    how many layers the project holds

    Returns findings ordered worst-first, plus whether anything was measured at
    all. An empty project is a state with its own advice, not an error.
    """
    state = state or {}
    layer = state.get("layer") if isinstance(state.get("layer"), Mapping) else {}
    kind = str((layer or {}).get("kind") or "")

    findings: list[dict[str, Any]] = []
    if not layer:
        findings.extend(_empty_project() if not int(state.get("layer_count") or 0) else [
            _finding(
                "no_active_layer",
                INFO,
                "No layer is selected.",
                "Click a layer in the Layers panel and Nivo reads it here.",
                [],
            )
        ])
    elif kind == "raster":
        findings.extend(_raster_findings(layer))
    elif kind == "vector":
        findings.extend(_vector_findings(layer))
    else:
        findings.append(
            _finding(
                "unknown_layer",
                INFO,
                "{} is open.".format(layer.get("name") or "A layer"),
                "Nivo reads vector and raster layers. Ask it anything about this project.",
                [],
            )
        )

    findings.extend(_crs_mismatch(state))
    findings.sort(key=lambda item: _RANK.get(item["severity"], 9))
    return {"findings": findings, "measured": bool(layer)}


def account_actions(reading: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The Mapdex work this particular reading has earned the right to offer.

    Derived from the findings rather than listed as a menu: a workflow named
    without a measurement behind it is the brochure this module exists instead
    of.
    """
    out = []
    for finding in (reading or {}).get("findings") or []:
        for action in finding.get("actions") or []:
            if action.get("kind") == MAPDEX:
                out.append(action)
    return out
