from __future__ import annotations

import json
import hashlib
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Tuple


Version = Tuple[int, int, int]
DOWNLOAD_ATTEMPTS = 3


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
    lines = [
        "@echo off",
        f'set "NODE_HOME={node_home}"',
        f'set "NODE_EXE={node_exe}"',
        f'set "NPM_CMD={npm_cmd}"',
        f'set "NPX_CMD={npx_cmd}"',
        'set "PATH=%NODE_HOME%;%PATH%"',
        "",
    ]
    encoding = "mbcs" if sys.platform == "win32" else "utf-8"
    env_file.write_text("\r\n".join(lines), encoding=encoding)
    (env_file.parent / "env.json").write_text(
        json.dumps(
            {
                "node_exe": str(node_exe),
                "npm_cmd": str(npm_cmd),
                "npx_cmd": str(npx_cmd),
            },
            ensure_ascii=True,
            indent=2,
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
    releases = json.loads(_download_bytes("https://nodejs.org/dist/index.json").decode("utf-8"))

    for release in releases:
        version = release.get("version", "")
        if release.get("lts") and _is_supported_node(_version_tuple(version)):
            return version

    raise RuntimeError("Nenhuma versao LTS compativel do Node foi encontrada.")


def _download_bytes(url: str) -> bytes:
    last_error = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "NeveTracker-Installer/1.0"})
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except Exception as error:
            last_error = error
            if attempt < DOWNLOAD_ATTEMPTS:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Falha ao baixar {url} apos {DOWNLOAD_ATTEMPTS} tentativas: {last_error}")


def _download_file(url: str, destination: Path) -> None:
    last_error = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "NeveTracker-Installer/1.0"})
            with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
                shutil.copyfileobj(response, output)
            return
        except Exception as error:
            last_error = error
            destination.unlink(missing_ok=True)
            if attempt < DOWNLOAD_ATTEMPTS:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Falha ao baixar {url} apos {DOWNLOAD_ATTEMPTS} tentativas: {last_error}")


def _expected_sha256(version: str, file_name: str) -> str:
    checksums = _download_bytes(f"https://nodejs.org/dist/{version}/SHASUMS256.txt").decode("utf-8")
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == file_name:
            return parts[0].lower()
    raise RuntimeError(f"Checksum oficial do Node nao encontrado para {file_name}.")


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if target != root and root not in target.parents:
            raise RuntimeError("O pacote do Node contem um caminho de extracao invalido.")
    archive.extractall(destination)


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
        _download_file(url, zip_path)
        expected_hash = _expected_sha256(version, zip_name)
        actual_hash = hashlib.sha256(zip_path.read_bytes()).hexdigest().lower()
        if actual_hash != expected_hash:
            raise RuntimeError("O checksum do Node portatil nao confere com o publicado em nodejs.org.")
        extracted = node_root / zip_name[:-4]
        if extracted.exists():
            shutil.rmtree(extracted)
        with zipfile.ZipFile(zip_path) as archive:
            _safe_extract(archive, node_root)

    node_exe = extracted / "node.exe"
    npm_cmd = extracted / "npm.cmd"
    if not node_exe.exists() or not npm_cmd.exists():
        raise RuntimeError("O pacote do Node foi baixado, mas os executaveis nao foram encontrados.")

    return node_exe, npm_cmd


def main() -> int:
    root_arg = sys.argv[1].strip('"') if len(sys.argv) > 1 else None
    root = Path(root_arg).resolve() if root_arg else Path.cwd().resolve()

    source = "Node portatil existente"
    found = _portable_node(root)
    if not found:
        try:
            found = _download_portable_node(root)
            source = "Node portatil baixado"
        except Exception as error:
            found = _global_node()
            if not found:
                raise RuntimeError(
                    "Nao foi possivel baixar o Node portatil e nenhum Node global compativel foi encontrado."
                ) from error
            source = "Node global compativel"
            print(f"AVISO: falha ao baixar Node portatil ({error}). Usando Node global compativel.")

    node_exe, npm_cmd = found
    _write_env(root, node_exe, npm_cmd)
    version = ".".join(str(part) for part in _node_version(node_exe))
    print(f"Origem: {source}")
    print(f"Node pronto: {node_exe} ({version})")
    print(f"npm pronto:  {npm_cmd}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERRO: {error}", file=sys.stderr)
        raise SystemExit(1)
