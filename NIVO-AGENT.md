# Nivo agent architecture (QGIS)

Nivo is the GIS agent inside QGIS. This document is for contributors: how the
pieces fit, how to add a capability, and which rules are security boundaries
rather than conventions.

The short version: **the model chooses, trusted code executes.** A model reply
is never code, SQL, an expression or an algorithm id. It names a capability from
a registry and supplies parameters; Python you can read validates and runs it.

---

## 1. The modules

| Module | Responsibility | Imports QGIS? |
| --- | --- | --- |
| `capabilities.py` | The trusted registry: what exists, its risk, where it runs | no |
| `agent.py` | The bounded objective → capability → observation loop | no |
| `analytics.py` | Statistics. Every number Nivo states comes from here | no |
| `spatial.py` | CRS/unit correctness plus an exact small geometry kernel | no |
| `postgis.py` | Read-only typed query builders and the SQL guard | no |
| `presentation.py` | Analysis → renderer spec, and evidence → report | no |
| `providers.py` | Model vendors behind one interface; hosted vs BYOK | no |
| `credentials.py` | Provider keys in the QGIS auth database only | lazily |
| `qgis_runtime.py` | The only module that touches the live project | yes |

Everything except `qgis_runtime.py` is unit-testable without a QGIS runtime,
which is why the maths and the security boundaries are covered by tests rather
than by inspection.

---

## 2. The loop

```
objective (any language)
  → context (bounded project summary)
  → model picks ONE capability
  → registry validates id + params
  → executor runs it in QGIS / PostGIS / Mapdex
  → observation (real measured facts)
  → model picks the next capability, or answers
```

Bounds that matter: a fixed step budget, a guard against repeating an identical
call, and an honest "I ran out of steps" outcome. A model that cannot finish
must not invent a conclusion.

**Facts come from observations.** The system prompt forbids stating a number
that is not in an observation, and the report builder can only render sections
from measured results. A step that failed is printed as *not checked*, never
omitted — a report that silently drops what it could not do reads as a clean
bill of health.

---

## 3. Adding a capability

Three things, no forks and no `if/elif` chain to extend.

**1. Declare it** in `capabilities.py`:

```python
_c("style.hillshade@1", "style", "Render a hillshade from a DEM.",
   params={"layer_id": {"type": "string", "required": True},
           "z_factor": {"type": "number", "min": 0.1, "max": 10, "default": 1.0}},
   targets=("raster",), execution=EXEC_CLIENT_UI,
   reversible=True, previewable=True, produces=("map_effect",),
   clients=(CLIENT_QGIS,))
```

**2. Bind an executor** in `qgis_runtime.build_executor`:

```python
"style.hillshade@1": lambda p: runtime.hillshade(p["layer_id"], p.get("z_factor", 1.0)),
```

**3. Test it.** Analytical capabilities need a known-answer fixture; anything
that changes the map needs an undo test.

That is all. `GET`-style discovery, the agent's allowed-tool list, the
capability-negotiation handshake with the server and the risk gate are all
derived from the registry entry.

### Registry fields worth understanding

- `risk` — `safe` runs immediately; `consequential` **always** asks for
  confirmation (derived automatically, so a new consequential capability cannot
  ship ungated by forgetting a flag); `forbidden` cannot be executed by any
  path and carries a stated refusal.
- `clients` — `qgis`, `workspace`, or both. QGIS Processing is desktop-only;
  analytics and PostGIS are shared, so the same typed intent means the same
  thing on both surfaces.
- `execution` — where it physically runs. Orthogonal to which vendor answers
  the chat turn.
- `reversible` — the runtime pushes previous state onto the undo stack before
  applying, so "undo" restores what was actually there.

---

## 4. Providers: hosted by default, BYOK when a key exists

```
no key            → hosted   → Mapdex /v1/compose, gated by the plan
key configured    → byok     → straight to the vendor, never through Mapdex
local endpoint    → byok     → nothing leaves the machine
```

BYOK wins over the plan whenever a key is present: the user paid for that key.
Because the request goes direct, BYOK traffic costs Mapdex nothing to serve.

