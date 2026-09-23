from __future__ import annotations

import textwrap

import pytest

from provisioner.config import ConfigError, Job, load_settings

ENV_VARS = [
    "CONFIG_FILE", "JOBS",
    "LDAP_SERVER", "LDAP_PORT", "LDAP_USE_SSL", "LDAP_BIND_DN", "LDAP_BIND_PASSWORD",
    "LDAP_USER_SEARCH_BASE", "LDAP_GROUP", "LDAP_ALLOW_EMPTY_GROUP", "LDAP_USERNAME_ATTRIBUTE",
    "PELICAN_ORIGIN_URL", "PELICAN_API_TOKEN", "PELICAN_ADMIN_USERNAME", "PELICAN_ADMIN_PASSWORD",
    "PELICAN_BASE_PATH", "PELICAN_COLLECTION_ROLE", "PELICAN_ISSUER", "PELICAN_VERIFY_TLS",
    "DRY_RUN", "LOG_LEVEL", "LOG_FORMAT", "MAX_DELETE_FRACTION", "ALLOW_MASS_DELETE",
]

SECRETS = {
    "LDAP_BIND_PASSWORD": "ldap-s3cr3t",
    "PELICAN_API_TOKEN": "eyJ.s3cr3t-token.sig",
}

YAML = textwrap.dedent("""
    ldap:
      server: ldap.example.org
      bind_dn: cn=readonly,dc=example
      user_search_base: ou=people,dc=example
      username_attribute: employeeNumber
    pelican:
      origin_url: https://origin.example.org/
      verify_tls: false
      collection_role: read
    jobs:
      - group: ap40-login
        base_path: /test/data/
      - group: ap41-login
        base_path: /ospool/ap41
    log_level: DEBUG
""")


@pytest.fixture
def clean_env(monkeypatch, tmp_path, tmp_path_factory):
    for key in ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    # Make sure ./config.yaml from the developer's checkout is not picked up.
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
    return monkeypatch


def write_yaml(tmp_path, text=YAML):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_yaml_plus_env_secrets(clean_env, tmp_path):
    path = write_yaml(tmp_path)
    for k, v in SECRETS.items():
        clean_env.setenv(k, v)
    clean_env.setenv("CONFIG_FILE", str(path))

    s = load_settings()

    assert s.ldap.server == "ldap.example.org"
    assert s.ldap.port == 636                                   # default
    assert s.ldap.bind_password.get_secret_value() == "ldap-s3cr3t"
    assert s.pelican.origin_url == "https://origin.example.org"  # trailing slash stripped
    assert s.pelican.verify_tls is False
    assert s.pelican.collection_role == "read"
    assert s.pelican.api_token.get_secret_value() == SECRETS["PELICAN_API_TOKEN"]
    assert s.run.log_level == "DEBUG"
    assert s.run.dry_run is False
    assert [(j.group, j.base_path) for j in s.jobs] == [("ap40-login", "/test/data"), ("ap41-login", "/ospool/ap41")]


def test_env_overrides_yaml(clean_env, tmp_path):
    path = write_yaml(tmp_path)
    for k, v in SECRETS.items():
        clean_env.setenv(k, v)
    clean_env.setenv("PELICAN_COLLECTION_ROLE", "write")
    clean_env.setenv("LDAP_SERVER", "other.example.org")
    clean_env.setenv("DRY_RUN", "true")

    s = load_settings(str(path))

    assert s.pelican.collection_role == "write"
    assert s.ldap.server == "other.example.org"
    assert s.run.dry_run is True


def test_env_only_with_single_job_fallback(clean_env):
    env = {
        "LDAP_SERVER": "ldap.example.org", "LDAP_BIND_DN": "cn=ro,dc=example",
        "LDAP_USER_SEARCH_BASE": "ou=people,dc=example", "LDAP_GROUP": "ap40-login",
        "PELICAN_ORIGIN_URL": "https://origin.example.org", "PELICAN_BASE_PATH": "/test/data",
        **SECRETS,
    }
    for k, v in env.items():
        clean_env.setenv(k, v)

    s = load_settings()
    assert s.jobs == [Job(group="ap40-login", base_path="/test/data")]


def test_jobs_from_env_json(clean_env):
    env = {
        "LDAP_SERVER": "ldap.example.org", "LDAP_BIND_DN": "cn=ro,dc=example",
        "LDAP_USER_SEARCH_BASE": "ou=people,dc=example",
        "PELICAN_ORIGIN_URL": "https://origin.example.org",
        "JOBS": '[{"group": "g1", "base_path": "/a"}, {"group": "g2", "base_path": "/b/c"}]',
        **SECRETS,
    }
    for k, v in env.items():
        clean_env.setenv(k, v)
    s = load_settings()
    assert [(j.group, j.base_path) for j in s.jobs] == [("g1", "/a"), ("g2", "/b/c")]


