"""从 Mihomo 官方 Release 下载并验证资产 digest 或校验文件。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

REPOSITORY = "https://github.com/MetaCubeX/mihomo/releases/download/"
API = "https://api.github.com/repos/MetaCubeX/mihomo/releases/"


def fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "smart-dns-lb-installer"})
    with urlopen(request, timeout=30) as response:
        return response.read()


def select_asset(assets: list[dict], arch: str, version: str) -> dict:
    machine = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armv7",
        "armv7": "armv7",
    }.get(arch.lower())
    if not machine:
        raise ValueError(f"尚未适配架构 {arch}，可设置 SKIP_MIHOMO=1 后手动安装")
    names = [f"mihomo-linux-{machine}-{version}.gz"]
    if machine == "amd64":
        names.insert(0, f"mihomo-linux-amd64-compatible-{version}.gz")
    for name in names:
        for asset in assets:
            if asset["name"] == name:
                return asset
    raise ValueError(f"官方 Release 缺少 {machine} 对应的压缩包")


def expected_checksum(assets: list[dict], name: str) -> str:
    for asset in assets:
        if asset["name"] == name:
            digest = asset.get("digest") or ""
            match = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", digest)
            if match:
                return match[1].lower()
    dedicated = {name + suffix for suffix in (".sha256", ".sha256sum")}
    combined = {
        "sha256sum.txt",
        "sha256sums.txt",
        "sha256sums",
        "checksums.txt",
        "checksums",
        "sha256sum",
    }
    candidates = sorted(assets, key=lambda item: item["name"] not in dedicated)
    for asset in candidates:
        single = asset["name"] in dedicated
        if not single and asset["name"].lower() not in combined:
            continue
        text = fetch(asset_url(asset)).decode("utf-8-sig")
        for line in text.splitlines():
            parts = line.split()
            if parts and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                if (single and len(parts) == 1) or (
                    len(parts) == 2 and parts[1].lstrip("*") == name
                ):
                    return parts[0].lower()
            match = re.fullmatch(r"SHA256 \((.+)\) = ([0-9a-fA-F]{64})", line)
            if match and match[1] == name:
                return match[2].lower()
    raise ValueError("官方 Release 缺少对应 SHA-256 校验记录，安装已中止")


def asset_url(asset: dict) -> str:
    url = asset["browser_download_url"]
    if not url.startswith(REPOSITORY):
        raise ValueError("发布资源地址不是 Mihomo 官方仓库")
    return url


def download(output: Path, version: str = "latest", arch: str | None = None) -> None:
    if not re.fullmatch(r"latest|v?\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.-]+)?", version):
        raise ValueError("MIHOMO_VERSION 应为 latest 或 vX.Y.Z")
    if version != "latest" and not version.startswith("v"):
        version = "v" + version
    release = json.loads(
        fetch(API + ("latest" if version == "latest" else "tags/" + version))
    )
    tag = release["tag_name"]
    assets = release["assets"]
    asset = select_asset(assets, arch or platform.machine(), tag)
    expected = expected_checksum(assets, asset["name"])
    compressed = fetch(asset_url(asset))
    if hashlib.sha256(compressed).hexdigest() != expected:
        raise ValueError("Mihomo SHA-256 校验失败，保留已有内核")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mihomo-", dir=output.parent) as temporary:
        archive = Path(temporary) / "mihomo.gz"
        archive.write_bytes(compressed)
        binary = Path(temporary) / "mihomo"
        with gzip.open(archive, "rb") as source, binary.open("wb") as target:
            shutil.copyfileobj(source, target)
        if not binary.stat().st_size:
            raise ValueError("Mihomo 压缩包内容为空")
        binary.chmod(0o755)
        os.replace(binary, output)
    print(f"Mihomo {tag} 已安装：{output}（SHA-256 已验证）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", default="latest")
    args = parser.parse_args()
    try:
        download(args.output, args.version)
    except Exception as exc:
        raise SystemExit(
            f"Mihomo 下载失败：{exc}；可设置 SKIP_MIHOMO=1 后重试"
        ) from None
