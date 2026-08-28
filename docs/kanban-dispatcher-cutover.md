# Kanban dispatcher cutover and rollback

SIS-58 moves the shared single-host Hermes Kanban dispatcher from the accidental lock winner to the dedicated headless `broker` profile.

## Production invariant

Exactly one profile may have `kanban.dispatch_in_gateway=true`:

| Profile | Dispatch |
|---|---:|
| `broker` | `true` |
| `default` | `false` |
| `ideas` | `false` |
| `swe` | `false` |
| `books` | `false` |
| `crypto-analyst` | `false` |
| `project-manager` | `false` |

The lock is `/Users/hermes/.hermes/kanban/.dispatcher.lock`. Notification delivery remains profile-owned; disabling dispatch does not disable a profile's notifier.

## Read-only verification

```bash
cd /Users/hermes/workspaces/swamp-ops
/Users/hermes/.hermes/hermes-agent/venv/bin/python scripts/kanban_dispatcher_audit.py
swamp workflow run kanban-dispatcher-audit
```

A healthy result has:

- `result=pass`;
- `config.enabled=["broker"]` and no missing explicit values;
- exactly one lock PID;
- `lock.owner_profile=broker`.

The committed audit is fixed to the production profile roster and paths. It performs no writes.

## Cutover order

The board must be empty or intentionally drained before cutover.

1. Back up all seven configs.
2. Set all profiles to explicit `false`, then set `broker=true` using `hermes config set`.
3. Restart the previous lock owner first so it releases the lock.
4. Restart the other non-broker Gateways.
5. Restart `broker`; verify it owns the lock.
6. Restart `default` so its runtime also reflects explicit `false`.
7. Run the read-only audit and one transport-only task on a temporary board.

SIS-58 observed previous owner `crypto-analyst`. The transport proof `t_7876a489` had exactly one claimed event, one spawned event, one completed event and one run; metadata recorded `external_changes=false`. The temporary board was archived after verification.

## Recovery proof

Restart only the broker LaunchDaemon and verify:

```bash
sudo launchctl kickstart -k system/local.hermes.gateway-broker
sudo launchctl print system/local.hermes.gateway-broker | grep -E 'state = |pid = '
```

Then rerun the audit. A new broker PID must be the sole lock holder. SIS-58 verified this with `85536 → 86335` (gateway child PID) and a fresh `holding singleton dispatcher lock` log entry.

## Rollback to the previous owner

Rollback is a deliberate explicit topology, not restoration of all old implicit defaults.

**Agent config phase:**

```bash
HERMES_HOME=/Users/hermes/.hermes/profiles/broker hermes config set kanban.dispatch_in_gateway false
HERMES_HOME=/Users/hermes/.hermes/profiles/crypto-analyst hermes config set kanban.dispatch_in_gateway true
```

Keep every other profile explicitly `false`.

**Owner restart phase:**

```bash
sudo launchctl kickstart -k system/local.hermes.gateway-broker
sudo launchctl kickstart -k system/local.hermes.gateway-crypto-analyst
sudo launchctl print system/local.hermes.gateway-crypto-analyst | grep -E 'state = |pid = '
```

**Read-back:**

```bash
lsof /Users/hermes/.hermes/kanban/.dispatcher.lock
```

The single holder command must contain `--profile crypto-analyst`. After rollback diagnosis, cut back to broker using the normal cutover order. Pre-cutover config copies are retained as `config.yaml.sis58-pre-cutover` for forensic comparison only; restoring all of them would re-enable implicit dispatch on several profiles and is not the approved rollback.
