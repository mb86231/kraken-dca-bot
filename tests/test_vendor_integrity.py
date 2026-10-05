"""Tests for vendored static-asset integrity.

These tests ensure the local Chart.js files have not been modified and that the
verification script catches drift or undocumented files.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.verify_vendor_checksums import verify_manifest


VENDOR_DIR = Path(__file__).parent.parent / "web" / "static" / "vendor" / "chart.js"
MANIFEST_PATH = VENDOR_DIR / "vendor-manifest.json"


def test_chart_js_manifest_matches_files():
    """The committed vendor files must match the recorded checksums."""
    problems = verify_manifest(MANIFEST_PATH)
    assert problems == [], "\n".join(problems)


def test_verify_manifest_catches_checksum_mismatch(tmp_path: Path):
    manifest = {
        "files": [
            {
                "filename": "example.js",
                "sha256": "0" * 64,
            }
        ]
    }
    vendor_dir = tmp_path / "vendor"
    vendor_dir.mkdir()
    (vendor_dir / "example.js").write_text("console.log('hello');")
    manifest_path = vendor_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    problems = verify_manifest(manifest_path)
    assert any("Checksum mismatch" in p for p in problems)


def test_verify_manifest_rejects_undocumented_js(tmp_path: Path):
    vendor_dir = tmp_path / "vendor"
    vendor_dir.mkdir()
    (vendor_dir / "known.js").write_text("// known")
    (vendor_dir / "surprise.js").write_text("// undocumented")
    manifest_path = vendor_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "filename": "known.js",
                        "sha256": "b6da908fcf29749bf0d099f8c76d0c392ed49e0f1fc9d6049b48b4ed968f0680",
                    }
                ]
            }
        )
    )

    problems = verify_manifest(manifest_path)
    assert any("Undocumented vendor file" in p for p in problems)


def test_verify_manifest_reports_missing_file(tmp_path: Path):
    vendor_dir = tmp_path / "vendor"
    vendor_dir.mkdir()
    manifest_path = vendor_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "filename": "missing.js",
                        "sha256": "0" * 64,
                    }
                ]
            }
        )
    )

    problems = verify_manifest(manifest_path)
    assert any("Missing file" in p for p in problems)


def test_vendor_files_are_tracked_in_git():
    """Sanity check: the manifest and every listed file must exist on disk."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for entry in manifest.get("files", []):
        file_path = VENDOR_DIR / entry["filename"]
        assert file_path.exists(), f"{entry['filename']} is missing"
