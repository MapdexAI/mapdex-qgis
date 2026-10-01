"""Generate the monorepo's Nivo prompt FROM the vendored package.

The direction reversed. The prompt used to live in packages/ai-prompts and be
copied into the plugin as a Python string literal; it now lives inside the
`nivo-gis` package as package data (`nivo/prompts/nivo.system.md`), because the
package IS the shared agent core and a contract that ships with the code cannot
drift from it. Every client - QGIS, the Mapdex workspace, anything else that
adopts the package - drives the loop from that one text.

The monorepo still needs its own copy: the prompt registry selects prompts by
name and version, the eval harness scores them, and neither can read inside a
vendored directory in a plugin. So this writes packages/ai-prompts/prompts/
nivo.system.md byte-for-byte from the package copy, front matter included - the
version and evaluation_suite fields are exactly what the registry reads, so
stripping them would break the thing the file is for.

    python3 scripts/vendor_prompt.py            # write it
    python3 scripts/vendor_prompt.py --check    # fail if it would change

`--check` is the drift gate: `tests/` cannot own it, because the file it guards
is outside the published plugin tree and the plugin's suite must pass with no
monorepo present.

A CHANGED PROMPT IS A RELEASE ARTIFACT. Bumping `version:` obliges a re-run of
the `nivo.system` eval suite before the new text ships; a prompt whose behaviour
nobody re-measured is not a prompt anybody can stand behind.
"""
from __future__ import annotations

import pathlib
import sys

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = PLUGIN_ROOT.parents[1] / "packages" / "nivo" / "nivo" / "prompts" / "nivo.system.md"
TARGET = PLUGIN_ROOT.parents[1] / "packages" / "ai-prompts" / "prompts" / "nivo.system.md"


def declared_version(text: str) -> str:
    """The `version:` field of the front matter, for the report line."""
    if not text.startswith("---"):
        return "?"
    for line in text.split("---", 2)[1].splitlines():
        if line.strip().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return "?"


def main(argv) -> int:
    check = "--check" in argv
    if not SOURCE.is_file():
        print("error: {} is missing; the vendored package is incomplete".format(SOURCE), file=sys.stderr)
        return 2
    if not TARGET.parent.is_dir():
        print("error: {} is missing; run this from the monorepo".format(TARGET.parent), file=sys.stderr)
        return 2

    # Bytes, not text: this file is compared for equality by the prompt
    # registry's own tooling, and a line-ending rewrite would read as a change.
    source = SOURCE.read_bytes()
    current = TARGET.read_bytes() if TARGET.is_file() else b""
    version = declared_version(source.decode("utf-8"))

    if source == current:
        print("packages/ai-prompts/prompts/nivo.system.md is current (version {})".format(version))
        return 0

    if check:
        print(
            "error: packages/ai-prompts/prompts/nivo.system.md differs from the vendored package "
            "(version {}). Run scripts/vendor_prompt.py, and re-run the nivo.system eval suite "
            "before shipping the new text.".format(version),
            file=sys.stderr,
        )
        return 1

    TARGET.write_bytes(source)
    print(
        "wrote {} bytes into packages/ai-prompts/prompts/nivo.system.md (version {}).\n"
        "The prompt changed: re-run the nivo.system eval suite before this ships.".format(
            len(source), version)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
