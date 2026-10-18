#!/usr/bin/env python3
"""Build the patched Open Computer Use engine (engine/README.md) into engine/dist; never alter Hermes config.

  python3 scripts/build-engine.py                 clone the pinned upstream commit, apply engine/patches, build
  python3 scripts/build-engine.py --source DIR    build an existing checkout as it is (patches already applied)
  python3 scripts/build-engine.py --output DIR    put the app somewhere else than engine/dist

Needs Apple Silicon macOS with Xcode Command Line Tools (swift, git) and openssl. The first build creates a
self-signed code-signing identity in a dedicated keychain under the Hermes root (tools/ocu-signing); later builds
reuse it, so macOS keeps the permissions granted to "Open Computer Use (Dev)".
"""
import argparse
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
IDENTITY = "Hermes OCU Local Signing"
APP = "Open Computer Use (Dev).app"


def run(*cmd, **kw):
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def output(*cmd):
    return subprocess.run([str(c) for c in cmd], check=True, capture_output=True, text=True).stdout


def hermes_root() -> Path:
    try:
        sys.path.insert(0, os.environ.get("HERMES_AGENT_DIR", ""))
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except Exception:
        return Path.home() / ".hermes"


def checkout(work: Path) -> Path:
    pin = json.loads((ENGINE / "UPSTREAM.json").read_text())
    src = work / "open-computer-use"
    if not (src / ".git").is_dir():
        work.mkdir(parents=True, exist_ok=True)
        run("git", "clone", "--quiet", pin["repository"], src)
    run("git", "-C", src, "fetch", "--quiet", "origin")
    if (src / ".git" / "rebase-apply").exists():
        subprocess.run(["git", "-C", str(src), "am", "--abort"])
    run("git", "-C", src, "checkout", "--quiet", "--force", "--detach", pin["commit"])
    patches = sorted((ENGINE / "patches").glob("*.patch"))
    run("git", "-C", src, "-c", "user.name=Computer Use Plus", "-c", "user.email=build@localhost",
        "am", "--quiet", "--keep-cr", *patches)
    return src


def signing_identity(signing: Path) -> tuple:
    """(SHA-1 of the identity, keychain path), creating both on first use."""
    keychain = signing / "ocu-signing.keychain-db"
    secret = signing / "keychain-password"
    signing.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not keychain.exists():
        secret.write_text(secrets.token_urlsafe(24))
        secret.chmod(0o600)
        conf = signing / "cert.cnf"
        conf.write_text("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=" + IDENTITY +
                        "\n[ext]\nbasicConstraints=critical,CA:false\nkeyUsage=critical,digitalSignature\n"
                        "extendedKeyUsage=critical,codeSigning\n")
        key, cert, p12 = signing / "key.pem", signing / "cert.pem", signing / "cert.p12"
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
            "-days", "3650", "-config", conf, capture_output=True)
        p12_pass = secrets.token_urlsafe(16)
        export = ["openssl", "pkcs12", "-export", "-inkey", key, "-in", cert, "-out", p12, "-passout", f"pass:{p12_pass}"]
        if subprocess.run(export + ["-legacy"], capture_output=True).returncode != 0:
            run(*export, capture_output=True)
        password = secret.read_text().strip()
        run("security", "create-keychain", "-p", password, keychain)
        run("security", "set-keychain-settings", keychain)
        run("security", "unlock-keychain", "-p", password, keychain)
        run("security", "import", p12, "-k", keychain, "-P", p12_pass, "-T", "/usr/bin/codesign", capture_output=True)
        run("security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", password,
            keychain, capture_output=True)
        for f in (key, p12, conf):
            f.unlink()
    if not secret.exists():
        raise SystemExit(f"{keychain} exists but {secret} is missing; write its password there (mode 600).")
    run("security", "unlock-keychain", "-p", secret.read_text().strip(), keychain)
    for line in output("security", "find-identity", "-p", "codesigning", keychain).splitlines():
        if f'"{IDENTITY}"' in line:
            return line.split()[1], keychain
    raise SystemExit(f"no '{IDENTITY}' identity in {keychain}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, help="existing open-computer-use checkout to build as it is")
    parser.add_argument("--output", type=Path, default=ENGINE / "dist", help="where the app goes")
    args = parser.parse_args()

    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("The patched engine is built and verified on Apple Silicon macOS only.")
    for tool in ("swift", "git", "openssl", "codesign", "security"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} is missing; install Xcode Command Line Tools (xcode-select --install).")

    src = args.source.expanduser().resolve() if args.source else checkout(ENGINE / ".work")
    identity, keychain = signing_identity(hermes_root() / "tools" / "ocu-signing")
    out = args.output.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ,
           "OPEN_COMPUTER_USE_APP_VARIANT": "dev",
           "OPEN_COMPUTER_USE_INCLUDE_LOCKED_USE": "0",
           "OPEN_COMPUTER_USE_CODESIGN_MODE": "identity",
           "OPEN_COMPUTER_USE_CODESIGN_IDENTITY": identity,
           "OPEN_COMPUTER_USE_CODESIGN_KEYCHAIN": str(keychain),
           "OPEN_COMPUTER_USE_APP_OUTPUT_DIR": str(out)}
    run(src / "scripts" / "build-open-computer-use-app.sh", "release", cwd=src, env=env)

    binary = out / APP / "Contents" / "MacOS" / "OpenComputerUse"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise SystemExit("The build finished, but the expected executable is missing or not executable.")
    print(f"Built {out / APP}.\nRestart Hermes, then grant Accessibility and Screen Recording to "
          "'Open Computer Use (Dev)' when asked (once; rebuilds keep the grants).")


if __name__ == "__main__":
    main()
