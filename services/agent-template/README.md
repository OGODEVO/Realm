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

The generic launcher is:

```bash
services/agent-template/start-opencode-agent.sh
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

## Inventory

To remember which local durable agents exist and whether their OpenCode ports
are currently listening:

```bash
tools/agent-runtime-list
```

The command scans `~/.local/share/*/.env`, hides secret values, and reports each
agent's runtime home, port, tmux session, state file status, prompt path, and log
paths.
