"""Build code.zip for submission (any OS, no shell needed).

    python code/tools/package.py            # writes code.zip in the repository root
    python code/tools/package.py --check    # only verify an existing code.zip

Layout inside the zip (AGENTS.md section 6.8 / 6.9):
    README.md
    code/...                                 the runnable solution, prompts, config, tests, tools
    evaluation/evaluation_report.md          copies of code/evaluation/*.md at the top level
    evaluation/usage_report.md
Never included: dataset/, .env, .cache/, virtual environments, caches, nested archives.
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ZIP_PATH = ROOT / "code.zip"
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "venv", "node_modules"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".zip", ".log"}
SKIP_NAMES = {".env", ".DS_Store", "Thumbs.db"}
REQUIRED = ("README.md", "code/main.py", "code/requirements.txt", "code/config.json",
            "evaluation/evaluation_report.md", "evaluation/usage_report.md")
FORBIDDEN_PREFIXES = ("dataset/", ".cache/", ".env", "code/.env")


def entries() -> list[tuple[Path, str]]:
    out = [(ROOT / "README.md", "README.md")]
    for p in sorted((ROOT / "code").rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts) or p.suffix in SKIP_SUFFIXES or p.name in SKIP_NAMES:
            continue
        out.append((p, rel.as_posix()))
    for name in ("evaluation_report.md", "usage_report.md"):
        out.append((ROOT / "code" / "evaluation" / name, f"evaluation/{name}"))
    return out


def build() -> Path:
    items = entries()
    missing = [src for src, _ in items if not src.exists()]
    if missing:
        raise SystemExit("missing files: " + ", ".join(str(m.relative_to(ROOT)) for m in missing))
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src, arc in items:
            zf.write(src, arc)
    return ZIP_PATH


def check(path: Path) -> list[str]:
    problems = []
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
    for req in REQUIRED:
        if req not in names:
            problems.append(f"missing {req}")
    for n in sorted(names):
        if n.startswith(FORBIDDEN_PREFIXES) or n.endswith(".zip") or "/.env" in n or n.endswith("/.env"):
            problems.append(f"forbidden entry {n}")
        if "__pycache__" in n or n.endswith(".pyc"):
            problems.append(f"cache entry {n}")
    return problems


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true", help="verify the existing code.zip without rebuilding")
    a = p.parse_args(argv)
    if not a.check:
        build()
    problems = check(ZIP_PATH)
    with zipfile.ZipFile(ZIP_PATH) as zf:
        n = len(zf.namelist())
    print(f"{ZIP_PATH.name}: {n} entries, {ZIP_PATH.stat().st_size:,} bytes")
    for pr in problems:
        print("PROBLEM:", pr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
