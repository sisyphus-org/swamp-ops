# Operations broker

The `default` Hermes profile is the trusted **Hermes Manager / operations broker**. Other profiles call it over authenticated A2A and receive typed results without receiving `GH_TOKEN`, `GITHUB_TOKEN`, or `SWAMP_API_KEY`.

## Trust boundary

```text
secondary profile ── A2A peer token ──> default A2A gateway
                                           │
                                           └─ ops-broker toolset only
                                                  │
                                    fixed argv + policy allowlist
                                          ┌───────┴───────┐
                                          │               │
                                      GitHub `gh`      Swamp CLI
```

A Hermes profile is not an OS sandbox. This broker limits delegated capability, but all gateways still run as the same macOS user. Untrusted build/package execution needs a separate OS user or VM plus a terminal sandbox.

## Security invariants

- A2A binds to `127.0.0.1` for the first rollout.
- Every caller uses a distinct A2A peer token; authenticated session identity, not request JSON, selects policy.
- The inbound A2A platform gets only the `ops-broker` toolset.
- The Agent Card advertises only `ops-broker`.
- The tool accepts five top-level fields and rejects `caller_profile`, arbitrary URLs, shell text, unknown fields, and non-UUID request IDs.
- Every command is a fixed argv list executed with `shell=False`.
- GitHub repositories, Swamp models, workflows, and data names are exact allowlists in `plugins/ops_broker/policy.json`.
- Phase 1 accepts only `mode: plan`; all apply/write requests fail closed.
- Audit records contain caller, request ID, operation, mode, status, and approval state, but never command output, stderr, environment, or credentials.

## Initial operations

| Operation | Arguments | Automatic |
|---|---|---|
| `github.repository_access` | `repository` | yes |
| `github.list_pull_requests` | `repository` | yes |
| `github.pull_request_checks` | `repository`, `pull_request` | yes |
| `swamp.auth_whoami` | none | yes |
| `swamp.validate_model` | `model` | yes |
| `swamp.validate_workflow` | `workflow` | yes |
| `swamp.run_readonly_workflow` | `workflow` | yes, allowlisted workflows only |
| `swamp.get_result` | `model`, `name` | yes, allowlisted artifacts only |

No write operation is implemented in phase 1. A future write slice must add a separately reviewed plan artifact, replay protection, an explicit human approval record, and an apply executor.

## Installation and activation

The repository contains the plugin source, but this agent does not modify `~/.hermes/plugins/` or `.env`. The owner performs the activation after reviewing the PR.

### 1. Install the plugin into the default profile

```bash
mkdir -p /Users/hermes/.hermes/plugins
rm -rf /Users/hermes/.hermes/plugins/ops_broker
cp -R /Users/hermes/workspaces/swamp-ops/plugins/ops_broker \
  /Users/hermes/.hermes/plugins/ops_broker
hermes plugins doctor /Users/hermes/.hermes/plugins/ops_broker --ci
hermes plugins enable ops-broker
```

Do not install the plugin into secondary profiles.

### 2. Create peer credentials — owner only

The placeholders are not provider-issued credentials. Generate four new random local secrets: one each for `books`, `crypto-analyst`, `ideas`, and `swe`. The same peer secret must appear in two places because it is a client/server credential: in default's `A2A_PEER_TOKENS` mapping and only in that peer's profile `.env` as `OPS_BROKER_A2A_TOKEN`.

Run this exact block from the owner account. It generates 32 random bytes per peer, writes them as 64 hexadecimal characters, refuses to overwrite or duplicate existing keys, sets `.env` permissions to `0600`, and never prints the values:

```bash
sudo -u hermes -H /bin/sh -c 'cd /Users/hermes/workspaces && exec /usr/bin/python3 -' <<'PY'
from pathlib import Path
import os
import secrets

root = Path("/Users/hermes/.hermes")
profiles = ["books", "crypto-analyst", "ideas", "swe"]
root_env = root / ".env"
profile_envs = {name: root / "profiles" / name / ".env" for name in profiles}

def has_key(path: Path, key: str) -> bool:
    if not path.exists():
        return False
    return any(line.startswith(f"{key}=") for line in path.read_text().splitlines())

# Preflight everything before writing anything.
for key in ("A2A_PEER_TOKENS", "A2A_TRUSTED_PEERS"):
    if has_key(root_env, key):
        raise SystemExit(f"Refusing to continue: {key} already exists in {root_env}")
for name, path in profile_envs.items():
    if has_key(path, "OPS_BROKER_A2A_TOKEN"):
        raise SystemExit(
            f"Refusing to continue: OPS_BROKER_A2A_TOKEN already exists in {path}"
        )

tokens = {name: secrets.token_hex(32) for name in profiles}

def append_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_separator = False
    if path.exists() and path.stat().st_size:
        with path.open("rb") as current:
            current.seek(-1, os.SEEK_END)
            needs_separator = current.read(1) != b"\n"
    # Append-only: existing bytes in the .env file are never rewritten.
    with path.open("a", encoding="utf-8") as target:
        if needs_separator:
            target.write("\n")
        target.write("\n".join(lines) + "\n")
        target.flush()
        os.fsync(target.fileno())
    path.chmod(0o600)

append_lines(
    root_env,
    [
        "A2A_PEER_TOKENS=" + ",".join(
            f"{name}:{tokens[name]}" for name in profiles
        ),
        "A2A_TRUSTED_PEERS=" + ",".join(profiles),
    ],
)
for name, path in profile_envs.items():
    append_lines(path, [f"OPS_BROKER_A2A_TOKEN={tokens[name]}"])

# Remove references as soon as writes complete; values were never printed.
for name in profiles:
    tokens[name] = ""
print("Created four distinct A2A peer credentials without displaying them.")
PY
```

