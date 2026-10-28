# Deployment Requirements

## Runtime versions

| Requirement | Version | Why |
|---|---|---|
| Node.js | v20+ (LTS) | Execution Plane runtime — Next.js API, BullMQ workers |
| Python | 3.12 (exact minimum) | Native `datetime` `timezone.utc` handling and the `cryptography` primitives required for Ed25519 signature verification. Older versions break both. |
| Git | 2.30+ | Required by `log_sync.sh` for reliable scripted commit/push behavior; older versions can silently fail inside the automated pipeline |
| Package manager | npm or pnpm | Node.js dependency management |

## Reference core dependencies

The executable reference core under `src/` needs Python 3.12 and `pydantic>=2.6`
(`requirements.txt`); everything else is the standard library (`sqlite3 ≥ 3.38`,
`hmac`, `hashlib`). `jsonschema` and `PyYAML` are optional and used only by tests that
cross-check the packet schema and the restricted YAML parser. The integrity triggers
need JSON functions and a `json_quote` that matches RFC 8785; `open_writer` self-tests
that and refuses to open otherwise (checked on SQLite 3.45.1 only). The telemetry layer stays stdlib-only.

## Python environment isolation

`agent.py` runs inside a **virtual environment (venv)**, isolating
dependencies (`cryptography`, `numpy`) from the host system and from any
other Python process on the machine.

## Containerization

The architecture requires physical container boundaries between the
Execution, Reasoning, and Control planes — a single monolithic container
would collapse the security isolation the whole design depends on. The
target orchestration file is `/containerization/docker-compose.yml`.

| Container | Plane | Base stack | Network policy |
|---|---|---|---|
| `execution-api` | Execution | Node.js v20+, Next.js | Open external ports; owns the SQLite volume |
| `reasoning-sandbox` | Reasoning | Python 3.12 | Blocked outbound sockets (egress-proxied to whitelisted LLM APIs only); no DB volume mounted |
| `control-sidecar` | Control | Python, `validator.py` | Internal container-to-container IPC only |
| `redis-queue` | Execution | Redis / BullMQ | Reachable only from `execution-api` |

See [ADR-0007](../decisions/0007-multi-container-deployment.md) for the
monolith-vs-multi-container trade-off,
[ADR-0009](../decisions/0009-egress-proxy-for-llm-calls.md) for why the
sandbox's "no sockets" rule still allows LLM API calls, and
[ADR-0012](../decisions/0012-ai-gateway-egress-implementation.md) for how
that proxy also enforces per-request spend budgets.

### Gateway cache policy

The AI Gateway must have semantic caching **disabled** for every route that
produces or influences a Decision Packet; an optional cache for those routes must
be the state-bound exact cache
([ADR-0012](../decisions/0012-ai-gateway-egress-implementation.md)). The declaration
is [`config/ai_gateway.yaml`](../../config/ai_gateway.yaml), checked by
`python3 -m src.config` (`make check-config`). That checks the declaration; it does
not prove a deployed gateway product enforces it (the manifest is not a LiteLLM
config and no translation is provided).

### Environment

[`.env.example`](../../.env.example) lists the settings. `META_CORE_HMAC_SECRET` is
empty on purpose and `src.config.Settings` refuses to start without a 64-hex-character
key, because a default key committed to git would be a known key.

## Boot ordering

Services must come up in dependency order — the Reasoning Plane depends on
the Execution Plane's database/queue infrastructure being live, and the
Execution API should not accept traffic until the Control Plane sidecar
reports healthy. See [ADR-0008](../decisions/0008-sequential-health-gated-bootstrap.md)
and [Assembly Stages](assembly-stages.md) for the full sequence.

## Open questions

- Full `.env` / environment-variable contract linking Redis to the BullMQ
  workers.
- Concrete sandbox jail policy (AppArmor, gVisor, or equivalent) enforcing
  the blocked-socket rule at the OS level.
- Actual contents of `/containerization/docker-compose.yml` — referenced by
  path in the source design but not yet written.
- Process supervisor for the Node.js API in production (PM2, systemd unit,
  or container-native restart policy).

See [`docs/open-questions.md`](../open-questions.md) for the full list.