def test_reports_every_problem_at_once(clean_env, tmp_path):
    path = write_yaml(tmp_path, "pelican:\n  collection_role: admin\njobs:\n  - group: g\n    base_path: relative\n")
    with pytest.raises(ConfigError) as info:
        load_settings(str(path))
    msg = str(info.value)
    for expected in ("LDAP_SERVER", "LDAP_BIND_DN", "LDAP_BIND_PASSWORD", "LDAP_USER_SEARCH_BASE",
                     "PELICAN_ORIGIN_URL", "PELICAN_COLLECTION_ROLE",
                     "jobs.0.base_path", "must start with"):
        assert expected in msg, expected


def test_missing_credentials_reported(clean_env, tmp_path):
    # The credentials rule is a model-level check, so it only runs once every field is
    # individually valid. Give a valid origin_url and no token or login.
    path = write_yaml(tmp_path, "pelican:\n  origin_url: https://origin.example.org\n")
    clean_env.setenv("LDAP_BIND_PASSWORD", "x")
    with pytest.raises(ConfigError) as info:
        load_settings(str(path))
    assert "PELICAN_API_TOKEN, or both PELICAN_ADMIN_USERNAME and PELICAN_ADMIN_PASSWORD" in str(info.value)


def test_no_jobs_anywhere_is_an_error(clean_env):
    env = {
        "LDAP_SERVER": "ldap.example.org", "LDAP_BIND_DN": "cn=ro,dc=example",
        "LDAP_USER_SEARCH_BASE": "ou=people,dc=example",
        "PELICAN_ORIGIN_URL": "https://origin.example.org", **SECRETS,
    }
    for k, v in env.items():
        clean_env.setenv(k, v)
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert "JOBS" in str(info.value)


def test_missing_explicit_config_file(clean_env):
    with pytest.raises(ConfigError) as info:
        load_settings("/nonexistent/config.yaml")
    assert "does not exist" in str(info.value)


def test_base_path_rejects_traversal():
    with pytest.raises(ValueError):
        Job(group="g", base_path="/test/../etc")
    assert Job(group="g", base_path="/test//data/").base_path == "/test/data"


def test_secrets_not_in_repr(clean_env, tmp_path):
    path = write_yaml(tmp_path)
    for k, v in SECRETS.items():
        clean_env.setenv(k, v)
    s = load_settings(str(path))
    assert "ldap-s3cr3t" not in repr(s.ldap)
    assert "s3cr3t-token" not in repr(s.pelican)


def test_jobs_with_nested_base_paths_rejected(clean_env, tmp_path):
    path = write_yaml(tmp_path, YAML.replace("base_path: /ospool/ap41", "base_path: /test/data/inner"))
    for k, v in SECRETS.items():
        clean_env.setenv(k, v)
    with pytest.raises(ConfigError) as info:
        load_settings(str(path))
    assert "nested base paths" in str(info.value)


def test_delete_ceiling_settings(clean_env, tmp_path):
    path = write_yaml(tmp_path)
    for k, v in SECRETS.items():
        clean_env.setenv(k, v)
    s = load_settings(str(path))
    assert s.run.max_delete_fraction == 0.05 and s.run.allow_mass_delete is False

    clean_env.setenv("MAX_DELETE_FRACTION", "0.2")
    clean_env.setenv("ALLOW_MASS_DELETE", "true")
    s = load_settings(str(path))
    assert s.run.max_delete_fraction == 0.2 and s.run.allow_mass_delete is True

    clean_env.setenv("MAX_DELETE_FRACTION", "1.5")
    with pytest.raises(ConfigError) as info:
        load_settings(str(path))
    assert "MAX_DELETE_FRACTION" in str(info.value)


def test_fallback_job_bad_base_path_names_the_variable(clean_env):
    env = {
        "LDAP_SERVER": "ldap.example.org", "LDAP_BIND_DN": "cn=ro,dc=example",
        "LDAP_USER_SEARCH_BASE": "ou=people,dc=example", "LDAP_GROUP": "ap40-login",
        "PELICAN_ORIGIN_URL": "https://origin.example.org", "PELICAN_BASE_PATH": "relative/path",
        **SECRETS,
    }
    for k, v in env.items():
        clean_env.setenv(k, v)
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert "PELICAN_BASE_PATH: Value error, must start with '/'" in str(info.value)
