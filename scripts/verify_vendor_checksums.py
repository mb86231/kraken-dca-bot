#!/usr/bin/env python3
"""Verify SHA-256 checksums of vendored JavaScript dependencies.

Usage:
    python scripts/verify_vendor_checksums.py [path/to/vendor-manifest.json]

If no manifest path is given, the default
``web/static/vendor/chart.js/vendor-manifest.json`` is used.

Exits with 0 when every listed file is present and matches its recorded
hash, and no undocumented ``.js`` files exist in the vendor directory.
Exits with 1 otherwise.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


DEFAULT_MANIFEST = Path("web/static/vendor/chart.js/vendor-manifest.json")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_manifest(manifest_path: Path) -> list[str]:
    """Return a list of problem descriptions; empty means success."""
    problems: list[str] = []

    if not manifest_path.exists():
        return [f"Manifest not found: {manifest_path}"]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"Manifest is not valid JSON: {exc}"]

    vendor_dir = manifest_path.parent
    entries = manifest.get("files", [])
    documented_files: set[str] = set()

    for entry in entries:
        filename = entry.get("filename")
        if not filename:
            problems.append("Manifest entry missing 'filename' field")
            continue

        documented_files.add(filename)
        file_path = vendor_dir / filename
        expected = entry.get("sha256", "").lower().strip()

        if not file_path.exists():
            problems.append(f"Missing file: {filename}")
            continue

        if not expected:
            problems.append(f"No recorded SHA-256 for {filename}")
            continue

        actual = _sha256_file(file_path)
        if actual != expected:
            problems.append(
                f"Checksum mismatch for {filename}\n"
                f"  expected: {expected}\n"
                f"  actual:   {actual}"
            )

    # Reject undocumented .js files in the vendor directory.
    for js_file in sorted(vendor_dir.glob("*.js")):
        if js_file.name not in documented_files:
            problems.append(f"Undocumented vendor file: {js_file.name}")

    return problems


def main(argv: list[str]) -> int:
    manifest_path = Path(argv[0]) if argv else DEFAULT_MANIFEST
    manifest_path = manifest_path.resolve()

    problems = verify_manifest(manifest_path)

    if problems:
        print(f"Vendor integrity check failed for {manifest_path}", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"Vendor integrity check passed: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
