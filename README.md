# AI Network Assistant with Pi agent

A local AI assistant for network engineers: the [Pi coding agent](https://pi.dev/) talks to a self-hosted LLM (Ollama, `qwen3.6:35b`) and reaches your Containerlab routers only through a Python MCP server.

```
VS Code (Remote-SSH) -> Ubuntu VM: Pi agent -> MCP server (server.py) -> Containerlab routers
                                       |
                                       +-> Ollama (remote) : qwen3.6:35b
```

Tested layout: Ubuntu 22.04 or 24.04 VM with Containerlab already installed, connected from VS Code over Remote-SSH. Run all commands below in the VS Code terminal on the VM.

> Use this only in a lab. Never point the agent at production devices.

## 1. Install Pi agent

Documentation: https://pi.dev/

```bash
curl -fsSL https://pi.dev/install.sh | sh
pi --version
```

Pi itself does not need Python. Python is only needed for the MCP server (steps 4-7).

## 2. Configure Pi to use the remote Ollama model

```bash
mkdir -p ~/.pi/agent
vi ~/.pi/agent/models.json
```

```json
{
  "providers": {
    "ollama": {
      "baseUrl": "http://x.x.x.x:30280/v1",
      "api": "openai-completions",
      "apiKey": "ollama",
      "models": [
        { "id": "qwen3.6:35b" }
      ]
    }
  }
}
```

- Replace `x.x.x.x:30280` with your Ollama address. Keep the `/v1` suffix.
- The model `id` must match exactly what `ollama list` shows on the server.
- `apiKey` is a dummy value. Ollama ignores it, but Pi needs one to list the model.
- Ollama has no authentication. Keep it on a private network.
- Router configs are long. If answers get cut off, increase Ollama's context length on the server.
- Validate the file: `python3 -m json.tool ~/.pi/agent/models.json`

## 3. Add MCP support to Pi

Pi installs with a minimal set of packages and has no built-in MCP. Add the adapter:

```bash
pi install npm:pi-mcp-adapter
```

## 4. Install `uv` (Python environment and package manager)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal (or run `source ~/.local/bin/env`), then check:

```bash
uv --version
```

No `sudo` is needed: the installer puts `uv` in `~/.local/bin`.

## 5. Create the project

```bash
mkdir -p ~/lessons
cd ~/lessons
uv init network-assistant
cd network-assistant
```

`uv init` creates `pyproject.toml`, `main.py`, `.python-version` and the project files.

Quick test:

```bash
uv run main.py
```

`uv run` creates `.venv` and installs dependencies automatically, so you never need to activate the environment by hand.

## 6. Add the packages

Run this inside `~/lessons/network-assistant`:

```bash
uv add netmiko fastmcp pyyaml
```

- `netmiko`: SSH to the routers and run commands
- `fastmcp`: build the MCP server
- `pyyaml`: read the device inventory

## 7. MCP server code

Create `inventory.yaml` (adjust names, addresses and credentials to your topology):

```yaml
devices:
  R1:
    device_type: cisco_ios
    host: clab-mylab-r1
    username: admin
    password: admin
  R2:
    device_type: cisco_ios
    host: clab-mylab-r2
    username: admin
    password: admin
```

Create `server.py` (a starting point, read-only on purpose):

```python
import os

import yaml
from fastmcp import FastMCP
from netmiko import ConnectHandler

mcp = FastMCP("network-assistant")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_inventory() -> dict:
    with open(os.path.join(BASE_DIR, "inventory.yaml")) as f:
        return yaml.safe_load(f)["devices"]


@mcp.tool()
def list_routers() -> list[str]:
    """List the routers available in the lab."""
    return list(load_inventory())


@mcp.tool()
def show_command(router: str, command: str) -> str:
    """Run a read-only 'show' command on a router and return the output."""
    devices = load_inventory()
    if router not in devices:
        return f"Unknown router '{router}'. Call list_routers() first."
    if not command.strip().lower().startswith("show "):
        return "Only 'show' commands are allowed."
    with ConnectHandler(**devices[router]) as conn:
        return conn.send_command(command)


if __name__ == "__main__":
    mcp.run()
```

The tool names and docstrings are what the LLM reads to decide when to call a tool, so keep them clear.

## 8. Register the MCP server in Pi

```bash
vi ~/.pi/agent/mcp.json
```

```json
{
  "mcpServers": {
    "network-assistant": {
      "command": "uv",
      "args": ["run", "server.py"],
      "cwd": "~/lessons/network-assistant"
    }
  }
}
```

Servers start lazily: Pi connects to the MCP server the first time a tool is used.

## 9. Run it

```bash
cd ~/lessons/network-assistant
pi
```

Select the model with `/model` if needed, then try:

- `List all routers in the lab`
- `Check the OSPF neighbors on R1`
- `Why can't R3 reach R1? Show me the commands you ran`

## Troubleshooting

| Problem | Check |
|---|---|
| Model not listed in `/model` | `apiKey` present in `models.json`, JSON valid, `id` matches `ollama list` |
| Tools are never called | The model must support tool calling; test with one simple tool first |
| `uv: command not found` | Open a new terminal or `source ~/.local/bin/env` |
| MCP server does not start | Run `uv run server.py` by hand in the project folder and read the error |
| Cannot reach routers | From the VM: `ssh admin@clab-mylab-r1`; check names with `containerlab inspect` |
