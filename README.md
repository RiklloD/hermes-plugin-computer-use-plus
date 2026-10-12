# Computer Use Plus for Hermes

Open Computer Use's nine native computer tools **plus our Hermes backend and Desktop live viewer**: per-window screen share, native cursor tracking, Stop, Take over / Hand back. One package carries both `__init__.py` and `desktop/plugin.js`.

**This is the current Open Computer Use MCP integration, not the retired cua-driver backend.**

```text
Hermes agent → our adapter → OpenComputerUse mcp (open-computer-use 0.3.6)
                    └────→ our live-view bridge → desktop/plugin.js
```

The tool adapter and Desktop viewer are the active Hermes implementation. Only installation-path handling changes for portability.

## Install

Requires **Hermes Desktop 0.21.6 or newer, Apple Silicon macOS, Python 3 and Node.js >=18/npm**. This integration's window capture and viewer are macOS-specific even though upstream Open Computer Use has other platform builds. Verified against upstream Hermes `1fd18277267152893a8a24792b79213e7ca3f899`; internal API changes in later Hermes releases may require updates. No Hermes core patch is needed.

```sh
hermes plugins install RiklloD/hermes-plugin-computer-use-plus --no-enable
python3 "${HERMES_HOME:-$HOME/.hermes}/plugins/computer-use-plus/scripts/install-backend.py"
hermes plugins enable computer-use-plus
```

Run the last command interactively to review the `tools.override` capability. With your consent, it hides Hermes's built-in cua-driver tool so the model has one computer-use interface. Without consent both interfaces remain visible. Ensure **Computer Use** is enabled for your chat surface in `hermes tools`.

Restart Hermes Desktop/backend; enable **Computer Use Plus** in **Capabilities → Plugins**. Grant **Open Computer Use** Accessibility and Screen Recording in macOS System Settings when requested—only you can grant them. `/computer-view` reports tool/server/viewer readiness. The **Computer** status-bar chip shows the viewer.

Do **not** add a second `mcp_servers.open-computer-use` entry: this plugin manages the native MCP process. If an old raw entry is enabled, disable it through Hermes's MCP settings. The install script only runs `npm ci --ignore-scripts` against the bundled lockfile and checks the executable; it does not edit configuration, native permissions or a Hermes checkout.

## Clone instead

```sh
git clone https://github.com/RiklloD/hermes-plugin-computer-use-plus.git
cd hermes-plugin-computer-use-plus
python3 scripts/install-backend.py
hermes plugins install "file://$PWD" --no-enable
hermes plugins enable computer-use-plus
```

Local installs may link to this folder; keep it. The pinned upstream **open-computer-use 0.3.6** binary is fetched from npm with lockfile integrity, not checked into Git. The upstream source is public at [iFurySt/open-codex-computer-use](https://github.com/iFurySt/open-codex-computer-use). Set `HERMES_OCU_HOME` only if intentionally reusing an existing backend installation.

## Safety and limitations

The viewer is local-only: authenticated loopback ports 47831–47840. It shows target windows, never a whole-desktop capture. Browser-origin checks are not a sandbox or protection from a malicious local process. Take over fences agent actions and viewer captures. Stop kills an in-flight MCP call; the action may already have happened and is reported as unknown—do not blindly repeat it. Global input fallback is off unless the upstream explicit gate is enabled.

No hosted remote viewer or Windows/Linux support is claimed by this package. macOS permissions must be granted on each user's machine. Running the package gives trusted local Python/native code the user's privileges.

## Development

Use a Hermes-managed test interpreter, an importable stock Hermes checkout on `PYTHONPATH`, and a disposable `HOME`/`HERMES_HOME`: `python -m pytest tests -q -p no:xdist`. Native schema verification runs only where the backend is installed. `evals/run.py` is an **opt-in live Calculator test**; do not run it unattended while someone uses Calculator. Set `HERMES_AGENT_DIR` to a source checkout if it is not already importable. See [ARCHITECTURE.md](ARCHITECTURE.md) for the adapter and viewer internals.

## Profiles and updates

Installation follows the active Hermes profile (`HERMES_HOME`). Install and enable in every profile that should use the backend. Hermes Desktop discovers `desktop/plugin.js` as the frontend of the same package: **Capabilities → Plugins**, then turn it on. No manual frontend copying or npm build is required. Restart the backend after Python updates.

Update with `hermes plugins update computer-use-plus`; to reproduce a release use `hermes plugins install RiklloD/hermes-plugin-computer-use-plus --ref <full-40-character-commit>`. Tags are convenient for Git clones; Hermes's `--ref` accepts full commit SHAs, not tags.

Packages contain source, manifests, licenses and tests—not personal config, credentials, live databases, logs, transcripts, native permission grants or caches. The private recovery repo is not made public.
