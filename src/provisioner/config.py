"""Configuration: a YAML file for structure, environment variables for secrets and overrides.

Precedence, highest first: environment variable, YAML file, built-in default. Secrets never
need to appear in the file because any field can come from the environment instead. The
same environment variable names work with or without a file, so a plain `--env-file`
deployment keeps working.

The file is named by CONFIG_FILE. When that is unset the loader looks for ./config.yaml and
then /etc/provisioner/config.yaml, and runs without a file if neither exists.

    ldap:       -> LdapSettings,    env prefix LDAP_
    pelican:    -> PelicanSettings, env prefix PELICAN_
    top level   -> RunSettings:     jobs (env JOBS as JSON, or LDAP_GROUP + PELICAN_BASE_PATH
                                    for a single users job),
                                    dry_run, max_delete_fraction, allow_mass_delete,
                                    log_level, log_format (env names are the upper-cased fields)
"""

from __future__ import annotations

import os
import posixpath
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml
from pydantic import BaseModel, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_CONFIG_PATHS = ("config.yaml", "/etc/provisioner/config.yaml")


class ConfigError(Exception):
    """Raised when configuration is missing or malformed."""


# The parsed YAML document for the load in progress. A ContextVar rather than a global so
# tests loading different documents cannot bleed into each other.
_yaml_document: ContextVar[dict[str, Any]] = ContextVar("_yaml_document", default={})


class YamlSectionSource(PydanticBaseSettingsSource):
    """Feeds one top-level section of the YAML document into a settings class."""

    def __init__(self, settings_cls: type[BaseSettings], section: str | None) -> None:
        super().__init__(settings_cls)
        doc = _yaml_document.get()
        data = doc if section is None else doc.get(section) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"config section {section!r} must be a mapping")
        self._data = {k: v for k, v in data.items() if k in settings_cls.model_fields}

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


class SectionSettings(BaseSettings):
    """BaseSettings whose values come from env first, then the YAML section, then defaults."""

    yaml_section: ClassVar[str | None] = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            YamlSectionSource(settings_cls, cls.yaml_section),
        )


class LdapSettings(SectionSettings):
    model_config = SettingsConfigDict(env_prefix="LDAP_", extra="ignore")
    yaml_section = "ldap"

    server: str
    port: int = 636
    use_ssl: bool = True
    bind_dn: str
    bind_password: SecretStr
    user_search_base: str
    member_of_attribute: str = "isMemberOf"
    allow_empty_group: bool = False
    username_attribute: str = "employeeNumber"
    display_name_attribute: str = "cn"
    sub_attribute: str = "uid"
    timeout_seconds: int = 30
    page_size: int = 500


