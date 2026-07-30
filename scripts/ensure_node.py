from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Tuple


Version = Tuple[int, int, int]


def _version_tuple(value: str) -> Version:
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", value or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())


def _is_supported_node(version: Version) -> bool:
    major, minor, patch = version
    if major == 20:
        return (major, minor, patch) >= (20, 19, 0)
    if major == 22:
        return (major, minor, patch) >= (22, 12, 0)
    return major > 22


def _node_version(node_exe) -> Version:
    try:
        result = subprocess.run(
            [str(node_exe), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            universal_newlines=True,
        )
    except Exception:
        return (0, 0, 0)
    return _version_tuple(result.stdout.strip())


def _write_env(root: Path, node_exe: Path, npm_cmd: Path) -> None:
    node_home = node_exe.parent
    env_file = root / ".tools" / "node" / "env.bat"
    npx_cmd = node_home / "npx.cmd"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "\n".join(
            [
                "@echo off",
                f'set "NODE_HOME={node_home}"',
                f'set "NODE_EXE={node_exe}"',
                f'set "NPM_CMD={npm_cmd}"',
                f'set "NPX_CMD={npx_cmd}"',
                'set "PATH=%NODE_HOME%;%PATH%"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _global_node() -> Optional[Tuple[Path, Path]]:
    node = shutil.which("node")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not node or not npm:
        return None
    node_path = Path(node).resolve()
    npm_path = Path(npm).resolve()
    if _is_supported_node(_node_version(node_path)):
        return node_path, npm_path
    return None


def _portable_node(root: Path) -> Optional[Tuple[Path, Path]]:
    node_root = root / ".tools" / "node"
    candidates = []
    for node_path in node_root.rglob("node.exe"):
        npm_path = node_path.parent / "npm.cmd"
        if npm_path.exists() and _is_supported_node(_node_version(node_path)):
            candidates.append((node_path, npm_path, _node_version(node_path)))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[2], reverse=True)
    return candidates[0][0], candidates[0][1]


def _windows_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x64"


def _latest_supported_lts() -> str:
    with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=30) as response:
        releases = json.loads(response.read().decode("utf-8"))

    for release in releases:
        version = release.get("version", "")
        if release.get("lts") and _is_supported_node(_version_tuple(version)):
            return version

    raise RuntimeError("Nenhuma versao LTS compativel do Node foi encontrada.")


def _download_portable_node(root: Path) -> Tuple[Path, Path]:
    version = _latest_supported_lts()
    arch = _windows_arch()
    zip_name = f"node-{version}-win-{arch}.zip"
    url = f"https://nodejs.org/dist/{version}/{zip_name}"
    node_root = root / ".tools" / "node"
    node_root.mkdir(parents=True, exist_ok=True)

    print(f"Baixando Node.js portatil {version} ({arch})...")
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = Path(temp_dir) / zip_name
        urllib.request.urlretrieve(url, zip_path)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(node_root)

    extracted = node_root / zip_name[:-4]
    node_exe = extracted / "node.exe"
    npm_cmd = extracted / "npm.cmd"
    if not node_exe.exists() or not npm_cmd.exists():
        raise RuntimeError("O pacote do Node foi baixado, mas os executaveis nao foram encontrados.")

    return node_exe, npm_cmd


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd().resolve()

    found = _global_node() or _portable_node(root)
    if not found:
        found = _download_portable_node(root)

    node_exe, npm_cmd = found
    _write_env(root, node_exe, npm_cmd)
    version = ".".join(str(part) for part in _node_version(node_exe))
    print(f"Node pronto: {node_exe} ({version})")
    print(f"npm pronto:  {npm_cmd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