> **Not wired yet.** `resolve_runtime` returns that decision and nothing acts on
> it: its only caller sets a label in the settings panel, `build_provider` has no
> caller at all, and `ask_nivo` calls `self.api.compose(...)` unconditionally. So
> every turn currently takes the hosted row of this table whatever the key says.
> The table describes the design; `tests/test_privacy_claims.py` is what stops it
> being restated to a user as a fact until the branch exists.

Adding a vendor is one subclass and one registration:

```python
class MyVendor(ModelProvider):
    name = "myvendor"
    default_model = "my-model-1"

    def complete(self, system, messages): ...

register_provider(MyVendor)
```

No GIS code changes. GIS actions are never coupled to a vendor.

### Key handling rules (these are boundaries, not style)

- A key lives in the **QGIS Authentication Manager** only. Never `QSettings` —
  that is a plaintext file that syncs, backs up and ends up in bug reports.
- If the auth database is unavailable the key is **session-only**. We do not
  write a plaintext fallback, because that silently downgrades a secure install.
- A key is passed to the transport and nowhere else: not a prompt, not a
  capability payload, not a log, not an error. Every provider error is passed
  through `redact()`.
- A provider endpoint must be `https`, or `http` on localhost. Checked when the
  provider is built **and** again in the transport, so a subclass that builds a
  URL differently still cannot reach `file://`.

---

## 5. PostGIS is read-only, structurally

Users ask analytical questions in their own language instead of writing SQL. The
tempting implementation — let the model emit SQL and run it — is prohibited: a
model that can emit SQL can emit `DROP TABLE`, and a prompt instruction is not a
security boundary.

```
question → typed AnalyticalSpec → trusted builder → parameterised SELECT
```

Three independent layers, so one defect is not enough to write:

1. **Construction** — only the builders in `postgis.py` emit SQL, and each emits
   a single `SELECT`. No code path renders a user string into a statement.
2. **Identifiers** — schema/table/column names must match the catalogue
   discovered from the live connection, then are quoted. A forged or remembered
   name does not resolve.
3. **Execution** — `guard_statement` re-inspects the finished SQL (rejecting
   multiple statements, write keywords, `SELECT INTO`, row locking and
   file/network functions), and the session runs in `BEGIN READ ONLY` with a
   statement timeout and row caps.

Layer 3 exists to catch a future builder bug, not to sanitise user input.

Connections are addressed by the id of a saved connection. Host, user, password
and DSN never enter a prompt, a log or an error.

---

## 6. Spatial correctness

The most damaging silent GIS error is a unit mistake, so it is decided in one
place. `spatial.plan_distance` returns one of three strategies and never guesses:

- `map_units` — projected CRS in a known linear unit; converted.
- `reproject` — geographic CRS. **A metric distance is never converted to
  degrees.** There is deliberately no degrees-per-metre shortcut: the factor is
  latitude-dependent, so a single scalar is wrong everywhere but one parallel.
- `unsupported` — unknown unit or unknown CRS unit. Ask, do not assume.

Related rules enforced by tests: bounding-box transforms densify their edges
(corner-only transforms can miss the real extent under rotation), and a viewport
that cannot be transformed into a layer's CRS is an error rather than silently
becoming "the whole layer".

---

## 7. Reading is bounded

Attribute analytics fetch only the columns needed and set `NoGeometry` —
pulling full geometries to compute one mean is what makes an assistant unusable
on a real project. Reads are capped at `MAX_ANALYTIC_FEATURES`, and when the cap
is hit the result says it was truncated instead of describing a sample as the
whole dataset.

---

## 8. What is deliberately not possible

| Request | Outcome |
| --- | --- |
| Run Python / shell | `system.execute_code@1` is `forbidden` with a stated refusal |
| Run arbitrary SQL | `system.execute_sql@1` is `forbidden`; ask the question instead |
| Write to PostGIS | `postgis.write@1` is `forbidden`; the refusal explains why |
| Call an unregistered capability | Rejected before execution |
| Smuggle an extra parameter | Rejected — unknown params are an error, not ignored |
| Run Processing without asking | `consequential` risk always confirms first |

An unexpected parameter is rejected rather than dropped on purpose: silently
normalising it away is how a smuggled field gets ignored in one release and
quietly honoured in the next.
