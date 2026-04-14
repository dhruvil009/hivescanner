"""CLI dependency auto-installer for HiveScanner scanners."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

# Registry of CLI tools and how to install them.
# Install methods are tried in order: brew → npm → pip.
_TOOL_REGISTRY = {
    "gh": {
        "description": "GitHub CLI",
        "brew": "gh",
        "post_install": "Run `gh auth login` to authenticate with GitHub.",
    },
    "gws": {
        "description": "Google Workspace CLI",
        "brew": "googleworkspace-cli",
        "npm": "@googleworkspace/cli",
        "post_install": "Run `gws auth setup --login` to configure OAuth and authenticate.",
        "setup_requires": ["gcloud"],
    },
    "gcloud": {
        "description": "Google Cloud SDK",
        "brew": "google-cloud-sdk",
        "post_install": "Run `gcloud auth login` to authenticate.",
    },
    "whatsapp-cli": {
        "description": "WhatsApp CLI",
        "brew": "vicentereig/tap/whatsapp-cli",
        "post_install": "Run `whatsapp-cli auth` (QR code), then `whatsapp-cli messages sync` to populate the local DB. The scanner reads from the synced DB.",
    },
    "git": {
        "description": "Git",
        "brew": "git",
    },
}

# Map scanner names to their required CLI tools.
_SCANNER_TOOLS = {
    "github": ["gh"],
    "calendar": ["gws"],
    "email": ["gws"],
    "gchat": ["gws"],
    "whatsapp": ["whatsapp-cli"],
    "git_status": ["git"],
}

_AUTO_INSTALL_ENV = "HIVESCANNER_AUTO_INSTALL"


def _log(msg: str) -> None:
    print(f"[hivescanner:deps] {msg}", file=sys.stderr)


def _auto_install_enabled() -> bool:
    return os.environ.get(_AUTO_INSTALL_ENV, "").strip() in {"1", "true", "yes"}


def _install_instructions(name: str, info: dict) -> str:
    parts = []
    if "brew" in info and platform.system() == "Darwin":
        parts.append(f"brew install {info['brew']}")
    if "npm" in info:
        parts.append(f"npm install -g {info['npm']}")
    if "pip" in info:
        parts.append(f"pip install {info['pip']}")
    if not parts:
        return "install manually (no known package)"
    joined = " OR ".join(parts)
    post = info.get("post_install")
    return f"{joined}" + (f"  (then: {post})" if post else "")


def _run_install(cmd: list[str], timeout: int = 120) -> bool:
    """Run an install command silently. Returns True on success."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return True
        _log(f"  failed ({cmd[0]}): {result.stderr.strip()[:200]}")
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        _log(f"  error ({cmd[0]}): {e}")
        return False


def ensure_tool(name: str) -> bool:
    """Check if a CLI tool is available; auto-install if missing and opted in.

    By default, logs install instructions and returns False when missing.
    Set HIVESCANNER_AUTO_INSTALL=1 to enable the auto-install chain.
    """
    if shutil.which(name):
        return True

    info = _TOOL_REGISTRY.get(name)
    if not info:
        _log(f"{name}: not found and no install method known.")
        return False

    desc = info.get("description", name)

    if not _auto_install_enabled():
        _log(f"{desc} ({name}) not found. Install: {_install_instructions(name, info)}")
        _log(f"  (or set {_AUTO_INSTALL_ENV}=1 to enable auto-install)")
        return False

    # Install prerequisites first (e.g. gws needs gcloud for auth setup)
    for prereq in info.get("setup_requires", []):
        ensure_tool(prereq)

    _log(f"{desc} ({name}) not found. Attempting auto-install...")

    # Try brew (macOS)
    if platform.system() == "Darwin" and shutil.which("brew") and "brew" in info:
        _log(f"  trying: brew install {info['brew']}")
        if _run_install(["brew", "install", info["brew"]]):
            if shutil.which(name):
                _log(f"  installed {name} via brew.")
                _post_install_hint(info)
                return True

    # Try npm
    if shutil.which("npm") and "npm" in info:
        _log(f"  trying: npm install -g {info['npm']}")
        if _run_install(["npm", "install", "-g", info["npm"]]):
            if shutil.which(name):
                _log(f"  installed {name} via npm.")
                _post_install_hint(info)
                return True

    # Try pip
    if "pip" in info:
        pip_cmd = _find_pip()
        if pip_cmd:
            _log(f"  trying: {pip_cmd} install {info['pip']}")
            if _run_install([pip_cmd, "install", info["pip"]]):
                if shutil.which(name):
                    _log(f"  installed {name} via pip.")
                    _post_install_hint(info)
                    return True

    _log(f"  could not auto-install {name}. Install it manually.")
    return False


def _post_install_hint(info: dict) -> None:
    hint = info.get("post_install")
    if hint:
        _log(f"  note: {hint}")


def _find_pip() -> str | None:
    if shutil.which("pip3"):
        return "pip3"
    if shutil.which("pip"):
        return "pip"
    return None


def preflight(config: dict) -> dict[str, bool]:
    """Check and auto-install CLI deps for all enabled scanners.

    Returns {tool_name: available} for each required tool.
    """
    results: dict[str, bool] = {}
    scanners_config = config.get("scanners", {})

    for scanner_name, tools in _SCANNER_TOOLS.items():
        scanner_cfg = scanners_config.get(scanner_name, {})
        if not scanner_cfg.get("enabled", False):
            continue
        for tool in tools:
            if tool not in results:
                results[tool] = ensure_tool(tool)

    return results
