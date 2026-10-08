"""Regression tests for the DOM-XSS hardening of the web dashboard.

Two layers:

1. Static guard — no template literal with dynamic ``${...}`` interpolations
   may be assigned to ``innerHTML`` in any Jinja template. Dynamic values must
   be rendered via DOM APIs (``textContent``/``replaceChildren``) or the
   shared helpers in ``web/static/js/dom-safe.js``.
2. Behavioural check — the shared ``escapeHtml`` helper neutralises
   representative XSS payloads (markup injection and attribute breakout),
   executed with Node.js against the real ``dom-safe.js``.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE_AVAILABLE = shutil.which("node") is not None

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / "web" / "templates"
DOM_SAFE_JS = REPO_ROOT / "web" / "static" / "js" / "dom-safe.js"

# Captures the expression assigned to innerHTML up to the terminating
# semicolon. Template literals in this codebase never contain semicolons.
INNER_HTML_ASSIGNMENT = re.compile(r"\.innerHTML\s*=\s*(.*?);", re.DOTALL)


def test_no_dynamic_template_interpolation_into_innerhtml() -> None:
    offenders: list[str] = []
    for path in sorted(TEMPLATE_DIR.glob("*.html")):
        source = path.read_text(encoding="utf-8")
        for match in INNER_HTML_ASSIGNMENT.finditer(source):
            expression = match.group(1)
            if "`" in expression and "${" in expression:
                line = source[: match.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}")
    assert not offenders, (
        "Dynamic template interpolation assigned to innerHTML (stored/reflected "
        "XSS risk). Render via textContent/replaceChildren or the dom-safe.js "
        f"helpers instead: {', '.join(offenders)}"
    )


def test_base_template_loads_dom_safe_helpers() -> None:
    base = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")
    assert "dom-safe.js" in base, "base.html must load dom-safe.js before app.js"


NODE_CHECK_SCRIPT = r"""
const assert = require('assert');
const fs = require('fs');

global.window = {};
eval(fs.readFileSync(process.argv[2], 'utf8'));
const escapeHtml = global.window.escapeHtml;
assert.strictEqual(typeof escapeHtml, 'function', 'escapeHtml must be exported');

// Exact-output assertions.
assert.strictEqual(escapeHtml('<b>&"\'</b>'), '&lt;b&gt;&amp;&quot;&#39;&lt;/b&gt;');
assert.strictEqual(escapeHtml(null), '');
assert.strictEqual(escapeHtml(undefined), '');
assert.strictEqual(escapeHtml(42), '42');

// Payload sweep: nothing may survive that could break out of a text node
// or a double-quoted attribute.
const payloads = [
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  '"><script>alert(1)</script>',
  "' onmouseover='alert(1)",
  '"><svg onload=alert(1)>',
  '&lt;script&gt;',
  'plain text',
];
for (const p of payloads) {
  const out = escapeHtml(p);
  for (const ch of ['<', '>', '"', "'"]) {
    assert.ok(!out.includes(ch), `payload ${JSON.stringify(p)} kept ${ch} in ${out}`);
  }
}
console.log('escapeHtml OK');
"""


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node.js not available on PATH")
def test_escape_html_neutralises_xss_payloads(tmp_path: Path) -> None:
    script = tmp_path / "check_escape_html.js"
    script.write_text(NODE_CHECK_SCRIPT, encoding="utf-8")
    node = shutil.which("node") or "node"
    cmd = [node, str(script), str(DOM_SAFE_JS)]
    if node.lower().endswith((".cmd", ".bat")):
        # CreateProcess cannot execute CMD shims directly — go through cmd.exe.
        cmd = ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline(cmd)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"escapeHtml payload check failed:\n{result.stdout}\n{result.stderr}"
    )
