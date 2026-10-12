#!/usr/bin/env python3
"""Install the pinned, plugin-local native runtime; never alter Hermes config."""
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
if sys.platform != "darwin":
    raise SystemExit("Computer Use Plus currently requires macOS (native capture and viewer).")
if platform.machine() != "arm64":
    raise SystemExit("This release is verified on Apple Silicon; Intel Macs are not supported by this installer.")
npm = shutil.which("npm")
if not npm:
    raise SystemExit("Install Node.js >=18 (including npm), then run this command again.")
subprocess.run([npm, "ci", "--prefix", str(root / "backend"), "--ignore-scripts", "--no-audit", "--no-fund"], check=True)
binary = root / "backend/node_modules/open-computer-use/dist/Open Computer Use.app/Contents/MacOS/OpenComputerUse"
if not binary.is_file() or not os.access(binary, os.X_OK):
    raise SystemExit("npm finished, but the expected executable is missing or not executable.")
print("Installed Open Computer Use 0.3.6. Enable the plugin, restart Hermes, then grant macOS permissions when asked.")
