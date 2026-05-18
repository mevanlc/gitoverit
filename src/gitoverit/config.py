from __future__ import annotations

import fnmatch
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

RuleType = Literal["literal_full", "literal_substring", "glob", "regex"]
_RULE_TYPES: frozenset[str] = frozenset(
    {"literal_full", "literal_substring", "glob", "regex"}
)


@dataclass(frozen=True)
class IdRewriteRule:
    type: RuleType
    find: str
    replace: str
    fg: str = ""  # Rich foreground color spec; empty = inherit global


@dataclass(frozen=True)
class Config:
    log_file: str = ""
    log_level: str = ""
    column_priorities: str = ""
    id_rewrite_rules: tuple[IdRewriteRule, ...] = ()
    id_rewrite_fg_color: str = ""


def config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "gito" / "config.toml"


def _warn(msg: str) -> None:
    print(f"gito: warning: {msg}", file=sys.stderr)


def _load_file(path: Path) -> dict[str, object]:
    try:
        with open(path, "rb") as fp:
            return tomllib.load(fp)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _warn(f"could not read {path}: {exc}")
        return {}


def _str_field(data: dict[str, object], key: str) -> str:
    value = data.get(key, "")
    return value if isinstance(value, str) else ""


def _parse_rules(data: dict[str, object], path: Path) -> tuple[IdRewriteRule, ...]:
    raw = data.get("id_rewrite")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _warn(f"{path}: id_rewrite must be an array of tables; ignoring")
        return ()

    rules: list[IdRewriteRule] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            _warn(f"{path}: id_rewrite[{idx}] is not a table; skipping")
            continue
        rtype = entry.get("type")
        find = entry.get("find")
        replace = entry.get("replace", "")
        fg = entry.get("fg", "")
        if not isinstance(rtype, str) or rtype not in _RULE_TYPES:
            _warn(
                f"{path}: id_rewrite[{idx}] has invalid type "
                f"{rtype!r} (expected one of {sorted(_RULE_TYPES)}); skipping"
            )
            continue
        if not isinstance(find, str) or not isinstance(replace, str):
            _warn(f"{path}: id_rewrite[{idx}] find/replace must be strings; skipping")
            continue
        if not isinstance(fg, str):
            _warn(f"{path}: id_rewrite[{idx}] fg must be a string; ignoring fg")
            fg = ""
        if rtype == "regex":
            try:
                re.compile(find)
            except re.error as exc:
                _warn(f"{path}: id_rewrite[{idx}] invalid regex {find!r}: {exc}; skipping")
                continue
        rules.append(IdRewriteRule(type=rtype, find=find, replace=replace, fg=fg))  # type: ignore[arg-type]
    return tuple(rules)


@lru_cache(maxsize=1)
def load_config() -> Config:
    path = config_path()
    data = _load_file(path)
    log_file = _str_field(data, "log_file")
    log_level = _str_field(data, "log_level")
    column_priorities = _str_field(data, "column_priorities")
    fg_color = _str_field(data, "id_rewrite_fg_color")
    rules = _parse_rules(data, path)

    # Environment variables override file values for backward compatibility.
    env_log = os.environ.get("GITO_DEBUG_LOG") or os.environ.get("GITOVERIT_DEBUG_LOG")
    if env_log:
        log_file = env_log
    env_level = os.environ.get("GITO_LOG_LEVEL")
    if env_level:
        log_level = env_level
    env_priorities = (
        os.environ.get("GITO_COLUMN_PRIORITIES")
        or os.environ.get("GITOVERIT_COLUMN_PRIORITIES")
    )
    if env_priorities:
        column_priorities = env_priorities

    return Config(
        log_file=log_file,
        log_level=log_level,
        column_priorities=column_priorities,
        id_rewrite_rules=rules,
        id_rewrite_fg_color=fg_color,
    )


_LEVELS = {
    "": 10,
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "WARN": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


def level_value(name: str) -> int:
    return _LEVELS.get(name.strip().upper(), 10)


def _match_and_rewrite(rule: IdRewriteRule, value: str) -> str | None:
    """Return the rewritten value if the rule matches, else None."""
    if rule.type == "literal_full":
        return rule.replace if value == rule.find else None
    if rule.type == "literal_substring":
        if not rule.find or rule.find not in value:
            return None
        return value.replace(rule.find, rule.replace)
    if rule.type == "glob":
        return rule.replace if fnmatch.fnmatchcase(value, rule.find) else None
    if rule.type == "regex":
        try:
            if re.search(rule.find, value) is None:
                return None
            return re.sub(rule.find, rule.replace, value)
        except re.error:
            return None
    return None


@dataclass(frozen=True)
class RewriteResult:
    value: str
    fg: str | None  # None when no rewrite applied


def apply_id_rewrites(identity: str) -> RewriteResult:
    """Apply the first matching rule. Once a rule matches, no further rules run."""
    cfg = load_config()
    for rule in cfg.id_rewrite_rules:
        rewritten = _match_and_rewrite(rule, identity)
        if rewritten is None:
            continue
        fg = rule.fg or cfg.id_rewrite_fg_color
        return RewriteResult(value=rewritten, fg=fg or None)
    return RewriteResult(value=identity, fg=None)


__all__ = [
    "Config",
    "IdRewriteRule",
    "RewriteResult",
    "apply_id_rewrites",
    "config_path",
    "level_value",
    "load_config",
]
