#!/usr/bin/env python3
"""Consistency check of the shared agent memory (stdlib only).

Run from anywhere:  python3 .agents/scripts/check_memory.py
Exit code 1 when a hard problem is found. Checks:
  - referenced repo paths and `path::symbol` references still exist
  - size budgets (warnings)
  - `live.*` diagnostics paths against the pinned schema fixture
  - UPPER_SNAKE constants exist; `NAME` (value) pairs match the code
  - privacy patterns (emails, URLs, IPs, hex secrets, entity ids, local-only folders,
    plus an optional local blocklist: env SMARTPI_MEMORY_BLOCKLIST or .agents/.local-blocklist,
    one case-insensitive word per line, never versioned)
  - Claude and Codex agent profiles parse, mirror each other and agree on read-only
"""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "custom_components" / "vtherm_smartpi"
SEARCH_BASES = [ROOT, PKG, PKG / "smartpi", ROOT / ".agents", ROOT / "tests"]
MD_FILES = [ROOT / "AGENTS.md", ROOT / "CLAUDE.md", *sorted((ROOT / ".agents").rglob("*.md"))]
MAX_CONTEXT_LINES = 400
MAX_AGENTS_BYTES = 24_000
MAX_CONTEXT_BYTES = 32_000
FIXTURE = ROOT / "tests" / "fixtures" / "published_diagnostics_paths.json"
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
    "entity-id": re.compile(
        r"\b(?:climate|sensor|binary_sensor|switch|number|select|light|input_[a-z]+|automation)\.[a-z0-9_]{3,}\b"
    ),
}
# Entity-id-looking strings that are generic placeholders, not real entities.
ENTITY_ALLOW = re.compile(r"\b(?:sensor|climate)\.(?:thermostat|name|entity|example|my|room)\b")
CONST = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
CONST_VALUE = re.compile(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`\s*\((-?\d+(?:\.\d+)?)(?:\s*(?:s|min|h|C|%|deg))?\)")
_code_cache: dict[str, str] = {}
_pinned: set[str] | None = None


def code_text() -> str:
    if "all" not in _code_cache:
        _code_cache["all"] = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore") for p in [*PKG.rglob("*.py"), *(ROOT / "tests").glob("*.py"), *(ROOT / ".agents" / "scripts").glob("*.py")]
        )
    return _code_cache["all"]


def blocklist() -> list[str]:
    path = os.environ.get("SMARTPI_MEMORY_BLOCKLIST") or str(ROOT / ".agents" / ".local-blocklist")
    try:
        return [w.strip().lower() for w in Path(path).read_text(encoding="utf-8").splitlines() if w.strip()]
    except OSError:
        return []
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


def check_constants(md: Path) -> None:
    skipping = False
    for n, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("#"):
            skipping = bool(SKIP_HEADING.search(line))
        if skipping:
            continue
        for tok in re.findall(r"`([^`]+)`", line):
            if CONST.match(tok) and not re.search(rf"\b{re.escape(tok)}\b", code_text()):
                errors.append(f"{md.relative_to(ROOT)}:{n}: constant `{tok}` not found in code")
        for name, val in CONST_VALUE.findall(line):
            m = re.search(rf"^{re.escape(name)}\s*(?::[^=\n]+)?=\s*(-?\d+(?:\.\d+)?(?:e-?\d+)?)\s*(?:#.*)?$", code_text(), re.M)
            if m and abs(float(m.group(1)) - float(val)) > 1e-9:
                errors.append(f"{md.relative_to(ROOT)}:{n}: `{name}` is {m.group(1)} in code, doc says {val}")


def check_live_paths(md: Path) -> None:
    global _pinned
    if _pinned is None:
        _pinned = set(json.loads(FIXTURE.read_text(encoding="utf-8"))) if FIXTURE.exists() else set()
    roots = {p.split(".")[0] for p in _pinned}
    for n, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        for tok in re.findall(r"`live\.([a-z0-9_.]+)`", line):
            tok = tok.rstrip(".")
            if tok.split(".")[0] not in roots:
                continue  # block not pinned by the schema test (tar_learning, room_model)
            if tok not in _pinned:
                errors.append(f"{md.relative_to(ROOT)}:{n}: `live.{tok}` is not in the pinned schema fixture")


def check_privacy(md: Path) -> None:
    for n, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        for label, rx in PRIVACY.items():
            m = rx.search(line)
            if m and not (label == "entity-id" and ENTITY_ALLOW.search(m.group(0))):
                errors.append(f"{md.relative_to(ROOT)}:{n}: privacy pattern ({label})")
        low = line.lower()
        for word in blocklist():
            if word in low:
                errors.append(f"{md.relative_to(ROOT)}:{n}: blocklisted word")


def check_sizes() -> None:
    agents = ROOT / "AGENTS.md"
    if agents.stat().st_size > MAX_AGENTS_BYTES:
        warnings.append(f"AGENTS.md is {agents.stat().st_size} bytes (budget {MAX_AGENTS_BYTES}; Codex stops at 32 KiB)")
    for md in sorted((ROOT / ".agents" / "context").glob("*.md")):
        lines = len(md.read_text(encoding="utf-8").splitlines())
        size = md.stat().st_size
        if size > MAX_CONTEXT_BYTES:
            warnings.append(f"{md.relative_to(ROOT)} is {size} bytes (budget {MAX_CONTEXT_BYTES}): split or prune")
        if lines > MAX_CONTEXT_LINES:
            warnings.append(f"{md.relative_to(ROOT)} has {lines} lines (budget {MAX_CONTEXT_LINES}): split or prune")


def check_profiles() -> None:
    claude = {p.stem for p in (ROOT / ".claude" / "agents").glob("*.md")}
    codex = {p.stem for p in (ROOT / ".codex" / "agents").glob("*.toml")}
    for name in sorted(claude ^ codex):
        errors.append(f"agent profile `{name}` exists for only one of Claude/Codex")
    for name in sorted(claude & codex):
        try:
            text = (ROOT / ".claude" / "agents" / f"{name}.md").read_text(encoding="utf-8")
            front = text.split("---")[1]
            fm = dict(line.split(": ", 1) for line in front.strip().splitlines() if ": " in line)
            cx = tomllib.loads((ROOT / ".codex" / "agents" / f"{name}.toml").read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - report any parse failure
            errors.append(f"agent profile `{name}` does not parse: {exc}")
            continue
        for key in ("name", "model", "effort"):
            if key not in fm:
                errors.append(f"Claude profile `{name}` lacks `{key}`")
        for key in ("name", "description", "model", "model_reasoning_effort", "developer_instructions"):
            if key not in cx:
                errors.append(f"Codex profile `{name}` lacks `{key}`")
        if cx.get("name") != name or fm.get("name") != name:
            errors.append(f"agent profile `{name}`: name differs from file name")
        claude_ro = "Edit" not in fm.get("tools", "")
        codex_ro = "read-only" in cx.get("developer_instructions", "")
        if claude_ro != codex_ro:
            errors.append(f"agent profile `{name}`: read-only status differs between Claude and Codex")
        role_c = next((l for l in text.splitlines() if l.startswith("Role:")), "")
        role_x = next((l for l in cx.get("developer_instructions", "").splitlines() if l.startswith("Role:")), "")
        if role_c != role_x:
            errors.append(f"agent profile `{name}`: instructions differ between Claude and Codex")


def main() -> int:
    for md in MD_FILES:
        check_refs(md)
        check_constants(md)
        check_live_paths(md)
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
