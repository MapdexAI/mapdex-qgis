# Mapdex for QGIS

Send a scanned map, PDF or vector layer from QGIS to [Mapdex](https://mapdex.ai),
run a georeference / parcel digitization / validation task there, and add the
finished raster or vector result back into your QGIS project.

The plugin is a companion to the Mapdex web workspace: heavy processing and the
review of flagged features happen in Mapdex, while QGIS stays the place where
you prepare the input and use the result.

## What it does

| Workflow | Input | Result added back to QGIS |
| --- | --- | --- |
| Georeference maps | scanned map / plan (raster or PDF) | georeferenced GeoTIFF |
| Digitize parcels | georeferenced raster | vector parcel layer (GeoJSON) |
| Validate & deliver | vector layer | validation report + delivery package |
| Full pipeline | scanned map / plan | georeferenced raster + vector result |

Work runs in the background: you can close the panel, keep working, or restart
QGIS — the panel picks the task back up from *Recent tasks*.

## Requirements

- QGIS 3.34 or newer (Qt5), or QGIS 4 (Qt6).
- A [Mapdex](https://mapdex.ai) account. The plugin opens your browser once to
  authorize this QGIS installation.
- Network access to your Mapdex endpoint (`api.mapdex.ai`, or your own address
  for a self-hosted install).
- No extra Python packages: the plugin uses only the QGIS standard runtime.

## Install

**From the QGIS plugin repository:** *Plugins → Manage and Install Plugins →
All*, search for “Mapdex”.

**From a zip:** *Plugins → Manage and Install Plugins → Install from ZIP*, then
pick `mapdex-qgis.zip`.

Open the panel from *Web → Mapdex*, or the Mapdex toolbar button.

## Use it

1. **Connect.** Click *Connect Mapdex*; your browser opens with a short code to
   approve. The access token is stored encrypted in the QGIS Authentication
   Manager — never in plain settings.
2. **Pick a project** in your Mapdex workspace.
3. **Pick a workflow** and a **source**: the active QGIS layer, or a file on
   disk. A vector layer is exported to GeoPackage first; a raster is sent as
   its own file.
4. **Start task.** Progress, the current phase, and the next action appear in
   the panel.
5. **Review** flagged features in the browser when the task asks for it
   (*Review in Mapdex*).
6. **Add result to QGIS** loads the finished layer into your project.

## Self-hosted / custom endpoint

*Settings* in the panel header exposes the API and web addresses. They must be
`https://`, except for a Mapdex running on your own machine
(`http://127.0.0.1:…`). Changing the endpoint clears the stored token: a
session belongs to the deployment that issued it, so you re-authorize against
the new address.

## What leaves your machine

- The layer or file you explicitly select, uploaded to your Mapdex project.
- The task parameters (workflow kind, project id).
- Nothing else: the plugin does not scan your project, and the access token is
  only ever sent to the API address you configured — not to storage hosts or
  redirect targets on other domains.

## Development

```bash
python3 -m pytest tests -q      # unit tests (no QGIS install required)
python3 scripts/package.py      # build dist/mapdex-qgis.zip
```

`mapdex_qgis/generated_contracts.py` is generated from the Mapdex contracts
package; do not edit it by hand.

## License

GNU General Public License v2.0 or later. See [LICENSE](LICENSE).

## Support

- Documentation: <https://mapdex.ai/qgis>
- Issues: <https://github.com/MapdexAI/mapdex-qgis/issues>
- Email: support@mapdex.ai
