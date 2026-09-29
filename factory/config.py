"""factory.toml loading and secret resolution."""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("FACTORY_CONFIG", "~/.config/factory/factory.toml")).expanduser()


class ConfigError(Exception):
    pass


@dataclass
class Context:
    """A bounded context, matched on the ticket's canonical `Domain:` project (finks-ddd) or a label."""
    name: str
    repo: str
    domains: list[str] = field(default_factory=list)
    linear_labels: list[str] = field(default_factory=list)
    witnesses: list[str] = field(default_factory=list)

    def matches(self, domain: str | None, labels: list[str]) -> bool:
        return domain in self.domains or bool(set(labels) & set(self.linear_labels))


@dataclass
class Config:
    raw: dict
    db: Path
    mirrors: Path
    dispatches: Path
    contexts: list[Context]
    repos: dict[str, dict]
    witnesses: dict[str, dict]

    @property
    def linear(self) -> dict:
        return self.raw.get("linear", {})

    @property
    def kanban(self) -> dict:
        return self.raw.get("kanban", {"enabled": False})

    def context_for(self, domain: str | None, labels: list[str], repo_lines: list[str]) -> list[Context]:
        """Contexts matching the ticket; a `Repo:` line narrows a multi-repo domain to one."""
        found = [c for c in self.contexts if c.matches(domain, labels)]
        if len(found) > 1 and repo_lines:
            found = [c for c in found if c.repo.split("/")[-1] in repo_lines] or found
        return found

    def context(self, name: str) -> Context:
        for c in self.contexts:
            if c.name == name:
                return c
        raise ConfigError(f"unknown context {name!r}")

    def mirror_path(self, repo: str) -> Path:
        return self.mirrors / repo.replace("/", "__")

    def trunk(self, repo: str) -> str:
        return self.repos.get(repo, {}).get("trunk", "main")


def load(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    raw = tomllib.loads(path.read_text())
    paths = raw.get("paths", {})
    p = lambda k, d: Path(os.environ.get(f"FACTORY_{k.upper()}", paths.get(k, d))).expanduser()
    contexts = [Context(**c) for c in raw.get("context", [])]
    witnesses = raw.get("witness", {})
    for c in contexts:
        for w in c.witnesses:
            if w not in witnesses:
                raise ConfigError(f"context {c.name}: unknown witness {w}")
    for name, w in witnesses.items():
        if w.get("readonly") is not True:
            raise ConfigError(f"witness {name} must declare readonly = true")
    return Config(
        raw=raw,
        db=p("db", "~/.hermes/factory.db"),
        mirrors=p("mirrors", "~/.hermes/factory/mirrors"),
        dispatches=p("dispatches", "~/factory/dispatches"),
        contexts=contexts,
        repos=raw.get("repos", {}),
        witnesses=witnesses,
    )


_env_cache: dict[str, str] | None = None


def secret(cfg: Config, name: str) -> str:
    """Environment first, then the configured KEY=VALUE files (never logged)."""
    global _env_cache
    if name in os.environ:
        return os.environ[name]
    if _env_cache is None:
        _env_cache = {}
        for f in cfg.raw.get("secrets", {}).get("env_files", []):
            fp = Path(f).expanduser()
            if not fp.exists():
                continue
            for line in fp.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.removeprefix("export ").split("=", 1)
                    _env_cache.setdefault(k.strip(), v.strip().strip("'\""))
    if name not in _env_cache:
        raise ConfigError(f"secret {name} not found in environment or secrets.env_files")
    return _env_cache[name]
