#!/usr/bin/env python3
"""Consistency check of the shared agent memory (stdlib only).

Run from anywhere:  python3 .agents/scripts/check_memory.py
Exit code 1 when a hard problem is found. Checks:
  - referenced repo paths and `path::symbol` references still exist
  - size budgets (warnings)
  - privacy patterns (emails, URLs, IP addresses, long hex secrets, local-only folders)
  - Claude and Codex agent profiles mirror each other
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "custom_components" / "vtherm_smartpi"
SEARCH_BASES = [ROOT, PKG, PKG / "smartpi", ROOT / ".agents", ROOT / "tests"]
MD_FILES = [ROOT / "AGENTS.md", ROOT / "CLAUDE.md", *sorted((ROOT / ".agents").rglob("*.md"))]
MAX_CONTEXT_LINES = 400
MAX_AGENTS_BYTES = 24_000
# Sections listing names that intentionally do not exist.
SKIP_HEADING = re.compile(r"(do not exist|removed code|removed / rejected|rejected approaches)", re.I)
PATHLIKE = re.compile(r"^[\w./-]+\.(py|md|json|toml|yaml|yml|js)(::[\w.]+)?$")
# Files of sibling repositories or of other machines: not checked here.
EXTERNAL_PREFIXES = ("vtherm_api/", "versatile_thermostat/", "equinox", "dist/", "src/", "thermostat_", "cycle_scheduler", "interfaces.py")
PRIVACY = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "url": re.compile(r"https?://"),
    "ipv4": re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    "hex-secret": re.compile(r"\b[0-9a-f]{32,}\b"),
    "local-bench-folder": re.compile(r"bench_work"),
}
errors: list[str] = []
warnings: list[str] = []


def resolve(token: str, md: Path) -> Path | None:
    for base in [md.parent, *SEARCH_BASES]:
        p = (base / token).resolve()
        if p.exists():
            return p
    if "/" not in token:  # bare file name: look it up anywhere in the repo
        for d in (PKG, ROOT / "tests", ROOT / "documentation", ROOT / "cards", ROOT / ".agents"):
            for p in d.rglob(token):
                if "__pycache__" not in p.parts:
                    return p
    return None


def check_refs(md: Path) -> None:
    skipping = False
    for n, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("#"):
            skipping = bool(SKIP_HEADING.search(line))
        if skipping:
            continue
        for tok in re.findall(r"`([^`]+)`", line):
            if any(c in tok for c in "<>*{}$ ()=,|") or "..." in tok or tok.startswith(("-", "/")):
                continue
            if not PATHLIKE.match(tok):
                continue
            path, _, sym = tok.partition("::")
            if path.startswith(EXTERNAL_PREFIXES):
                continue
            if path.rstrip("/") in {"plans", "dev_tests", "tools", "bench_work"}:
                continue
            target = resolve(path, md)
            if target is None:
                errors.append(f"{md.relative_to(ROOT)}:{n}: missing path `{tok}`")
            elif sym and target.is_file():
                name = sym.split(".")[-1]
                # A bare file name can match several files (const.py): accept a hit in any.
                cands = list(PKG.rglob(path)) if "/" not in path else [target]
                if not any(re.search(rf"\b{re.escape(name)}\b", c.read_text(encoding="utf-8", errors="ignore")) for c in cands):
                    errors.append(f"{md.relative_to(ROOT)}:{n}: symbol `{tok}` not found in file")


def check_privacy(md: Path) -> None:
    for n, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        for label, rx in PRIVACY.items():
            if rx.search(line):
                errors.append(f"{md.relative_to(ROOT)}:{n}: privacy pattern ({label})")


def check_sizes() -> None:
    agents = ROOT / "AGENTS.md"
    if agents.stat().st_size > MAX_AGENTS_BYTES:
        warnings.append(f"AGENTS.md is {agents.stat().st_size} bytes (budget {MAX_AGENTS_BYTES}; Codex stops at 32 KiB)")
    for md in sorted((ROOT / ".agents" / "context").glob("*.md")):
        lines = len(md.read_text(encoding="utf-8").splitlines())
        if lines > MAX_CONTEXT_LINES:
            warnings.append(f"{md.relative_to(ROOT)} has {lines} lines (budget {MAX_CONTEXT_LINES}): split or prune")


def check_profiles() -> None:
    claude = {p.stem for p in (ROOT / ".claude" / "agents").glob("*.md")}
    codex = {p.stem for p in (ROOT / ".codex" / "agents").glob("*.toml")}
    for name in sorted(claude ^ codex):
        errors.append(f"agent profile `{name}` exists for only one of Claude/Codex")


def main() -> int:
    for md in MD_FILES:
        check_refs(md)
        check_privacy(md)
    for prof in [*(ROOT / ".claude" / "agents").glob("*.md"), *(ROOT / ".codex" / "agents").glob("*.toml")]:
        check_privacy(prof)
    check_sizes()
    check_profiles()
    for w in warnings:
        print("WARN ", w)
    for e in errors:
        print("ERROR", e)
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
