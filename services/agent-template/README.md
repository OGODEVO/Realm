# Durable OpenCode Agent Template

This template turns one-off Realm/OpenCode wrappers into reusable runtime homes.
Each agent gets:

```text
~/.local/share/<agent-id>/.env
~/.local/share/<agent-id>/system-prompt.md
~/.local/share/<agent-id>/opencode.json
~/.local/share/<agent-id>/.realm/
~/.local/share/<agent-id>/.blobs/
```

Launchers:

```bash
services/agent-template/start-opencode-agent.sh   # OpenCode (local serve + wrapper)
services/agent-template/start-cli-agent.sh        # Codex or Grok CLI (no server)
```

## Create An Agent

```bash
mkdir -p ~/.local/share/m4-cuda
cp services/agent-template/env.example ~/.local/share/m4-cuda/.env
chmod 600 ~/.local/share/m4-cuda/.env
```

Edit `.env` and choose a unique identity and port:

```bash
REALM_AGENT_ID=m4-cuda
REALM_AGENT_NAME=m4-cuda
REALM_USERNAME=m4-cuda
OPENCODE_PORT=4198
REALM_SYSTEM_PROMPT_FILE=/Users/a.developer/.local/share/m4-cuda/system-prompt.md
```

Put the long-lived role prompt in:

```bash
~/.local/share/m4-cuda/system-prompt.md
```

Use `system-prompt.example.md` as the base network-aware prompt for new agents.

Start it:

```bash
REALM_AGENT_HOME="$HOME/.local/share/m4-cuda" \
  services/agent-template/start-opencode-agent.sh
```

## Wrapper Script

For a stable per-agent command, create:

```bash
#!/usr/bin/env bash
set -euo pipefail
export REALM_AGENT_HOME="/Users/a.developer/.local/share/m4-cuda"
exec "/Users/a.developer/Documents/Realm/services/agent-template/start-opencode-agent.sh"
```

For tmux durability:

```bash
tmux new-session -d -s m4-cuda /Users/a.developer/.local/share/m4-cuda/start-m4-cuda.sh
```

Then confirm it registered:

```bash
agentnet list --nats-url nats://agentnet_secret_token@localhost:4222
```

## Design Rules

- Put durable identity and ports in `.env`.
- Put secrets in `.env`, not in scripts.
- Put long prompts in `system-prompt.md`, not in scripts.
- Keep `start-opencode-agent.sh` generic for all OpenCode-backed Realm agents.
- Completion is explicit: the wrapper injects a `complete`/`blocked` protocol,
  keeps running while neither status is declared, and removes control markers
  before replying to the user.
- `REALM_MAX_AGENT_TURNS` is a configurable safety ceiling and defaults to 56.

## Adding a new instance (Codex / Grok / OpenCode)

Pick a brain runtime, then create a durable home under `~/.local/share/<id>/`.

| Runtime | Wrapper | Launcher | Needs local server? |
|---------|---------|----------|---------------------|
| OpenCode | `examples/opencode_realm_agent.py` | `start-opencode-agent.sh` | Yes (`opencode serve`) |
| Codex | `examples/cli_realm_agent.py` | `start-cli-agent.sh` | No (`codex exec`) |
| Grok | `examples/cli_realm_agent.py` | `start-cli-agent.sh` | No (`grok` headless) |

### Codex worker (`@codex-worker`)

```bash
mkdir -p ~/.local/share/codex-worker
cp services/agent-template/env.cli.example ~/.local/share/codex-worker/.env
chmod 600 ~/.local/share/codex-worker/.env
```

Edit `.env`:

```bash
REALM_AGENT_ID=codex-worker
REALM_AGENT_NAME=codex-worker
REALM_USERNAME=codex-worker
REALM_NATS_URL=nats://agentnet_secret_token@localhost:4222
REALM_RUNTIME=codex
REALM_WORKDIR=/Users/a.developer/Documents/Realm
CODEX_BIN=/opt/homebrew/bin/codex
CODEX_SANDBOX=workspace-write
CODEX_FULL_AUTO=false
```

Start:

```bash
REALM_AGENT_HOME="$HOME/.local/share/codex-worker" \
  services/agent-template/start-cli-agent.sh
# logs: /tmp/codex-worker-realm.log
```

### Grok worker (`@grok-worker`)

```bash
mkdir -p ~/.local/share/grok-worker
cp services/agent-template/env.cli.example ~/.local/share/grok-worker/.env
chmod 600 ~/.local/share/grok-worker/.env
```

Edit `.env`:

```bash
REALM_AGENT_ID=grok-worker
REALM_AGENT_NAME=grok-worker
REALM_USERNAME=grok-worker
REALM_NATS_URL=nats://agentnet_secret_token@localhost:4222
REALM_RUNTIME=grok
REALM_WORKDIR=/Users/a.developer/Documents/Realm
GROK_BIN=$HOME/.local/bin/grok
GROK_ALWAYS_APPROVE=true
GROK_MODE=agent
GROK_OUTPUT_FORMAT=plain
```

Start:

```bash
REALM_AGENT_HOME="$HOME/.local/share/grok-worker" \
  services/agent-template/start-cli-agent.sh
# logs: /tmp/grok-worker-realm.log
```

### OpenCode worker

Use `env.example` + `start-opencode-agent.sh` (starts `opencode serve` then
`opencode_realm_agent.py`). Do **not** set `REALM_RUNTIME=opencode` on the CLI
launcher — it will refuse and point you here.

### Shared contract

All three wrappers:

1. Register with username + capabilities (including runtime name).
2. On `task.assign`: emit progress `ack` then `working`.
3. Stream brain activity as `task.progress` (`tool` / `text` / `status`).
4. Finish once with `task.result` / blocked / failed (`require_delivery_ack=False`).
5. Answer `STATE` queries from the optional agent-state file.

Confirm on the mesh:

```bash
./network.sh list
./network.sh status @codex-worker
```

## Inventory

To remember which local durable agents exist and whether their OpenCode ports
are currently listening:

```bash
tools/agent-runtime-list
```

The command scans `~/.local/share/*/.env`, hides secret values, and reports each
agent's runtime home, port, tmux session, state file status, prompt path, and log
paths.
