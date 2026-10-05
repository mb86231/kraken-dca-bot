"""Tests for small operational scripts.

These tests import and exercise the helper functions in ``scripts/`` without
writing to production paths or calling live APIs.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from bot.preflight import CheckResult, CheckStatus, PreflightResult
from scripts import (
    check_docs,
    generate_demo_data,
    generate_password_hash,
    migrate_to_sqlite,
    preflight_production,
    reset_demo_data,
    run_web_demo,
    validate_live_trade,
    verify_vendor_checksums,
)


class TestVerifyVendorChecksums:
    def test_verify_manifest_passes_with_valid_hashes(self, tmp_path: Path):
        vendor_dir = tmp_path / "vendor"
        vendor_dir.mkdir()
        js_file = vendor_dir / "lib.js"
        js_file.write_text("console.log('hello');")
        manifest = {
            "files": [
                {
                    "filename": "lib.js",
                    "sha256": verify_vendor_checksums._sha256_file(js_file),
                    "version": "1.0.0",
                }
            ]
        }
        manifest_path = vendor_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        assert verify_vendor_checksums.verify_manifest(manifest_path) == []

    def test_verify_manifest_detects_checksum_mismatch(self, tmp_path: Path):
        vendor_dir = tmp_path / "vendor"
        vendor_dir.mkdir()
        (vendor_dir / "lib.js").write_text("console.log('hello');")
        manifest_path = vendor_dir / "manifest.json"
        manifest_path.write_text(json.dumps({"files": [{"filename": "lib.js", "sha256": "0" * 64}]}))
        problems = verify_vendor_checksums.verify_manifest(manifest_path)
        assert any("Checksum mismatch" in p for p in problems)

    def test_verify_manifest_detects_missing_file(self, tmp_path: Path):
        vendor_dir = tmp_path / "vendor"
        vendor_dir.mkdir()
        manifest_path = vendor_dir / "manifest.json"
        manifest_path.write_text(json.dumps({"files": [{"filename": "missing.js", "sha256": "0" * 64}]}))
        problems = verify_vendor_checksums.verify_manifest(manifest_path)
        assert any("Missing file" in p for p in problems)

    def test_verify_manifest_detects_undocumented_js(self, tmp_path: Path):
        vendor_dir = tmp_path / "vendor"
        vendor_dir.mkdir()
        (vendor_dir / "extra.js").write_text("x")
        manifest_path = vendor_dir / "manifest.json"
        manifest_path.write_text(json.dumps({"files": []}))
        problems = verify_vendor_checksums.verify_manifest(manifest_path)
        assert any("Undocumented vendor file" in p for p in problems)

    def test_verify_manifest_missing_manifest(self, tmp_path: Path):
        missing = tmp_path / "manifest.json"
        problems = verify_vendor_checksums.verify_manifest(missing)
        assert any("Manifest not found" in p for p in problems)

    def test_verify_manifest_invalid_json(self, tmp_path: Path):
        vendor_dir = tmp_path / "vendor"
        vendor_dir.mkdir()
        manifest_path = vendor_dir / "manifest.json"
        manifest_path.write_text("not-json")
        problems = verify_vendor_checksums.verify_manifest(manifest_path)
        assert any("not valid JSON" in p for p in problems)

    def test_main_exits_zero_on_valid_manifest(self, tmp_path: Path):
        vendor_dir = tmp_path / "vendor"
        vendor_dir.mkdir()
        js_file = vendor_dir / "lib.js"
        js_file.write_text("x")
        manifest_path = vendor_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps({"files": [{"filename": "lib.js", "sha256": verify_vendor_checksums._sha256_file(js_file)}]})
        )
        assert verify_vendor_checksums.main([str(manifest_path)]) == 0

    def test_main_exits_one_on_failure(self, tmp_path: Path):
        vendor_dir = tmp_path / "vendor"
        vendor_dir.mkdir()
        manifest_path = vendor_dir / "manifest.json"
        manifest_path.write_text(json.dumps({"files": [{"filename": "missing.js", "sha256": "0" * 64}]}))
        assert verify_vendor_checksums.main([str(manifest_path)]) == 1


class TestGeneratePasswordHash:
    def test_main_with_password_arg(self, capsys):
        assert generate_password_hash.main(["my-secret-password"]) == 0
        captured = capsys.readouterr()
        # stdout must be hash-only so it can be captured in shell variables
        hash_lines = [line for line in captured.out.splitlines() if line.strip()]
        assert len(hash_lines) == 1 and hash_lines[0].startswith("$2")
        # the usage hint goes to stderr
        assert "Set this value as WEB_UI_PASSWORD_HASH" in captured.err

    def test_main_with_mismatched_passwords(self, monkeypatch, capsys):
        inputs = iter(["pass1", "pass2"])
        monkeypatch.setattr("getpass.getpass", lambda _prompt: next(inputs))
        assert generate_password_hash.main([]) == 1
        captured = capsys.readouterr()
        assert "do not match" in captured.err.lower()


class TestResetDemoData:
    def test_main_writes_empty_transactions(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "transactions.json"
        monkeypatch.setattr("scripts.reset_demo_data.Path", lambda p: target if p == "data/transactions.json" else Path(p))
        assert reset_demo_data.main() == 0
        assert json.loads(target.read_text()) == []


class TestGenerateDemoData:
    def test_main_writes_demo_transactions(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "transactions.json"
        monkeypatch.setattr("scripts.generate_demo_data.Path", lambda p: target if p == "data/transactions.json" else Path(p))
        monkeypatch.setenv("DEMO_MODE", "false")  # main forces it to true
        monkeypatch.setenv("DEMO_COUNT", "5")
        assert generate_demo_data.main() == 0
        data = json.loads(target.read_text())
        assert len(data) == 5
        assert all(t["simulated"] is True for t in data)


class TestCheckDocs:
    def test_check_file_same_file_anchor(self, tmp_path: Path):
        md = tmp_path / "doc.md"
        md.write_text("# Heading\n[link](#heading)\n")
        assert check_docs.check_file(md, tmp_path) == []

    def test_check_file_broken_same_file_anchor(self, tmp_path: Path):
        md = tmp_path / "doc.md"
        md.write_text("# Heading\n[link](#missing)\n")
        problems = check_docs.check_file(md, tmp_path)
        assert any("broken same-file anchor" in p for p in problems)

    def test_check_file_missing_target(self, tmp_path: Path):
        md = tmp_path / "doc.md"
        md.write_text("[link](other.md)\n")
        problems = check_docs.check_file(md, tmp_path)
        assert any("missing target" in p for p in problems)

    def test_check_file_directory_without_readme(self, tmp_path: Path):
        d = tmp_path / "emptydir"
        d.mkdir()
        md = tmp_path / "doc.md"
        md.write_text(f"[link]({d.name})\n")
        problems = check_docs.check_file(md, tmp_path)
        assert any("directory target" in p for p in problems)

    def test_main_finds_no_problems_in_clean_repo(self, tmp_path: Path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        md_file = root / "README.md"

        class _FakePath(type(Path())):  # type: ignore[misc]
            def __new__(cls, *_args, **_kwargs):
                return root

            def resolve(self):
                return root

            def rglob(self, _pattern):
                return [md_file]

        monkeypatch.setattr("scripts.check_docs.Path", _FakePath)
        monkeypatch.setattr("scripts.check_docs.check_file", lambda _md_file, _repo_root: [])
        assert check_docs.main() == 0

    def test_main_reports_problems(self, tmp_path: Path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        md_file = root / "README.md"

        class _FakePath(type(Path())):  # type: ignore[misc]
            def __new__(cls, *_args, **_kwargs):
                return root

            def resolve(self):
                return root

            def rglob(self, _pattern):
                return [md_file]

        monkeypatch.setattr("scripts.check_docs.Path", _FakePath)
        monkeypatch.setattr("scripts.check_docs.check_file", lambda _md_file, _repo_root: ["broken link"])
        assert check_docs.main() == 1

    def test_collect_anchors_with_html_anchor(self):
        anchors = check_docs.collect_anchors('<a name="custom"></a>\n# Heading\n')
        assert "custom" in anchors
        assert "heading" in anchors

    def test_resolve_link_same_file_anchor(self, tmp_path: Path):
        md = tmp_path / "doc.md"
        target, anchor = check_docs.resolve_link(md, "#section")
        assert target == md
        assert anchor == "section"

    def test_check_file_directory_with_readme(self, tmp_path: Path):
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "README.md").write_text("# Subdir\n")
        md = tmp_path / "doc.md"
        md.write_text(f"[link]({subdir.name})\n")
        assert check_docs.check_file(md, tmp_path) == []

    def test_check_file_broken_anchor_in_target(self, tmp_path: Path):
        target = tmp_path / "target.md"
        target.write_text("# Heading\n")
        md = tmp_path / "doc.md"
        md.write_text("[link](target.md#missing)\n")
        problems = check_docs.check_file(md, tmp_path)
        assert any("broken anchor" in p for p in problems)


class TestPreflightProduction:
    @pytest.fixture
    def passing_result(self) -> PreflightResult:
        return PreflightResult(
            preflight_id="p1",
            ran_at="2024-01-01T00:00:00+00:00",
            expires_at="2024-01-01T01:00:00+00:00",
            config_hash="abc123",
            overall=CheckStatus.PASS,
            can_place_live_orders=True,
            checks=[CheckResult(name="env", status=CheckStatus.PASS, message="ok")],
        )

    def test_main_in_demo_mode(self, monkeypatch, capsys):
        monkeypatch.setattr(preflight_production, "is_demo_mode", lambda: True)
        assert preflight_production.main(["--json"]) == 1
        captured = capsys.readouterr()
        assert "Demo or staging mode" in captured.out

    def test_main_config_load_failure(self, monkeypatch, capsys):
        monkeypatch.setattr(preflight_production, "is_demo_mode", lambda: False)
        monkeypatch.setattr(preflight_production, "Config", Mock(side_effect=ValueError("bad config")))
        assert preflight_production.main([]) == 1
        captured = capsys.readouterr()
        assert "Failed to load configuration" in captured.out
        assert "bad config" in captured.out

    def test_main_preflight_pass(self, monkeypatch, capsys, passing_result):
        monkeypatch.setattr(preflight_production, "is_demo_mode", lambda: False)
        monkeypatch.setattr(preflight_production, "Config", Mock())
        mock_preflight = Mock()
        mock_preflight.return_value.run.return_value = passing_result
        monkeypatch.setattr(preflight_production, "ProductionPreflight", mock_preflight)
        write_mock = Mock()
        monkeypatch.setattr(preflight_production, "write_preflight_result", write_mock)
        assert (
            preflight_production.main(
                ["--json", "--telegram-test", "--pair", "XBTUSD", "--amount", "10"]
            )
            == 0
        )
        captured = capsys.readouterr()
        assert "PASS" in captured.out
        mock_preflight.assert_called_once()
        write_mock.assert_called_once()

    def test_main_preflight_failure(self, monkeypatch, capsys, passing_result):
        failing = replace(
            passing_result,
            overall=CheckStatus.FAIL,
            can_place_live_orders=False,
            failures=["something is wrong"],
        )
        monkeypatch.setattr(preflight_production, "is_demo_mode", lambda: False)
        monkeypatch.setattr(preflight_production, "Config", Mock())
        mock_preflight = Mock()
        mock_preflight.return_value.run.return_value = failing
        monkeypatch.setattr(preflight_production, "ProductionPreflight", mock_preflight)
        monkeypatch.setattr(preflight_production, "write_preflight_result", Mock())
        assert preflight_production.main([]) == 1
        captured = capsys.readouterr()
        assert "something is wrong" in captured.out

    def test_main_preflight_exception(self, monkeypatch, capsys):
        monkeypatch.setattr(preflight_production, "is_demo_mode", lambda: False)
        monkeypatch.setattr(preflight_production, "Config", Mock())
        mock_preflight = Mock()
        mock_preflight.return_value.run.side_effect = RuntimeError("boom")
        monkeypatch.setattr(preflight_production, "ProductionPreflight", mock_preflight)
        monkeypatch.setattr(preflight_production, "write_preflight_result", Mock())
        assert preflight_production.main([]) == 1
        captured = capsys.readouterr()
        assert "Preflight failed" in captured.out


class TestValidateLiveTrade:
    def test_check_environment_accepts_valid(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("DEMO_MODE", "false")
        monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
        monkeypatch.setenv("KRAKEN_API_KEY", "key")
        monkeypatch.setenv("KRAKEN_API_SECRET", "secret")
        assert validate_live_trade._check_environment() is None

    def test_check_environment_rejects_demo(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("DEMO_MODE", "true")
        with pytest.raises(SystemExit) as exc:
            validate_live_trade._check_environment()
        assert exc.value.code == 1

    def test_main_below_minimum_amount(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["validate_live_trade.py", "--amount", "1"])
        with pytest.raises(SystemExit) as exc:
            validate_live_trade.main()
        assert exc.value.code == 1


class TestRunWebDemo:
    def test_main_missing_password_hash(self, monkeypatch, capsys):
        monkeypatch.delenv("WEB_UI_PASSWORD_HASH", raising=False)
        monkeypatch.delenv("SESSION_SECRET", raising=False)
        assert run_web_demo.main() == 1
        assert "WEB_UI_PASSWORD_HASH" in capsys.readouterr().err

    def test_main_missing_session_secret(self, monkeypatch, capsys):
        monkeypatch.setenv("WEB_UI_PASSWORD_HASH", "hash")
        monkeypatch.delenv("SESSION_SECRET", raising=False)
        assert run_web_demo.main() == 1
        assert "SESSION_SECRET" in capsys.readouterr().err

    def test_main_starts_uvicorn(self, monkeypatch):
        monkeypatch.setenv("WEB_UI_PASSWORD_HASH", "hash")
        monkeypatch.setenv("SESSION_SECRET", "secret")
        mock_run = Mock()
        monkeypatch.setattr("uvicorn.run", mock_run)
        assert run_web_demo.main() == 0
        mock_run.assert_called_once()


class TestMigrateToSqlite:
    def test_main_no_transactions_file(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert migrate_to_sqlite.main() == 0
        assert "No transactions.json found" in capsys.readouterr().out

    def test_main_migrates_transactions(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        txn = {
            "id": "t1",
            "date": "2024-01-01",
            "trading_pair": "XBTCHF",
            "amount": 0.001,
            "price": 50000.0,
        }
        (data_dir / "transactions.json").write_text(json.dumps([txn]))
        assert migrate_to_sqlite.main() == 0
        conn = sqlite3.connect(data_dir / "transactions.db")
        try:
            count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            assert count == 1
        finally:
            conn.close()
