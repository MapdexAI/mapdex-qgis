"""QGIS main-thread affinity for the state Nivo sends.

QgsTask.run() executes on a worker thread. iface.activeLayer(), the map canvas
and the layer tree are main-thread only, so reading them from inside a task
returns an empty or stale snapshot - which is why Nivo reported "no layer is
active yet" while a layer was open in the Layers panel.

These are source-level guards: the real check needs a running QGIS, but the
mistake is visible in the code shape, so it is worth catching before it ships.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PLUGIN = (ROOT / "mapdex_qgis" / "plugin.py").read_text(encoding="utf-8")

# Calls that must never appear inside work handed to a background QgsTask.
MAIN_THREAD_ONLY = ("_nivo_snapshot(", "iface.mapCanvas(", "iface.activeLayer(", "layerTreeView(")


def _task_lambdas() -> list[str]:
    """The work callables passed to self._task(...)."""
    return re.findall(r"self\._task\(\s*[^\n]*\n\s*(lambda[^\n]*)", PLUGIN)


def test_the_compose_context_is_built_before_the_task_starts():
    body = PLUGIN[PLUGIN.index("def ask_nivo"):PLUGIN.index("def stop_nivo")]
    build = body.index("context = companion_context(self._nivo_snapshot())")
    start = body.index("self._nivo_compose_task = self._task(")
    assert build < start, "the snapshot must be captured on the main thread first"


def test_no_task_work_reads_main_thread_only_qgis_state():
    offenders = []
    for work in _task_lambdas():
        for call in MAIN_THREAD_ONLY:
            if call in work:
                offenders.append((call, work[:100]))
    assert not offenders, "main-thread QGIS calls inside background task work: {}".format(offenders)


def test_the_compose_task_sends_a_prebuilt_context():
    body = PLUGIN[PLUGIN.index("def ask_nivo"):PLUGIN.index("def stop_nivo")]
    assert "self.api.compose(self.project_id, message, context)" in body
    # The old shape rebuilt the snapshot inside the lambda.
    assert "companion_context(self._nivo_snapshot())" not in body.split("self._task(")[1]
