import pytest

from dynatrace_mcp.config import ConfigError, load_settings, normalize_base_url

from conftest import BASE, TOKEN


@pytest.mark.parametrize("raw,expected", [
    ("https://dt.corp/e/env-1", "https://dt.corp/e/env-1"),
    ("https://dt.corp/e/env-1/", "https://dt.corp/e/env-1"),
    ("https://dt.corp/e/env-1/api/v2/", "https://dt.corp/e/env-1"),
    ("https://dt.corp:9999/e/env-1/api/v2", "https://dt.corp:9999/e/env-1"),
    ("dt.corp/e/env-1", "https://dt.corp/e/env-1"),
    ("https://abc12345.live.dynatrace.com", "https://abc12345.live.dynatrace.com"),
    ("https://abc12345.live.dynatrace.com/api/v2", "https://abc12345.live.dynatrace.com"),
])
def test_normalize_base_url(raw, expected):
    url, _ = normalize_base_url(raw)
    assert url == expected


def test_managed_ui_link_is_trimmed():
    url, warnings = normalize_base_url("https://dt.corp/e/env-1/ui/problems?gtf=-2h")
    assert url == "https://dt.corp/e/env-1"
    assert warnings


def test_platform_url_is_rewritten_to_live():
    url, warnings = normalize_base_url("https://abc12345.apps.dynatrace.com")
    assert url == "https://abc12345.live.dynatrace.com"
    assert "classic API v2" in warnings[0]


def test_managed_without_env_path_warns():
    _, warnings = normalize_base_url("https://dt.corp")
    assert any("/e/<environment-id>" in w for w in warnings)


def test_plain_http_rejected_unless_allowed():
    with pytest.raises(ConfigError, match="plain http"):
        normalize_base_url("http://dt.corp/e/env-1")
    url, _ = normalize_base_url("http://dt.corp/e/env-1", allow_http=True)
    assert url.startswith("http://")
    assert normalize_base_url("http://localhost:8080/e/x")[0] == "http://localhost:8080/e/x"


def test_credentials_in_url_rejected():
    with pytest.raises(ConfigError, match="credentials"):
        normalize_base_url("https://user:pw@dt.corp/e/env-1")


def test_missing_values():
    with pytest.raises(ConfigError, match="DT_BASE_URL"):
        load_settings({"DT_API_TOKEN": TOKEN})
    with pytest.raises(ConfigError, match="DT_API_TOKEN"):
        load_settings({"DT_BASE_URL": BASE})


@pytest.mark.parametrize("bad", ["Api-Token " + TOKEN, "abc\ndef", "a b"])
def test_bad_tokens(bad):
    with pytest.raises(ConfigError):
        load_settings({"DT_BASE_URL": BASE, "DT_API_TOKEN": bad})


def test_token_surrounding_whitespace_is_stripped():
    assert load_settings({"DT_BASE_URL": BASE, "DT_API_TOKEN": f"  {TOKEN}\n"}).api_token == TOKEN


def test_defaults_are_safe():
    s = load_settings({"DT_BASE_URL": BASE, "DT_API_TOKEN": TOKEN})
    assert s.enable_write is False
    assert s.redact is True
    assert s.insecure_tls is False
    assert s.dql_enabled is False
    assert s.api_root == BASE + "/api/v2"
    assert s.deployment == "managed"


def test_token_never_in_repr_or_describe():
    s = load_settings({"DT_BASE_URL": BASE, "DT_API_TOKEN": TOKEN, "DT_PROXY": "http://u:p@proxy:3128"})
    assert TOKEN not in repr(s)
    described = str(s.describe())
    assert TOKEN not in described
    assert "u:p" not in described
    assert s.describe()["api_token"] == "set"


def test_ca_bundle_and_insecure(tmp_path):
    with pytest.raises(ConfigError, match="DT_CA_BUNDLE"):
        load_settings({"DT_BASE_URL": BASE, "DT_API_TOKEN": TOKEN, "DT_CA_BUNDLE": str(tmp_path / "nope.pem")})
    s = load_settings({"DT_BASE_URL": BASE, "DT_API_TOKEN": TOKEN, "DT_INSECURE_TLS": "true"})
    assert s.insecure_tls
    assert any("NOT verified" in w for w in s.warnings)


def test_flags_and_numbers():
    s = load_settings({"DT_BASE_URL": BASE, "DT_API_TOKEN": TOKEN, "DT_ENABLE_WRITE": "yes",
                       "DT_TIMEOUT": "12.5", "DT_DISABLE_TOOLS": "close_problem, list_slos"})
    assert s.enable_write and s.timeout == 12.5
    assert s.disabled_tools == ("close_problem", "list_slos")
    with pytest.raises(ConfigError, match="DT_TIMEOUT"):
        load_settings({"DT_BASE_URL": BASE, "DT_API_TOKEN": TOKEN, "DT_TIMEOUT": "fast"})


def test_dql_needs_both_values():
    s = load_settings({"DT_BASE_URL": BASE, "DT_API_TOKEN": TOKEN, "DT_PLATFORM_URL": "https://x.apps.dynatrace.com"})
    assert not s.dql_enabled
    s = load_settings({"DT_BASE_URL": BASE, "DT_API_TOKEN": TOKEN, "DT_PLATFORM_URL": "https://x.apps.dynatrace.com",
                       "DT_PLATFORM_TOKEN": "dt0s16.ABCDEFGH.IJKLMNOP"})
    assert s.dql_enabled
