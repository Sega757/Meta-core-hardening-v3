"""Fail-fast configuration: environment settings and the AI Gateway manifest.

* ``Settings`` - validated at start-up from the environment (``.env.example``). A
  missing, malformed or weak ``META_CORE_HMAC_SECRET`` refuses to start.
* ``parse_simple_yaml`` - a deliberately small YAML subset parser (maps, block and
  inline lists of scalars, comments). Anything else raises, so the manifest cannot
  contain a construct this validator does not understand. The standard library has no
  YAML parser and the reference core takes no extra dependency.
* ``validate_gateway_manifest`` - enforces the ADR-0012 rules on
  ``config/ai_gateway.yaml``: deny-by-default egress, no wildcard hosts, no semantic
  cache on any route that produces a Decision Packet or has tools in scope, only the
  state-bound exact cache otherwise, finite timeouts, bounded retries.

Run ``python3 -m src.config [manifest.yaml]`` to check a manifest (exit 0 / 1).
This checks the declaration only; it cannot prove that a deployed gateway product
enforces it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Final, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

DEFAULT_MANIFEST: Final[Path] = Path(__file__).resolve().parent.parent / "config" / "ai_gateway.yaml"
STATE_BOUND_KEY_FIELDS: Final[tuple[str, ...]] = (
    "model_id",
    "system_prompt_hash",
    "state_root",
    "canonical_prompt_payload",
)
DECISION_ROUTE: Final[str] = "reasoning-decision"


class ConfigError(ValueError):
    """Configuration is missing, malformed or unsafe; the process must not start."""


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #

_ENV_LINE: Final = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_HEX64: Final = re.compile(r"^[0-9a-fA-F]{64}$")


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; blank lines and full-line ``#`` comments are skipped."""
    out: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if match is None:
            raise ConfigError(f"line {number}: expected KEY=VALUE")
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in out:
            raise ConfigError(f"line {number}: duplicate key {key}")
        out[key] = value
    return out


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hmac_secret: bytes
    sqlite_db_path: Path
    max_token_budget: int = Field(ge=1, le=10_000_000)
    capability_ttl_ms: int = Field(ge=1, le=60_000)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        secret_hex = env.get("META_CORE_HMAC_SECRET", "")
        if not _HEX64.match(secret_hex):
            raise ConfigError(
                "META_CORE_HMAC_SECRET must be exactly 64 hex characters (32 bytes); "
                'generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"'
            )
        if len(set(secret_hex.lower())) < 8:
            raise ConfigError("META_CORE_HMAC_SECRET looks like a placeholder, not random bytes")
        try:
            return cls(
                hmac_secret=bytes.fromhex(secret_hex),
                sqlite_db_path=Path(env.get("SQLITE_DB_PATH", "./data/meta_core.db")),
                max_token_budget=int(env.get("MAX_TOKEN_BUDGET", "20000")),
                capability_ttl_ms=int(env.get("CAPABILITY_TTL_MS", "5000")),
            )
        except (ValueError, ValidationError) as exc:
            raise ConfigError(f"invalid settings: {exc}") from exc


# --------------------------------------------------------------------------- #
# Restricted YAML subset
# --------------------------------------------------------------------------- #

_KEY: Final = re.compile(r"^[A-Za-z0-9_.-]+$")
_INT: Final = re.compile(r"^[+-]?\d+$")
_FLOAT: Final = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_UNSUPPORTED_START: Final = ("&", "*", "!", "|", ">", "{", "@", "`", "%")


def _strip_comment(line: str) -> str:
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _scalar(token: str, where: str) -> Any:
    token = token.strip()
    if token == "":
        raise ConfigError(f"{where}: empty value")
    if token[0] in _UNSUPPORTED_START:
        raise ConfigError(f"{where}: unsupported YAML construct {token[0]!r}")
    if token[0] in "\"'":
        if len(token) < 2 or token[-1] != token[0]:
            raise ConfigError(f"{where}: unterminated string")
        return token[1:-1]
    lowered = token.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "~"):
        return None
    if _INT.match(token):
        return int(token)
    if _FLOAT.match(token):
        return float(token)
    return token