class PelicanSettings(SectionSettings):
    model_config = SettingsConfigDict(env_prefix="PELICAN_", extra="ignore")
    yaml_section = "pelican"

    origin_url: str
    issuer: str = "https://cilogon.org"
    collection_role: Literal["read", "write", "owner"] = "write"
    collection_description: str = "AUTOMATED: Do not manually edit this group."
    collection_visibility: Literal["private", "public"] = "private"
    ca_bundle: str | None = None
    verify_tls: bool = True
    timeout_seconds: int = 30
    # Authentication: either a server-minted JWT, or an admin username and password
    # used to log in and obtain one. See design/user-collections.md, Authentication.
    api_token: SecretStr | None = None
    admin_username: str | None = None
    admin_password: SecretStr | None = None

    @property
    def bearer_token(self) -> str | None:
        """The configured JWT, or None when login credentials are to be used instead."""
        if self.api_token is None:
            return None
        return self.api_token.get_secret_value().strip() or None

    @field_validator("origin_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _require_credentials(self) -> PelicanSettings:
        has_login = bool(self.admin_username) and self.admin_password is not None
        if self.bearer_token is None and not has_login:
            raise ValueError(
                "set PELICAN_API_TOKEN, or both PELICAN_ADMIN_USERNAME and PELICAN_ADMIN_PASSWORD"
            )
        return self


def clean_base_path(value: str) -> str:
    """Normalize a federation path such as /test/data. Must be absolute; '..' components
    are rejected because the value ends up spliced into token scopes."""
    value = value.strip()
    if not value.startswith("/"):
        raise ValueError("must start with '/'")
    if ".." in value.split("/"):
        raise ValueError("must not contain '..' path components")
    return posixpath.normpath(value)


class Job(BaseModel):
    """One LDAP group synchronized into one subtree of the Origin.

    kind "users": every member gets a personal collection at <base_path>/<employeeNumber>
    with a write grant to their own Pelican user.

    kind "projects": the members' other memberships are matched against the project names
    in the JSON at projects_url; every project with at least one member gets a collection at
    <base_path>/<project> with a write grant to a Pelican group of the same name whose
    membership is kept equal to the project's members.

    base_path may sit anywhere at or below an exported namespace, e.g. /test/data on an
    Origin exporting /test. The script owns it exclusively.
    """

    kind: Literal["users", "projects"]
    group: str
    base_path: str
    projects_url: str | None = None

    @field_validator("base_path")
    @classmethod
    def _clean(cls, value: str) -> str:
        return clean_base_path(value)

    @model_validator(mode="after")
    def _projects_need_url(self) -> Job:
        if self.kind == "projects" and not self.projects_url:
            raise ValueError("a projects job needs projects_url")
        return self


class RunSettings(SectionSettings):
    model_config = SettingsConfigDict(extra="ignore")
    yaml_section = None  # top level of the document

    jobs: list[Job] = []
    dry_run: bool = False
    # Refuse a run that would delete more than this fraction of a job's collections
    # (always allowing one). Bypass for one run with allow_mass_delete.
    max_delete_fraction: float = 0.05
    allow_mass_delete: bool = False
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    @field_validator("max_delete_fraction")
    @classmethod
    def _fraction_range(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("must be between 0 and 1")
        return value

    @field_validator("jobs")
    @classmethod
    def _base_paths_disjoint(cls, jobs: list[Job]) -> list[Job]:
        """Each job owns its base path exclusively, so two jobs may not share or nest paths."""
        for i, a in enumerate(jobs):
            for b in jobs[i + 1:]:
                pa, pb = a.base_path.rstrip("/") + "/", b.base_path.rstrip("/") + "/"
                if pa.startswith(pb) or pb.startswith(pa):
                    raise ValueError(
                        f"jobs {a.group!r} ({a.base_path}) and {b.group!r} ({b.base_path}) have the same or nested base paths"
                    )
        return jobs

    @field_validator("jobs")
    @classmethod
    def _one_projects_job(cls, jobs: list[Job]) -> list[Job]:
        """A projects job deletes every marked group that is not one of its own projects, so a
        second projects job on the same Origin would delete the first one's groups each run."""
        projects = [j for j in jobs if j.kind == "projects"]
        if len(projects) > 1:
            raise ValueError(
                f"only one projects job is supported per Origin, got {len(projects)} "
                f"({', '.join(repr(j.group) for j in projects)}); each would delete the other's groups"
            )
        return jobs


@dataclass(frozen=True)
class Settings:
    """All configuration for one run."""

    ldap: LdapSettings
    pelican: PelicanSettings
    run: RunSettings

    @property
    def jobs(self) -> list[Job]:
        return self.run.jobs


def _describe(prefix: str, error: ValidationError) -> list[str]:
    """One line per validation problem, naming the environment variable where one exists.

    Top-level fields map to variables (prefix + upper-cased name). Nested locations such as
    jobs.0.base_path have no variable and are shown as the YAML path.
    """
    lines = []
    for item in error.errors():
        parts = [str(part) for part in item["loc"]]
        if not parts:
            name = prefix.rstrip("_") or "config"   # a model-level rule, e.g. the credentials check
        elif len(parts) == 1:
            name = f"{prefix}{parts[0]}".upper()
        else:
            name = ".".join(parts)
        lines.append(f"{name}: {item['msg']}")
    return lines


def find_config_file(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"CONFIG_FILE={explicit} does not exist")
        return path
    for candidate in DEFAULT_CONFIG_PATHS:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def load_yaml(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        with path.open() as fh:
            doc = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return doc


def _fallback_job_from_env(problems: list[str]) -> list[Job]:
    """A single job from LDAP_GROUP and PELICAN_BASE_PATH, for file-less deployments."""
    group = os.environ.get("LDAP_GROUP", "").strip()
    base_path = os.environ.get("PELICAN_BASE_PATH", "").strip()
    if not group and not base_path:
        problems.append("JOBS: define `jobs` in the config file, or set LDAP_GROUP and PELICAN_BASE_PATH")
    elif not group:
        problems.append("LDAP_GROUP: required when PELICAN_BASE_PATH is set without a `jobs` list")
    elif not base_path:
        problems.append("PELICAN_BASE_PATH: required when LDAP_GROUP is set without a `jobs` list")
    else:
        try:
            return [Job(kind="users", group=group, base_path=base_path)]
        except ValidationError as exc:
            problems.extend(f"PELICAN_BASE_PATH: {item['msg']}" for item in exc.errors())
    return []


def load_settings(config_file: str | None = None) -> Settings:
    """Load every settings group, collecting all validation problems into one error."""
    path = find_config_file(config_file if config_file is not None else os.environ.get("CONFIG_FILE"))
    token = _yaml_document.set(load_yaml(path))
    try:
        problems: list[str] = []

        def load(cls: type[SectionSettings], prefix: str) -> Any:
            try:
                return cls()
            except ValidationError as exc:
                problems.extend(_describe(prefix, exc))
                return None

        ldap: LdapSettings | None = load(LdapSettings, "LDAP_")
        pelican: PelicanSettings | None = load(PelicanSettings, "PELICAN_")
        run: RunSettings | None = load(RunSettings, "")
        if run is not None and not run.jobs:
            run.jobs = _fallback_job_from_env(problems)

        if problems or ldap is None or pelican is None or run is None:
            where = f" (config file: {path})" if path else " (no config file)"
            raise ConfigError("Configuration errors" + where + ":\n  " + "\n  ".join(problems))
        return Settings(ldap=ldap, pelican=pelican, run=run)
    finally:
        _yaml_document.reset(token)
