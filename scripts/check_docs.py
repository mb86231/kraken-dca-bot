#!/usr/bin/env python3
"""Validate relative Markdown links in the repository.

Usage:
    python scripts/check_docs.py

Exits with 0 if all relative links resolve, 1 otherwise.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Match Markdown links [text](url) and images ![alt](url).
LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")

# Schemes we do not validate.
EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "tel:")

# Files we skip entirely.
SKIP_FILES: set[str] = set()


def anchor_slug(text: str) -> str:
    """Convert a heading line to a GitHub-style anchor slug."""
    text = text.strip().lstrip("#").strip()
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    # Match github-slugger: one hyphen per whitespace character, no
    # collapsing. "Teil A — B" becomes "teil-a--b" (double hyphen), not
    # "teil-a-b".
    text = re.sub(r"\s", "-", text).strip("-")
    return text


def collect_anchors(markdown: str) -> set[str]:
    """Return the set of anchor ids that can be linked inside a Markdown file."""
    anchors: set[str] = set()
    for line in markdown.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+)", line)
        if match:
            anchors.add(anchor_slug(match.group(2)))
        # Also support explicit HTML anchors: <a name="...">
        for name in re.findall(r'<a\s+name="([^"]+)"', line):
            anchors.add(name)
    return anchors


def resolve_link(source: Path, url: str) -> tuple[Path | None, str | None]:
    """Resolve a relative link against its source file.

    Returns (target_path, anchor) where anchor may be None.
    """
    if "#" in url:
        path_part, anchor = url.split("#", 1)
    else:
        path_part, anchor = url, None

    if not path_part:
        # Anchor-only link inside the same file.
        return source, anchor

    target = (source.parent / path_part).resolve()
    return target, anchor


def check_file(md_file: Path, repo_root: Path) -> list[str]:
    """Check all relative links in a single Markdown file.

    Returns a list of human-readable problem strings.
    """
    problems: list[str] = []
    text = md_file.read_text(encoding="utf-8")

    anchors = collect_anchors(text)

    for line_no, line in enumerate(text.splitlines(), start=1):
        for _, raw_url in LINK_RE.findall(line):
            url = raw_url.strip()
            if not url or url.startswith(EXTERNAL_SCHEMES):
                continue
            if url.startswith("#"):
                # Same-file anchor.
                anchor = url[1:]
                if anchor not in anchors:
                    problems.append(
                        f"{md_file.relative_to(repo_root)}:{line_no}: "
                        f"broken same-file anchor '#{anchor}'"
                    )
                continue

            try:
                target, anchor = resolve_link(md_file, url)
            except Exception as exc:  # pragma: no cover - defensive
                problems.append(
                    f"{md_file.relative_to(repo_root)}:{line_no}: "
                    f"cannot resolve '{raw_url}' ({exc})"
                )
                continue

            if target is None:
                continue

            if not target.exists():
                problems.append(
                    f"{md_file.relative_to(repo_root)}:{line_no}: "
                    f"missing target '{raw_url}'"
                )
                continue

            if target.is_dir():
                # Linking to a directory is fine if it contains a README/index.
                if not (target / "README.md").exists() and not (target / "index.md").exists():
                    problems.append(
                        f"{md_file.relative_to(repo_root)}:{line_no}: "
                        f"directory target '{raw_url}' has no README.md or index.md"
                    )
                continue

            if anchor and target.suffix.lower() == ".md":
                target_anchors = collect_anchors(target.read_text(encoding="utf-8"))
                if anchor not in target_anchors:
                    problems.append(
                        f"{md_file.relative_to(repo_root)}:{line_no}: "
                        f"broken anchor '{anchor}' in '{raw_url}'"
                    )

    return problems


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    markdown_files = sorted(repo_root.rglob("*.md"))

    all_problems: list[str] = []
    checked = 0

    for md_file in markdown_files:
        if md_file.name in SKIP_FILES:
            continue
        rel_parts = md_file.relative_to(repo_root).parts
        # Skip generated/dependency directories that are not project docs.
        if any(
            part.startswith(".") or part in {"node_modules", "vendor", "__pycache__"}
            for part in rel_parts
        ):
            continue
        checked += 1
        all_problems.extend(check_file(md_file, repo_root))

    if all_problems:
        print("Broken Markdown links found:")
        for problem in all_problems:
            print(f"  - {problem}")
        print(f"\nChecked {checked} Markdown file(s); {len(all_problems)} problem(s).")
        return 1

    print(f"OK — checked {checked} Markdown file(s); all relative links resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
