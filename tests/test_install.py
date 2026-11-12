import contextlib
import gzip
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.download_mihomo import (
    API,
    REPOSITORY,
    download,
    expected_checksum,
    select_asset,
)


class MihomoDownloadTests(unittest.TestCase):
    def test_api_digest_without_checksum_files(self):
        responses, assets = self.fixture(checksum="missing")
        compressed = responses[assets[0]["browser_download_url"]]
        assets[0]["digest"] = "sha256:" + hashlib.sha256(compressed).hexdigest()
        release = json.dumps({"tag_name": "v1.2.3", "assets": assets}).encode()
        responses[API + "latest"] = release
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mihomo"
            with patch(
                "scripts.download_mihomo.fetch", side_effect=responses.__getitem__
            ) as fetch, contextlib.redirect_stdout(io.StringIO()):
                download(output, arch="x86_64")
            self.assertEqual(output.read_bytes(), b"fixture mihomo binary")
            self.assertEqual(fetch.call_count, 2)

    def test_digest_priority_and_invalid_digest_fallback(self):
        responses, assets = self.fixture()
        assets[0]["digest"] = "sha256:" + "a" * 64
        with patch("scripts.download_mihomo.fetch") as fetch:
            self.assertEqual(expected_checksum(assets, assets[0]["name"]), "a" * 64)
            fetch.assert_not_called()
        for invalid in (None, "", "sha256:bad", "sha512:" + "a" * 64):
            assets[0]["digest"] = invalid
            with patch(
                "scripts.download_mihomo.fetch", side_effect=responses.__getitem__
            ):
                self.assertEqual(
                    expected_checksum(assets, assets[0]["name"]),
                    hashlib.sha256(
                        responses[assets[0]["browser_download_url"]]
                    ).hexdigest(),
                )

    def test_digest_mismatch_preserves_binary(self):
        responses, assets = self.fixture(checksum="missing")
        assets[0]["digest"] = "sha256:" + "0" * 64
        responses[API + "latest"] = json.dumps(
            {"tag_name": "v1.2.3", "assets": assets}
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mihomo"
            output.write_bytes(b"original")
            with patch(
                "scripts.download_mihomo.fetch", side_effect=responses.__getitem__
            ), self.assertRaisesRegex(ValueError, "SHA-256"):
                download(output, arch="x86_64")
            self.assertEqual(output.read_bytes(), b"original")

    def fixture(self, checksum="valid", combined=False):
        name = "mihomo-linux-amd64-compatible-v1.2.3.gz"
        compressed = gzip.compress(b"fixture mihomo binary")
        digest = (
            hashlib.sha256(compressed).hexdigest() if checksum == "valid" else "0" * 64
        )
        checksum_name = "sha256sums.txt" if combined else name + ".sha256sum"
        checksum_body = f"{digest}  {name}\n".encode() if combined else digest.encode()

        def asset(name):
            return {"name": name, "browser_download_url": REPOSITORY + "v1.2.3/" + name}

        assets = [asset(name)]
        if checksum != "missing":
            assets.append(asset(checksum_name))
        release = {"tag_name": "v1.2.3", "assets": assets}
        responses = {
            API + "latest": json.dumps(release).encode(),
            API + "tags/v1.2.3": json.dumps(release).encode(),
            asset(name)["browser_download_url"]: compressed,
            asset(checksum_name)["browser_download_url"]: checksum_body,
        }
        return responses, assets

    def test_verified_download_latest_pinned_and_combined_checksums(self):
        for version, combined in [("latest", False), ("1.2.3", True)]:
            with self.subTest(
                version=version
            ), tempfile.TemporaryDirectory() as directory:
                responses, _ = self.fixture(combined=combined)
                output = Path(directory) / "mihomo"
                with patch(
                    "scripts.download_mihomo.fetch", side_effect=responses.__getitem__
                ), contextlib.redirect_stdout(io.StringIO()):
                    download(output, version, "x86_64")
                self.assertEqual(output.read_bytes(), b"fixture mihomo binary")
                self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_missing_or_wrong_checksum_preserves_existing_binary(self):
        for checksum in ("missing", "wrong"):
            with self.subTest(
                checksum=checksum
            ), tempfile.TemporaryDirectory() as directory:
                responses, _ = self.fixture(checksum)
                output = Path(directory) / "mihomo"
                output.write_bytes(b"old binary")
                with patch(
                    "scripts.download_mihomo.fetch", side_effect=responses.__getitem__
                ), self.assertRaisesRegex(ValueError, "SHA-256"):
                    download(output, arch="x86_64")
                self.assertEqual(output.read_bytes(), b"old binary")

    def test_network_and_archive_failures_preserve_existing_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mihomo"
            output.write_bytes(b"old binary")
            with patch(
                "scripts.download_mihomo.fetch",
                side_effect=OSError("fixture network failure"),
            ), self.assertRaises(OSError):
                download(output, arch="x86_64")
            responses, assets = self.fixture()
            corrupt = b"not gzip"
            responses[assets[0]["browser_download_url"]] = corrupt
            responses[assets[1]["browser_download_url"]] = (
                hashlib.sha256(corrupt).hexdigest().encode()
            )
            with patch(
                "scripts.download_mihomo.fetch", side_effect=responses.__getitem__
            ), self.assertRaises(OSError):
                download(output, arch="x86_64")
            self.assertEqual(output.read_bytes(), b"old binary")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_architecture_selection(self):
        for arch, machine in [
            ("aarch64", "arm64"),
            ("arm64", "arm64"),
            ("armv7l", "armv7"),
            ("x86_64", "amd64"),
        ]:
            asset = {"name": f"mihomo-linux-{machine}-v1.2.3.gz"}
            self.assertEqual(select_asset([asset], arch, "v1.2.3"), asset)
        with self.assertRaisesRegex(ValueError, "架构"):
            select_asset([], "unknown", "v1.2.3")
        with self.assertRaisesRegex(ValueError, "缺少"):
            select_asset([], "arm64", "v1.2.3")

    def test_unrelated_checksum_and_nonofficial_asset_are_rejected(self):
        _, assets = self.fixture(combined=True)
        with patch(
            "scripts.download_mihomo.fetch",
            return_value=("a" * 64 + "  other.gz").encode(),
        ), self.assertRaises(ValueError):
            expected_checksum(assets, assets[0]["name"])
        assets[1]["browser_download_url"] = "https://example.com/checksums"
        with self.assertRaisesRegex(ValueError, "官方"):
            expected_checksum(assets, assets[0]["name"])


class FrontendLoginTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证浏览器令牌行为")
    def test_login_storage_behavior(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("test_frontend.js"))],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