def _inline_list(token: str, where: str) -> list[Any]:
    inner = token.strip()[1:-1].strip()
    if inner == "":
        return []
    items, current, quote = [], "", None
    for ch in inner:
        if quote:
            current += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            current += ch
        elif ch == ",":
            items.append(current)
            current = ""
        elif ch in "[]{}":
            raise ConfigError(f"{where}: nested flow collections are not supported")
        else:
            current += ch
    if quote:
        raise ConfigError(f"{where}: unterminated string in list")
    items.append(current)
    return [_scalar(item, where) for item in items]


def _value(token: str, where: str) -> Any:
    token = token.strip()
    if token.startswith("["):
        if not token.endswith("]"):
            raise ConfigError(f"{where}: unterminated inline list")
        return _inline_list(token, where)
    return _scalar(token, where)


def parse_simple_yaml(text: str) -> dict[str, Any]:
    lines: list[tuple[int, int, str]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        body = _strip_comment(raw).rstrip()
        if not body.strip():
            continue
        lead = body[: len(body) - len(body.lstrip())]
        if "\t" in lead:
            raise ConfigError(f"line {number}: tabs are not allowed for indentation")
        if len(lead) % 2:
            raise ConfigError(f"line {number}: indentation must be a multiple of 2 spaces")
        lines.append((number, len(lead), body.strip()))
    if not lines:
        return {}
    value, end = _block(lines, 0, 0)
    if end != len(lines):
        raise ConfigError(f"line {lines[end][0]}: unexpected indentation")
    if not isinstance(value, dict):
        raise ConfigError("top level must be a mapping")
    return value


def _block(lines: list[tuple[int, int, str]], i: int, indent: int) -> tuple[Any, int]:
    number, line_indent, content = lines[i]
    if line_indent != indent:
        raise ConfigError(f"line {number}: unexpected indentation")
    if content.startswith("- ") or content == "-":
        items: list[Any] = []
        while i < len(lines) and lines[i][1] == indent and lines[i][2].startswith("-"):
            number, _, content = lines[i]
            item = content[1:].strip()
            if item == "" or ":" in item.split("#")[0] and not item.startswith(("\"", "'", "[")):
                raise ConfigError(f"line {number}: only lists of scalars are supported")
            items.append(_value(item, f"line {number}"))
            i += 1
        return items, i
    mapping: dict[str, Any] = {}
    while i < len(lines) and lines[i][1] == indent:
        number, _, content = lines[i]
        if content.startswith("-"):
            raise ConfigError(f"line {number}: list item where a key was expected")
        key, sep, rest = content.partition(":")
        key = key.strip()
        if not sep or not _KEY.match(key):
            raise ConfigError(f"line {number}: expected 'key: value'")
        if key in mapping:
            raise ConfigError(f"line {number}: duplicate key {key!r}")
        rest = rest.strip()
        i += 1
        if rest:
            mapping[key] = _value(rest, f"line {number}")
        elif i < len(lines) and lines[i][1] > indent:
            if lines[i][1] != indent + 2:
                raise ConfigError(f"line {lines[i][0]}: unexpected indentation")
            mapping[key], i = _block(lines, i, indent + 2)
        elif i < len(lines) and lines[i][1] == indent and lines[i][2].startswith("-"):
            mapping[key], i = _block(lines, i, indent)  # list at the same indent as its key
        else:
            raise ConfigError(f"line {number}: key {key!r} has no value")
    if i < len(lines) and lines[i][1] > indent:
        raise ConfigError(f"line {lines[i][0]}: unexpected indentation")
    return mapping, i


# --------------------------------------------------------------------------- #
# Gateway manifest
# --------------------------------------------------------------------------- #

_STRICT = ConfigDict(extra="forbid", frozen=True)
_HOST: Final = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class Egress(BaseModel):
    model_config = _STRICT
    default_action: str
    allowed_hosts: list[str] = Field(min_length=1)
    tls_required: bool

    @field_validator("default_action")
    @classmethod
    def _deny(cls, value: str) -> str:
        if value != "deny":
            raise ValueError("egress.default_action must be 'deny' (fail closed)")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts(cls, hosts: list[str]) -> list[str]:
        for host in hosts:
            if "*" in host or not _HOST.match(host):
                raise ValueError(f"allowed host must be a plain hostname, got {host!r}")
        return hosts

    @field_validator("tls_required")
    @classmethod
    def _tls(cls, value: bool) -> bool:
        if not value:
            raise ValueError("egress.tls_required must be true")
        return value


class SemanticCache(BaseModel):
    model_config = _STRICT
    enabled: bool
    similarity_threshold: float | None = Field(default=None, gt=0, le=1)
    ttl_s: int | None = Field(default=None, gt=0)


class ExactCache(BaseModel):
    model_config = _STRICT
    enabled: bool
    key: str
    key_fields: list[str]

    @model_validator(mode="after")
    def _state_bound(self) -> "ExactCache":
        if self.key != "state_bound" or tuple(self.key_fields) != STATE_BOUND_KEY_FIELDS:
            raise ValueError(
                "exact_cache must be state_bound with key_fields " + ", ".join(STATE_BOUND_KEY_FIELDS)
            )
        return self


class Limits(BaseModel):
    model_config = _STRICT
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_request_budget_tokens: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "Limits":
        if self.max_output_tokens > self.max_request_budget_tokens:
            raise ValueError("max_output_tokens cannot exceed max_request_budget_tokens")
        if self.max_request_budget_tokens > self.max_input_tokens + self.max_output_tokens:
            raise ValueError("max_request_budget_tokens cannot exceed input + output ceilings")
        return self


class Timeouts(BaseModel):
    model_config = _STRICT
    connect: int = Field(gt=0, le=60)
    read: int = Field(gt=0, le=300)
    total: int = Field(gt=0, le=300)

    @model_validator(mode="after")
    def _ordered(self) -> "Timeouts":
        if not self.connect < self.read <= self.total:
            raise ValueError("timeouts must satisfy connect < read <= total")
        return self


class Route(BaseModel):
    model_config = _STRICT
    description: str
    produces_decision_packet: bool
    tools_in_scope: bool
    semantic_cache: SemanticCache
    exact_cache: ExactCache
    limits: Limits
    timeouts_s: Timeouts
    max_retries: int = Field(ge=0, le=2)

    @model_validator(mode="after")
    def _adr_0012(self) -> "Route":
        if self.semantic_cache.enabled and (self.produces_decision_packet or self.tools_in_scope):
            raise ValueError(
                "semantic_cache must be disabled on routes that produce a Decision Packet "
                "or have tools in scope (ADR-0012)"
            )
        return self


class GatewayManifest(BaseModel):
    model_config = _STRICT
    version: int
    egress: Egress
    routes: dict[str, Route]

    @model_validator(mode="after")
    def _decision_route(self) -> "GatewayManifest":
        if self.version != 1:
            raise ValueError("unsupported manifest version")
        route = self.routes.get(DECISION_ROUTE)
        if route is None:
            raise ValueError(f"route {DECISION_ROUTE!r} is required")
        if not (route.produces_decision_packet and route.tools_in_scope):
            raise ValueError(f"route {DECISION_ROUTE!r} must declare produces_decision_packet and tools_in_scope")
        return self


def validate_gateway_manifest(data: Mapping[str, Any]) -> GatewayManifest:
    try:
        return GatewayManifest.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid gateway manifest: {exc}") from exc


def load_gateway_manifest(path: str | Path = DEFAULT_MANIFEST) -> GatewayManifest:
    return validate_gateway_manifest(parse_simple_yaml(Path(path).read_text(encoding="utf-8")))


def check_budget_against_settings(manifest: GatewayManifest, settings: Settings) -> None:
    """Every route's per-request ceiling must fit the run's token bucket."""
    for name, route in manifest.routes.items():
        if route.limits.max_request_budget_tokens > settings.max_token_budget:
            raise ConfigError(
                f"route {name!r}: max_request_budget_tokens "
                f"{route.limits.max_request_budget_tokens} exceeds MAX_TOKEN_BUDGET {settings.max_token_budget}"
            )


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_MANIFEST
    try:
        manifest = load_gateway_manifest(path)
    except (ConfigError, OSError) as exc:
        print(f"FAIL {path}: {exc}", file=sys.stderr)
        return 1
    print(f"OK {path}: {len(manifest.routes)} routes, semantic cache off on {DECISION_ROUTE}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