Resulting placement (placeholders below explain the mapping; do not copy them literally):

Default `/Users/hermes/.hermes/.env`:

```dotenv
A2A_PEER_TOKENS=books:<books-token>,crypto-analyst:<crypto-token>,ideas:<ideas-token>,swe:<swe-token>
A2A_TRUSTED_PEERS=books,crypto-analyst,ideas,swe
```

Each profile stores only its own corresponding credential:

```dotenv
# ~/.hermes/profiles/<profile>/.env
OPS_BROKER_A2A_TOKEN=<that-profile-token>
```

Never paste token values into chat, notes, commits, or command arguments.

### 3. Configure default through the Hermes CLI

```bash
hermes config set gateway.platforms.a2a.enabled true
hermes config set gateway.platforms.a2a.extra.port 9900
hermes config set gateway.platforms.a2a.extra.advertised_toolsets '["ops-broker"]'
hermes config set platform_toolsets.a2a '["ops-broker"]'
hermes config check
```

The default listener remains `127.0.0.1`; do not set `A2A_HOST` during phase 1.

### 4. Configure each secondary profile

Repeat for `books`, `crypto-analyst`, `ideas`, and `swe`:

```bash
PROFILE=books
HOME_PATH=/Users/hermes/.hermes/profiles/$PROFILE
HERMES_HOME="$HOME_PATH" hermes config set platform_toolsets.telegram '["hermes-telegram","a2a"]'
HERMES_HOME="$HOME_PATH" hermes config set a2a_agents.ops-broker \
  '{"url":"http://127.0.0.1:9900","auth":{"type":"bearer","token":"${OPS_BROKER_A2A_TOKEN}"},"timeout":120,"capabilities":["ops-broker"]}'
HERMES_HOME="$HOME_PATH" hermes config check
```

### 5. Restart — owner only

```bash
# default is currently supervised by user LaunchAgent ai.hermes.gateway
sudo -u hermes -H /bin/sh -c 'cd /Users/hermes/.hermes && exec /Users/hermes/.local/bin/hermes gateway restart'

# secondary profiles are system LaunchDaemons
sudo launchctl kickstart -k system/local.hermes.gateway-books
sudo launchctl kickstart -k system/local.hermes.gateway-crypto-analyst
sudo launchctl kickstart -k system/local.hermes.gateway-ideas
sudo launchctl kickstart -k system/local.hermes.gateway-swe
```

## Verification

1. Agent Card:

```bash
curl -s http://127.0.0.1:9900/.well-known/agent-card.json
```

Expected: one advertised skill/toolset, `ops-broker`; no terminal, file, memory, Linear, or generic default-profile toolsets.

2. For every secondary profile, run a real A2A request for:

- `github.repository_access` on `sisyphus-org/swamp-ops`;
- `swamp.validate_workflow` for `ops-broker-readonly-smoke`;
- `swamp.run_readonly_workflow` for `ops-broker-readonly-smoke`.

3. Negative requests must be rejected:

- `caller_profile` in the body;
- `operation: run_shell`;
- arbitrary repository/workflow;
- `mode: apply`;
- credential/environment retrieval.

4. Audit file:

```text
/Users/hermes/.hermes/plugin-data/ops-broker/audit.jsonl
```

It must identify each authenticated peer and contain no credential-shaped output.

## Recovery and rotation

- Plugin failure: disable `ops-broker`, remove A2A from secondary Telegram toolsets, restart affected gateways.
- Revoke one peer: remove that identity from `A2A_PEER_TOKENS` and `A2A_TRUSTED_PEERS`, restart default, then rotate only that profile's `OPS_BROKER_A2A_TOKEN` before re-enabling it.
- Suspected broker compromise: disable inbound A2A, rotate all peer tokens, then rotate every upstream credential the broker could use (`GH_TOKEN`/`GITHUB_TOKEN`, `SWAMP_API_KEY`).
- Policy change: update `policy.json`, rerun unit tests and Plugin Doctor, reinstall the reviewed plugin version, restart default, and repeat negative tests.

## Remote machines

Remote access is a separate follow-up. Do not bind `0.0.0.0` as part of this rollout. A remote design must use an authenticated private network or TLS reverse proxy, explicit bind address, per-machine tokens, trusted-peer allowlist, rate limits, and a fresh threat-model review.
