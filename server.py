"""MCP Network Assistant - 
Read-only MCP server exposing Netmiko-driven tools to MCP-compatible clients.
"""
import logging
import re
import sys
from pathlib import Path

import yaml
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from netmiko import (
    ConnectHandler,
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)

# Log to stderr only - stdout is reserved for JSON-RPC and any print() corrupts the stream.
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("mcp-net")

mcp = FastMCP("Network Assistant")

INVENTORY_PATH = Path(__file__).parent / "inventory.yaml"

MAX_OUTPUT_CHARS = 8000   # keep tool output small so it fits the model's context window
MAX_COMMAND_LEN = 200
MAX_PATTERN_LEN = 100

# Blocklist for user-supplied commands (read-only intent).
# Also blocks 'tech' (show tech-support), 'archive' and 'snmp', which can print secrets.
BLOCKED_TOKENS = re.compile(
    r"\b(run(?:ning)?|startup|secret|password|username|community|key|crypto|tech|archive|snmp)\b",
    re.IGNORECASE,
)

# Control characters (including newlines) could smuggle in a second command.
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Server-side redaction, applied to EVERY output that may contain config text.
REDACT_PATTERNS = [
    (re.compile(r"\b(secret|password)\s+\d\s+\S+", re.I), r"\1 <REDACTED>"),
    (re.compile(r"\b(secret|password)\s+(?!<REDACTED>)\S+", re.I), r"\1 <REDACTED>"),
    (re.compile(r"\b(community)\s+\S+", re.I), r"\1 <REDACTED>"),
    (re.compile(r"\b(md5)\s+\d\s+\S+", re.I), r"\1 <REDACTED>"),
    (re.compile(r"\b(key-string|authentication-key)\s+(?:\d\s+)?\S+", re.I), r"\1 <REDACTED>"),
    (re.compile(r"\b(key)\s+\d\s+\S+", re.I), r"\1 <REDACTED>"),
]


def load_inventory() -> dict:
    with open(INVENTORY_PATH) as f:
        data = yaml.safe_load(f)
    defaults = data.get("defaults", {})
    return {name: {**defaults, **dev} for name, dev in data["devices"].items()}


def redact(text: str) -> str:
    for pat, replacement in REDACT_PATTERNS:
        text = pat.sub(replacement, text)
    return text


def truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    extra = len(text) - MAX_OUTPUT_CHARS
    return text[:MAX_OUTPUT_CHARS] + f"\n... [output truncated, {extra} more characters]"


def validate_command(command: str) -> str | None:
    """Return an error message if the command is not allowed, otherwise None."""
    if len(command) > MAX_COMMAND_LEN:
        return "BLOCKED: command is too long."
    if CONTROL_CHARS.search(command):
        return "BLOCKED: newlines and control characters are not allowed."
    if not command.strip().lower().startswith("show "):
        return "BLOCKED: only 'show ...' commands are allowed via this tool."
    if BLOCKED_TOKENS.search(command):
        return (
            "BLOCKED: command contains a token on the read-only blocklist "
            "(run/startup/secret/password/username/community/key/crypto/tech/archive/snmp). "
            "Use find_in_config() or compare_running_to_startup() for config inspection."
        )
    return None


def device_params(name: str) -> dict:
    inv = load_inventory()
    if name not in inv:
        raise ToolError(f"Unknown device: {name}. Known: {list(inv.keys())}")
    d = inv[name]
    return {
        "device_type": d["device_type"],
        "host": d["host"],
        "username": d["username"],
        "password": d["password"],
        "port": d.get("port", 22),
        "conn_timeout": 10,
    }


def run_on_device(device: str, commands: list[str]) -> dict[str, str]:
    """Open one SSH session, run the commands, return {command: output}."""
    params = device_params(device)
    log.info(f"connecting to {device} ({params['host']}): {commands}")
    try:
        with ConnectHandler(**params) as conn:
            return {cmd: conn.send_command(cmd) for cmd in commands}
    except NetmikoAuthenticationException:
        raise ToolError(f"Authentication failed on {device}. Check inventory.yaml.")
    except NetmikoTimeoutException:
        raise ToolError(f"Timed out connecting to {device}. Is the node up?")


@mcp.tool()
def list_devices() -> dict:
    """List all devices in the containerlab lab inventory.
    Use when the user asks about their lab network. Do not use for general
    networking questions unrelated to the lab.
    """
    inv = load_inventory()
    return {
        name: {"host": d["host"], "description": d.get("description", "")}
        for name, d in inv.items()
    }


@mcp.tool()
def get_device_status(device: str) -> dict:
    """Return a bundled status summary for a containerlab lab device: interfaces, neighbors,
    routes, and OSPF state. Read-only. Use when investigating live state on a lab device.
    """
    out = run_on_device(
        device,
        ["show ip interface brief", "show ip ospf neighbor", "show ip route", "show cdp neighbors"],
    )
    return {
        "device": device,
        "interfaces": truncate(out["show ip interface brief"]),
        "ospf_neighbors": truncate(out["show ip ospf neighbor"]),
        "routes": truncate(out["show ip route"]),
        "cdp_neighbors": truncate(out["show cdp neighbors"]),
    }


@mcp.tool()
def run_show_command(device: str, command: str) -> str:
    """Run a read-only show command on a containerlab lab device.
    Blocks commands containing run, startup, secret, password, etc.
    Only use for lab devices listed in the inventory.
    """
    error = validate_command(command)
    if error:
        return error
    out = run_on_device(device, [command.strip()])
    return truncate(redact(out[command.strip()]))


@mcp.tool()
def find_in_config(device: str, pattern: str) -> str:
    """Search a containerlab lab device's running-config for a pattern. Server-side redaction applied.
    Use when the user wants to inspect specific config sections on a lab device.
    """
    if len(pattern) > MAX_PATTERN_LEN:
        raise ToolError("Pattern is too long.")
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        raise ToolError(f"Invalid regular expression: {e}")
    running = run_on_device(device, ["show running-config"])["show running-config"]
    matches = [line for line in redact(running).splitlines() if rx.search(line)]
    if not matches:
        return f"No matches for pattern '{pattern}' in {device}'s running-config."
    return truncate("\n".join(matches))


@mcp.tool()
def compare_running_to_startup(device: str) -> str:
    """Diff running-config vs startup-config on a containerlab lab device to detect unsaved drift.
    Server-side redaction applied.
    """
    out = run_on_device(device, ["show running-config", "show startup-config"])
    running = redact(out["show running-config"]).splitlines()
    startup = redact(out["show startup-config"]).splitlines()
    running_set, startup_set = set(running), set(startup)
    only_running = [l for l in running if l not in startup_set]
    only_startup = [l for l in startup if l not in running_set]
    result = []
    if only_running:
        result.append("=== Lines in running-config but not startup-config ===")
        result.extend(only_running)
    if only_startup:
        result.append("=== Lines in startup-config but not running-config ===")
        result.extend(only_startup)
    if not result:
        return "No drift detected - running-config matches startup-config."
    return truncate("\n".join(result))


if __name__ == "__main__":
    mcp.run()
