"""The rules every setting follows (spoolsteward/config.py): never empty, `none` only where a setting may be unset, ranges, lists of
accepted words, lower-case booleans, one error message that lists every wrong setting and never a secret."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from spoolsteward.config import OPTIONAL, SECRETS, Settings, load_settings, problems

ROOT = Path(__file__).resolve().parent.parent
ALL = list(Settings.model_fields)


def env_name(field: str) -> str:
    return "SPOOLSTEWARD_" + field.upper()


def written(field: str) -> str:
    """The default of a setting, spelled the way it is written in .env."""
    default = Settings.model_fields[field].default
    if default is None or default == "":
        return "none"
    return ("true" if default else "false") if isinstance(default, bool) else str(default)


def test_the_list_of_optional_settings_is_exactly_the_ones_with_nothing_as_default():
    nothing = {f for f in ALL if Settings.model_fields[f].default in (None, "")}
    assert nothing <= OPTIONAL                                     # every "unset by default" setting accepts none
    assert OPTIONAL - nothing == {"lockout_file", "usb_bridge_host"}   # the two that have a value but can be switched off
    assert OPTIONAL <= set(ALL) and SECRETS <= set(ALL)


@pytest.mark.parametrize("field", ALL)
def test_every_default_is_accepted_when_written_in_env(field):
    """The values of docker/env.example are the defaults written out: they must pass the rules that check them."""
    assert Settings(**{field: written(field)}).model_dump()[field] == Settings.model_fields[field].default


@pytest.mark.parametrize("field", ALL)
@pytest.mark.parametrize("blank", ["", "   "])
def test_an_empty_line_is_always_an_error(field, blank, monkeypatch):
    monkeypatch.setenv(env_name(field), blank)
    with pytest.raises(ValidationError) as e:
        Settings()
    assert env_name(field) in "\n".join(problems(e.value))


@pytest.mark.parametrize("field", ALL)
def test_none_is_accepted_only_by_the_optional_settings(field):
    if field in OPTIONAL:
        for spelling in ("none", "NONE", "None"):
            assert Settings(**{field: spelling}).model_dump()[field] == Settings.model_fields[field].default
    else:
        with pytest.raises(ValidationError):
            Settings(**{field: "none"})


BOOLS = [f for f in ALL if Settings.model_fields[f].annotation is bool]


@pytest.mark.parametrize("field", BOOLS)
def test_booleans_are_true_or_false_in_lower_case(field):
    assert Settings(**{field: "true"}).model_dump()[field] is True and Settings(**{field: "false"}).model_dump()[field] is False
    for bad in ("True", "FALSE", "1", "0", "yes", "no", "on", "off", "t"):
        with pytest.raises(ValidationError):
            Settings(**{field: bad})


RANGES = {   # field: (accepted, refused)
    "port": (["1", "8631", "65535"], ["0", "65536", "-1", "80.5", "http"]),
    "radius_port": (["1", "1812", "65535"], ["0", "65536"]),
    "mdns_port": (["0", "8631", "65535"], ["-1", "65536"]),
    "notice_interval": (["0", "3600"], ["-1", "1.5"]),
    "escl_window_seconds": (["60", "900"], ["59", "901"]),
    "printers_refresh_seconds": (["5", "3600"], ["4", "3601", "0"]),
    "backup_hour": (["0", "23"], ["-1", "24"]),
    "backup_keep_daily": (["1", "30"], ["0", "-1"]),
    "backup_keep_weekly": (["0", "4"], ["-1"]),
    "backup_keep_monthly": (["0", "12"], ["-1"]),
    "key_history_days": (["0", "14"], ["-1"]),
    "key_ttl_days_local": (["1", "730"], ["0", "-5"]),
    "max_open_requests": (["1", "3"], ["0"]),
    "audit_max_mb": (["1", "50"], ["0"]),
    "web_max_copies": (["1", "20"], ["0"]),
    "reconcile_interval": (["0.5", "30"], ["0", "-1", "inf", "nan"]),
    "scan_timeout": (["1", "180.5"], ["0", "inf"]),
    "offline_remove_hours": (["0", "8", "0.5"], ["-1", "inf"]),
    "radius_timeout": (["1", "5"], ["0"]),
    "auth_cache_seconds": (["0", "60"], ["-1"]),
}


@pytest.mark.parametrize("field", RANGES)
def test_numbers_have_a_range(field):
    good, bad = RANGES[field]
    for v in good:
        Settings(**{field: v})
    for v in bad:
        with pytest.raises(ValidationError):
            Settings(**{field: v})


WORDS = {   # field: (accepted, refused)
    "log_level": (["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], ["info", "Info", "TRACE", "WARN"]),
    "language": (["en", "it", "es", "ru", "zh"], ["EN", "fr", "english"]),
    "ipp_auth": (["keys", "keys+radius"], ["radius", "Keys", "basic"]),
    "permission_mode": (["adjust", "refuse"], ["Adjust", "allow"]),
    "public_scheme": (["http", "https", "ipp", "ipps", "none"], ["ftp", "IPPS"]),
    "timezone": (["UTC", "Europe/Rome", "America/New_York"], ["Mars/Base", "rome", "+01:00"]),
    "healthz_info": (["version", "printers", "version,printers", "none"], ["secret", "version,secret", "version;printers"]),
    "cups_url": (["unix:///run/cups/cups.sock", "http://cups:631", "https://cups:631"], ["/run/cups/cups.sock", "ipp://cups", "unix://run"]),
    "portal_url": (["https://print.lan", "http://10.0.0.1:8631", "none"], ["print.lan", "ftp://x"]),
    "update_repository": (["deh-aleh/SpoolStewardTest", "a/b.c_d-e"], ["deh-aleh", "https://github.com/a/b", "a/..", "-a/b", "a/b c"]),
    "scan_url": (["http://ipp-usb:60000", "office=https://10.0.0.5:443", "a=http://x,http://y", "none"], ["ipp-usb:60000", "=http://x", "x=ftp://y"]),
    "usb_bridge_ports": (["60000-60099", "1-65535", "5-5"], ["60099-60000", "0-5", "1-65536", "60000", "a-b"]),
    "mdns_addresses": (["10.0.0.5", "10.0.0.5, 192.168.1.2", "none"], ["10.0.0.256", "::1", "host.lan"]),
    "mdns_hostname": (["print.lan", "print", "none"], ["-x", "a b", "a_b", "x."]),
    "database_url": (["sqlite:///./x.db", "sqlite:////data/x.db", "postgresql://u:p@h/db"], ["not a url", "x"]),
    "backup_dir": (["general", "pc/aula 3", "a.b-c_d"], ["../x", "/abs", "a//b", "a/b/c/d/e"]),
    "bootstrap_admin_username": (["administrator", "root.admin"], ["Admin", "-x", "a b"]),
    "usb_bridge_host": (["ipp-usb", "none"], []),
    "host": (["0.0.0.0", "::", "localhost"], ["a b"]),
}


@pytest.mark.parametrize("field", WORDS)
def test_words_must_be_in_the_accepted_list(field):
    good, bad = WORDS[field]
    for v in good:
        Settings(**{field: v})
    for v in bad:
        with pytest.raises(ValidationError):
            Settings(**{field: v})


def test_the_languages_of_the_setting_are_the_languages_of_the_portal():
    from spoolsteward.i18n import LANGUAGES
    allowed = set(Settings.model_fields["language"].annotation.__args__)
    assert allowed == set(LANGUAGES)


def test_radius_needs_host_and_secret_together():
    Settings(radius_host="radius.lan", radius_secret="s3cret")
    Settings(radius_host="none", radius_secret="none")
    for only in ({"radius_host": "radius.lan"}, {"radius_secret": "s3cret"}):
        with pytest.raises(ValidationError) as e:
            Settings(**only)
        assert "set both, or write none in both" in "\n".join(problems(e.value))


def test_an_error_lists_every_wrong_setting_and_never_a_secret(monkeypatch):
    monkeypatch.setenv("SPOOLSTEWARD_PORT", "0")
    monkeypatch.setenv("SPOOLSTEWARD_LOG_LEVEL", "info")
    monkeypatch.setenv("SPOOLSTEWARD_BACKUP_HOUR", "night")
    monkeypatch.setenv("SPOOLSTEWARD_RADIUS_SECRET", "hunter2-correct-horse")      # half of a pair: an error, with a secret in it
    with pytest.raises(ValidationError) as e:
        Settings()
    text = "\n".join(problems(e.value))
    for name in ("SPOOLSTEWARD_PORT", "SPOOLSTEWARD_LOG_LEVEL", "SPOOLSTEWARD_BACKUP_HOUR"):
        assert name in text
    assert "hunter2-correct-horse" not in text
    monkeypatch.setenv("SPOOLSTEWARD_SECRET_KEY", "")                             # a secret that is wrong: the name, not the value
    monkeypatch.setenv("SPOOLSTEWARD_BACKUP_RECIPIENT", "   ")
    with pytest.raises(ValidationError) as e:
        Settings()
    assert "SPOOLSTEWARD_SECRET_KEY" in "\n".join(problems(e.value))


def test_the_secret_key_placeholder_of_the_template_is_refused(monkeypatch):
    """docker/env.example copied by hand: __GENERATED__ is a key everybody knows."""
    monkeypatch.setenv("SPOOLSTEWARD_SECRET_KEY", "__GENERATED__")
    with pytest.raises(ValidationError) as e:
        Settings()
    assert "SPOOLSTEWARD_SECRET_KEY: still holds the placeholder" in "\n".join(problems(e.value))


def test_the_secret_key_needs_64_characters_the_length_of_openssl_rand_hex_32(monkeypatch):
    monkeypatch.setenv("SPOOLSTEWARD_SECRET_KEY", "k" * 63)
    with pytest.raises(ValidationError) as e:
        Settings()
    text = "\n".join(problems(e.value))
    assert "SPOOLSTEWARD_SECRET_KEY: is shorter than 64 characters" in text and "openssl rand -hex 32" in text
    assert "k" * 63 not in text                                                     # the name, never the value
    monkeypatch.setenv("SPOOLSTEWARD_SECRET_KEY", "0123456789abcdef" * 4)           # what openssl rand -hex 32 prints
    assert Settings().secret_key == "0123456789abcdef" * 4
    monkeypatch.setenv("SPOOLSTEWARD_SECRET_KEY", "none")                           # a random key at every start
    assert Settings().secret_key is None


def test_starting_with_wrong_settings_exits_with_code_2_and_a_readable_list(monkeypatch, capsys):
    monkeypatch.setenv("SPOOLSTEWARD_ESCL_WINDOW_SECONDS", "")
    monkeypatch.setenv("SPOOLSTEWARD_ESCL_ANONYMOUS", "True")
    with pytest.raises(SystemExit) as e:
        load_settings()
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("refusing to start: wrong settings") and "write none" in err
    assert "SPOOLSTEWARD_ESCL_WINDOW_SECONDS: must not be empty" in err
    assert "SPOOLSTEWARD_ESCL_ANONYMOUS: must be true or false (lower case)" in err
    assert "Traceback" not in err


def test_the_template_of_env_is_valid_as_written(monkeypatch):
    """docker/env.example, every active SPOOLSTEWARD_ line at once (what a new installation starts with)."""
    template = (ROOT / "docker" / "env.example").read_text()
    lines = {m[1]: m[2] for m in re.finditer(r"^(SPOOLSTEWARD_[A-Z0-9_]+)=(.*)$", template, re.M)}
    assert len(lines) > 30
    for name, value in lines.items():
        if name in ("SPOOLSTEWARD_REGISTRY", "SPOOLSTEWARD_VERSION"):
            continue                                                    # part A: read by compose, not by the program
        monkeypatch.setenv(name, "x" * 64 if value == "__GENERATED__" else value)
    Settings()


def test_the_template_has_no_empty_active_line():
    for line in (ROOT / "docker" / "env.example").read_text().splitlines():
        assert not re.match(r"^[A-Z][A-Z0-9_]*=$", line), f"{line} must be a value or none"
