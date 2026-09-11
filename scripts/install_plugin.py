#!/usr/bin/env python3
"""Configure this local checkout as a personal Codex plugin; code stays in the repo."""

import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
plugin = root / "plugins/talk2myagent"
python = root / ".venv/bin/python"
if not python.exists():
    raise SystemExit("Run scripts/setup.sh first.")
manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
if manifest["name"] != plugin.name:
    raise SystemExit("Plugin name mismatch.")
(plugin / ".mcp.json").write_text(
    json.dumps(
        {
            "mcpServers": {
                "phone": {
                    "command": str(python),
                    "args": ["-m", "talk2myagent", "mcp"],
                    "env": {"T2MA_ROOT": str(root)},
                }
            }
        },
        indent=2,
    )
    + "\n"
)
marketplace = Path.home() / ".agents/plugins/marketplace.json"
if not marketplace.exists():
    raise SystemExit("Personal marketplace missing. See docs/SETUP.md for scaffold instructions.")
data = json.loads(marketplace.read_text())
entry = next((p for p in data["plugins"] if p["name"] == "talk2myagent"), None)
if not entry or entry["source"]["path"] != "./plugins/talk2myagent":
    raise SystemExit("Personal marketplace entry missing or points elsewhere.")
# The implicit personal marketplace resolves ./plugins against the user's home.
link = Path.home() / "plugins/talk2myagent"
link.parent.mkdir(parents=True, exist_ok=True)
if link.exists() or link.is_symlink():
    if link.resolve() != plugin:
        raise SystemExit(f"Refusing to replace an existing plugin at {link}.")
else:
    link.symlink_to(plugin, target_is_directory=True)
skill = Path.home() / ".codex/skills/.system/plugin-creator"
name = subprocess.check_output(
    [str(python), str(skill / "scripts/read_marketplace_name.py")], text=True
).strip()
subprocess.run(
    [str(python), str(skill / "scripts/update_plugin_cachebuster.py"), str(plugin)], check=True
)
subprocess.run(["codex", "plugin", "add", f"talk2myagent@{name}"], check=True)
print("Installed. Start a new Codex task to load the phone-call skill and phone tools.")
