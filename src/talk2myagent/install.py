"""Wire this checkout into the agent hosts installed on this Mac.

One MCP server (``python -m talk2myagent mcp``) serves every host. Each host only
needs a config entry pointing at this checkout's virtual environment plus a copy
of the phone-call skill in a directory it scans:

- OpenCode: project ``opencode.json`` (``mcp`` block) and ``.claude/skills`` (scanned).
- Claude Code: project ``.mcp.json`` and ``.claude/skills``.
- Codex: the personal plugin marketplace (``codex plugin add``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .config import ROOT

SKILL_SOURCE = ROOT / "plugins/talk2myagent/skills/phone-call"


def server_entry(python: Path) -> dict:
    return {
        "command": str(python),
        "args": ["-m", "talk2myagent", "mcp"],
        "env": {"T2MA_ROOT": str(ROOT)},
    }


def install_opencode(python: Path) -> Path:
    path = ROOT / "opencode.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data.setdefault("$schema", "https://opencode.ai/config.json")
    data.setdefault("mcp", {})["phone"] = {
        "type": "local",
        "command": [str(python), "-m", "talk2myagent", "mcp"],
        "environment": {"T2MA_ROOT": str(ROOT)},
        "enabled": True,
        "timeout": 30000,
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def install_claude_code(python: Path) -> Path:
    path = ROOT / ".mcp.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data.setdefault("mcpServers", {})["phone"] = server_entry(python)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def install_skill_copy() -> Path:
    """Both OpenCode and Claude Code scan .claude/skills; keep it a copy, not a symlink."""
    target = ROOT / ".claude/skills/phone-call"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL_SOURCE / "SKILL.md", target / "SKILL.md")
    return target


def install_codex(python: Path) -> str:
    plugin = ROOT / "plugins/talk2myagent"
    (plugin / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"phone": server_entry(python)}}, indent=2) + "\n"
    )
    codex = shutil.which("codex") or next(
        (
            str(candidate)
            for candidate in [Path("/Applications/ChatGPT.app/Contents/Resources/codex")]
            if candidate.exists()
        ),
        None,
    )
    if codex is None:
        return "codex CLI not found; plugin manifest updated only."
    marketplace = Path.home() / ".agents/plugins/marketplace.json"
    if not marketplace.exists():
        return "Personal Codex marketplace missing; see docs/SETUP.md."
    data = json.loads(marketplace.read_text())
    entry = next((p for p in data["plugins"] if p["name"] == "talk2myagent"), None)
    if not entry or entry["source"]["path"] != "./plugins/talk2myagent":
        return "Personal marketplace entry missing or points elsewhere."
    link = Path.home() / "plugins/talk2myagent"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        if link.resolve() != plugin:
            return f"Refusing to replace an existing plugin at {link}."
    else:
        link.symlink_to(plugin, target_is_directory=True)
    skill = Path.home() / ".codex/skills/.system/plugin-creator"
    if skill.exists():
        name = subprocess.check_output(
            [str(python), str(skill / "scripts/read_marketplace_name.py")], text=True
        ).strip()
        subprocess.run(
            [str(python), str(skill / "scripts/update_plugin_cachebuster.py"), str(plugin)],
            check=True,
        )
    else:
        name = data.get("name", "personal")
    subprocess.run([codex, "plugin", "add", f"talk2myagent@{name}"], check=True)
    return f"Installed talk2myagent@{name}; start a new Codex task."


def install_all() -> dict:
    python = ROOT / ".venv/bin/python"
    if not python.exists():
        raise RuntimeError("Run scripts/setup.sh first to create .venv.")
    return {
        "opencode": str(install_opencode(python)),
        "claude_code": str(install_claude_code(python)),
        "skill": str(install_skill_copy()),
        "codex": install_codex(python),
        "next_step": (
            "Open this folder in OpenCode or Claude Code (new session) or start a new Codex "
            "task; the phone tools appear as an MCP server named 'phone'."
        ),
    }
