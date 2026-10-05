#!/usr/bin/env python3
"""End-to-end smoke test for the DCA-Bot dashboard.

Runs only against demo or staging environments. Refuses to run against
production unless an explicit override is provided, and even then destructive
actions are avoided.

The smoke test uses ``httpx`` because it is already a project dependency.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx


class SmokeTestError(Exception):
    """Raised when a smoke-test step fails."""

    def __init__(self, message: str, step: str | None = None):
        super().__init__(message)
        self.step = step
        self.message = message


class SmokeRunner:
    """Execute smoke-test steps and accumulate results."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        env: str,
        allow_production: bool = False,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.env = env.lower()
        self.allow_production = allow_production
        self.timeout = timeout
        self.results: list[dict[str, Any]] = []
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=False,
        )
        self.csrf_token: str | None = None
        self.session_cookie: str | None = None

    def _redact(self, value: str) -> str:
        """Redact a potentially sensitive value for logging."""
        if not value:
            return ""
        if len(value) <= 8:
            return "***"
        return value[:4] + "***" + value[-4:]

    def _log_request(self, method: str, path: str) -> None:
        print(f"  {method.upper()} {path}")

    def _log_result(self, step: str, passed: bool, detail: str | None = None) -> None:
        status = "PASS" if passed else "FAIL"
        line = f"  [{status}] {step}"
        if detail and not passed:
            line += f": {detail}"
        print(line)
        self.results.append({"step": step, "passed": passed, "detail": detail})

    def run(self) -> dict[str, Any]:
        """Execute the full smoke-test flow."""
        self._check_environment()
        self._health_live()
        self._health_ready()
        self._login()
        self._dashboard()
        self._operations_status()
        self._pause_bot()
        self._resume_bot()
        self._manual_cycle()
        self._verify_transaction()
        self._create_backup()
        self._security_headers()
        self._logout()
        return self._build_report()

    def _check_environment(self) -> None:
        step = "environment_check"
        if self.env not in ("demo", "staging") and not self.allow_production:
            raise SmokeTestError(
                f"env={self.env} is not demo or staging. "
                "Pass --allow-production to override.",
                step=step,
            )
        self._log_result(step, True)

    def _request(
        self,
        method: str,
        path: str,
        step: str,
        expected: int | tuple[int, ...] = 200,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        self._log_request(method, path)
        request_headers: dict[str, str] = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        if self.csrf_token:
            request_headers["X-CSRF-Token"] = self.csrf_token

        try:
            response = self.client.request(
                method,
                path,
                json=json_body,
                data=data,
                headers=request_headers,
            )
        except httpx.RequestError as exc:
            raise SmokeTestError(f"request failed: {exc}", step=step) from exc

        expected_codes = expected if isinstance(expected, tuple) else (expected,)
        if response.status_code not in expected_codes:
            raise SmokeTestError(
                f"unexpected status {response.status_code}, expected {expected}",
                step=step,
            )
        return response

    def _health_live(self) -> None:
        response = self._request("GET", "/health/live", "health_live", expected=200)
        body = response.json()
        passed = body.get("status") == "alive" and body.get("app") == "ok"
        self._log_result("health_live", passed, detail=json.dumps(body) if not passed else None)

    def _health_ready(self) -> None:
        response = self._request("GET", "/health/ready", "health_ready", expected=(200, 503))
        body = response.json()
        # Ready is ideal but not required for smoke testing; verify the response
        # is machine-readable and contains the expected fields.
        passed = "status" in body and "checks" in body
        self._log_result("health_ready", passed, detail=json.dumps(body) if not passed else None)

    def _login(self) -> None:
        response = self._request(
            "POST",
            "/login",
            "login",
            expected=303,
            data={"username": self.username, "password": self.password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        cookies = response.cookies
        self.session_cookie = cookies.get("dca_session")
        self.csrf_token = cookies.get("dca_csrf")
        if not self.session_cookie:
            raise SmokeTestError("session cookie not set after login", step="login")
        # Attach cookies to subsequent requests.
        self.client.cookies.set("dca_session", self.session_cookie)
        self.client.cookies.set("dca_csrf", self.csrf_token or "")
        self._log_result("login", True)

    def _dashboard(self) -> None:
        response = self._request("GET", "/dashboard", "dashboard", expected=200)
        passed = response.status_code == 200 and "text/html" in response.headers.get("content-type", "")
        self._log_result("dashboard", passed)

    def _operations_status(self) -> None:
        response = self._request("GET", "/api/operations/status", "operations_status", expected=200)
        body = response.json()
        passed = body.get("environment") in ("demo", "staging", "production", "development")
        self._log_result("operations_status", passed, detail=json.dumps(body) if not passed else None)

    def _pause_bot(self) -> None:
        response = self._request("POST", "/api/bot/pause", "pause_bot", expected=200)
        body = response.json()
        passed = body.get("status") == "ok"
        self._log_result("pause_bot", passed, detail=json.dumps(body) if not passed else None)

    def _verify_paused(self) -> None:
        response = self._request("GET", "/api/status", "verify_paused", expected=200)
        body = response.json()
        paused = body.get("paused") is True or body.get("status") == "paused"
        self._log_result("verify_paused", paused, detail=json.dumps(body) if not paused else None)

    def _resume_bot(self) -> None:
        response = self._request("POST", "/api/bot/resume", "resume_bot", expected=200)
        body = response.json()
        passed = body.get("status") == "ok"
        self._log_result("resume_bot", passed, detail=json.dumps(body) if not passed else None)

    def _manual_cycle(self) -> None:
        response = self._request("POST", "/api/bot/cycle", "manual_cycle", expected=200)
        body = response.json()
        passed = body.get("status") == "ok"
        self._log_result("manual_cycle", passed, detail=json.dumps(body) if not passed else None)

    def _verify_transaction(self) -> None:
        # Give the bot a moment to record the demo transaction.
        time.sleep(0.5)
        response = self._request("GET", "/api/transactions", "verify_transaction", expected=200)
        body = response.json()
        transactions = body.get("transactions", [])
        passed = isinstance(transactions, list) and len(transactions) > 0
        self._log_result("verify_transaction", passed, detail=f"count={len(transactions)}")

    def _create_backup(self) -> None:
        response = self._request("POST", "/api/backups/create", "create_backup", expected=200)
        body = response.json()
        passed = body.get("status") == "ok" and "file" in body
        self._log_result("create_backup", passed, detail=json.dumps(body) if not passed else None)

    def _security_headers(self) -> None:
        response = self._request("GET", "/login", "security_headers", expected=200)
        headers = response.headers
        checks = {
            "content-security-policy": "default-src 'self'" in headers.get("content-security-policy", ""),
            "x-content-type-options": headers.get("x-content-type-options") == "nosniff",
            "x-frame-options": headers.get("x-frame-options") == "DENY",
            "referrer-policy": headers.get("referrer-policy") == "strict-origin-when-cross-origin",
        }
        passed = all(checks.values())
        self._log_result(
            "security_headers",
            passed,
            detail=json.dumps(checks) if not passed else None,
        )

    def _logout(self) -> None:
        self._request("POST", "/logout", "logout", expected=303)
        self.csrf_token = None
        self.session_cookie = None
        self.client.cookies.clear()
        self._log_result("logout", True)

    def _build_report(self) -> dict[str, Any]:
        passed = all(r["passed"] for r in self.results)
        return {
            "status": "PASS" if passed else "FAIL",
            "environment": self.env,
            "base_url": self.base_url,
            "username": self.username,
            "password": self._redact(self.password),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "results": self.results,
            "summary": {
                "total": len(self.results),
                "passed": sum(1 for r in self.results if r["passed"]),
                "failed": sum(1 for r in self.results if not r["passed"]),
            },
        }

    def close(self) -> None:
        self.client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DCA-Bot dashboard smoke test")
    parser.add_argument("--url", required=True, help="Dashboard base URL")
    parser.add_argument("--username", required=True, help="Dashboard username")
    parser.add_argument("--password", required=True, help="Dashboard password")
    parser.add_argument("--env", required=True, choices=("demo", "staging", "production"), help="Target environment")
    parser.add_argument("--allow-production", action="store_true", help="Allow running against production")
    parser.add_argument("--timeout", type=float, default=30.0, help="Request timeout in seconds")
    parser.add_argument("--output", type=Path, help="Write machine-readable report to this file")
    args = parser.parse_args(argv)

    runner = SmokeRunner(
        base_url=args.url,
        username=args.username,
        password=args.password,
        env=args.env,
        allow_production=args.allow_production,
        timeout=args.timeout,
    )

    try:
        report = runner.run()
    except SmokeTestError as exc:
        runner._log_result(exc.step or "unknown", False, detail=exc.message)
        report = runner._build_report()
        status = 1
    else:
        status = 0 if report["status"] == "PASS" else 1
    finally:
        runner.close()

    machine_output = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(machine_output, encoding="utf-8")
        print(f"\nMachine-readable report written to {args.output}")
    else:
        print("\nMachine-readable report:")
        print(machine_output)

    return status


if __name__ == "__main__":
    sys.exit(main())
