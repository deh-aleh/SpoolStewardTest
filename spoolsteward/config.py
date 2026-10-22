"""Every setting of the program, read from the environment (SPOOLSTEWARD_*).

One rule for all of them, checked once at start (see load_settings):
  * a setting is never empty. A line like `SPOOLSTEWARD_PORT=` is an error;
  * a setting that can be switched off or left unset (OPTIONAL below) is written `none`;
  * numbers have a range, words have a list of accepted values, booleans are `true` or `false` (lower case);
  * a wrong value stops the start, and the message lists every wrong setting at once.
Nothing is corrected silently: a typo must not turn into some other value.
"""

import ipaddress
import re
import sys
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


NONE_WORD = "none"
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")   # user names in the portal, the guest account, the emergency administrator
BACKUP_FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}(/[A-Za-z0-9][A-Za-z0-9._ -]{0,63}){0,3}$")
HEALTHZ_ITEMS = ("version", "printers")
# a GitHub repository, owner/name, with the characters GitHub allows (scripts/update.sh checks the same rule)
REPOSITORY_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}", re.ASCII)
SECRET_KEY_MIN = 64   # characters: the length of `openssl rand -hex 32`, the command the docs and messages give
# Settings that may be switched off: they are written `none` (never left empty) and then hold their default
# (nothing: "" or None). The others always need a real value.
OPTIONAL = frozenset({
    "tls_certfile", "tls_keyfile", "public_scheme", "secret_key", "radius_host", "radius_secret", "portal_url",
    "audit_archive_host_path", "lockout_file", "scan_url", "healthz_info", "usb_bridge_host", "mdns_hostname",
    "mdns_addresses", "backup_host_path", "backup_recipient",
})
SECRETS = frozenset({"secret_key", "radius_secret", "backup_recipient"})   # never shown in an error message


class Settings(BaseSettings):
    # only real environment variables: a .env here belongs to docker compose, not to the app.
    # SPOOLSTEWARD_REGISTRY and SPOOLSTEWARD_VERSION (docker/env.example, part A) are for compose and scripts/update.sh: never add settings with those names
    model_config = SettingsConfigDict(env_prefix="SPOOLSTEWARD_", extra="ignore", validate_default=False)   # the defaults are ours: only what the user writes is checked

    database_url: str = "sqlite:///./spoolsteward.db"
    # How carefully SQLite saves (PRAGMA synchronous): "full" waits for the disk after every save, "normal" does not (faster on an
    # SD card; a power cut can lose the last few seconds of saves, the database itself stays sound). See docker/env.example.
    db_sync: Literal["full", "normal"] = "full"
    # CUPS is a private backend; the gateway owns :631. "unix:///run/cups/cups.sock" is the recommended form.
    cups_url: str = "unix:///run/cups/cups.sock"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # How often the same notice (same kind, same source) is reported again, in seconds. 0 = decided by log_level:
    # DEBUG 1 minute, INFO 1 day, WARNING 3 days, ERROR/CRITICAL 7 days (see spoolsteward/notices.py)
    notice_interval: int = Field(0, ge=0)
    # a paper jam, no paper or no ink/toner is always reported to the administrators (the bell); with this on, the people who
    # have a print waiting on that printer are told too, in their own bell
    printer_alerts_users: bool = True
    host: str = Field("0.0.0.0", pattern=r"^\S+$")
    port: int = Field(8631, ge=1, le=65535)
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    # set only behind a TLS-terminating reverse proxy ("ipps"); none = derived from the request
    public_scheme: Literal["http", "https", "ipp", "ipps"] | None = None
    # Basic credentials over plain HTTP are refused unless this is set (tests / trusted lab only)
    allow_insecure_auth: bool = False
    timezone: str = "UTC"
    # language of the portal for visitors whose browser does not say (and for users who did not choose)
    # tests/test_config.py checks that this list equals spoolsteward.i18n.LANGUAGES
    language: Literal["en", "it", "es", "ru", "zh"] = "en"
    default_group: str = Field("default", min_length=1)
    # Prepended to the user name CUPS sees. It can never equal a local account, so a RADIUS user
    # called "root" (or any member of CUPS' SystemGroup) cannot become a CUPS administrator.
    cups_user_prefix: str = Field("gw:", min_length=1)
    # answer Get-Printer-Attributes without login: required by IPP Everywhere clients to set a printer up
    anonymous_discovery: bool = True
    # Content-Security-Policy of the portal pages: "enforce" (default), "report-only" (the browser only logs what it would
    # block: for finding out why a page misbehaves behind a proxy that changes the pages), or "off"
    content_security_policy: Literal["enforce", "report-only", "off"] = "enforce"
    # Strict-Transport-Security: how many seconds a browser must keep using HTTPS only for this server, sent only on HTTPS answers.
    # 0 = not sent. A browser keeps it only for a certificate it trusts (it ignores it over a certificate error); once kept it forbids
    # clicking through a certificate error, for every port of the host name. Off by default: it is for someone who has a trusted certificate.
    hsts_seconds: int = Field(0, ge=0, le=63072000)
    # eSCL scans without credentials (NAPS2 and others cannot send them): attributed to a local account with no password.
    escl_anonymous: bool = False
    # the name of that account. If a real account (with a password, from RADIUS, or an administrator) already has the name,
    # those scans are refused and the administrators are told in the bell (see spoolsteward/escl.py: _guest)
    escl_guest_username: str = Field("unknown-guest", pattern=USERNAME_RE.pattern)
    # "scan window": a button on /me/scan lets the person tell the gateway "my scan app is about to scan"; for this many
    # seconds (plus the scan itself) credential-less eSCL requests from the IP that pressed it are accepted, as that person.
    escl_window: bool = True
    escl_window_seconds: int = Field(180, ge=60, le=900)
    max_request_mb: int = Field(200, ge=1)  # IPP request (document included) larger than this is refused
    reconcile_interval: float = Field(30.0, gt=0, allow_inf_nan=False)  # seconds between CUPS job-state syncs
    secret_key: str | None = None  # signs admin session cookies; random per start if none
    auto_create_users: bool = True

    # RADIUS is optional: with host and secret both `none` only local accounts exist
    radius_host: str = ""
    radius_port: int = Field(1812, ge=1, le=65535)
    radius_secret: str = ""
    radius_timeout: int = Field(5, ge=1)
    auth_cache_seconds: int = Field(60, ge=0)
    # How many password checks (argon2, a few tenths of a second and 32 MiB of memory each) run at the same time; the others wait.
    # It keeps a flood of login attempts from using all the memory of a small machine.
    password_checks_at_once: int = Field(3, ge=1, le=16)

    # What a print client may present as password: "keys" = only personal print keys (default),
    # "keys+radius" = also the RADIUS password (transition; it encourages saving the domain password on clients)
    ipp_auth: Literal["keys", "keys+radius"] = "keys"
    # The longest validity a user may pick for a print key (the menu offers only the durations up to this). RADIUS-backed
    # users must come back to the portal (a RADIUS login) to renew, so someone who left the company loses access.
    key_ttl_days_radius: int = Field(90, ge=1)
    key_ttl_days_local: int = Field(730, ge=1)
    key_once_hours: int = Field(24, ge=1)  # a single-use key that nobody used is void after this
    key_once_grace_seconds: int = Field(120, ge=0)  # after its print, a single-use key still answers status queries for this long
    key_history_days: int = Field(14, ge=0)  # revoked and expired keys disappear from the list after this (0 = never)
    key_expiry_warn_enabled: bool = False  # tell the user in the bell that a key is about to expire
    key_expiry_warn_days: int = Field(7, ge=1)
    # public address of the portal, used in messages ("ask for more quota at ..."); none = derived from the request
    portal_url: str = ""

    # first-run admin: created without a password, set with a one-time code printed in the log
    bootstrap_admin: bool = True
    bootstrap_admin_username: str = Field("administrator", pattern=USERNAME_RE.pattern)

    # printing from the browser (phones): largest accepted file and copies per job
    web_upload_mb: int = Field(25, ge=1)
    web_max_copies: int = Field(20, ge=1)

    # failed logins are kept in this file, so a restart does not end a lock-out (none = memory only)
    lockout_file: str = "/data/login-lockout.json"

    # What happens when a client explicitly asks for colour or a one-sided print that the user's permissions do not allow:
    # "adjust" = the job is printed in black and white / double-sided and the user is told in their bell,
    # "refuse" = the job is refused. Printers and allowed hours always refuse. See docs/en/quotas.md ("Advanced permissions").
    permission_mode: Literal["adjust", "refuse"] = "adjust"

    max_open_requests: int = Field(3, ge=1)
    request_ttl_days: int = Field(14, ge=1)
    # true = a request (quota, permission, group) can not be sent without a reason; false = the reason is optional
    request_reason_required: bool = False

    # audit: kept forever; when the active table outgrows this it is archived (compressed) to the folder below
    audit_max_mb: int = Field(50, ge=1)
    audit_archive_dir: str = Field("/data/audit-archive", min_length=1)
    audit_archive_host_path: str = ""  # shown in the portal only, so people know where the files are on the host
    audit_check_interval: float = Field(3600.0, gt=0, allow_inf_nan=False)
    # warnings about the TLS certificate (the one in SPOOLSTEWARD_TLS_CERTFILE). "Already expired" and "the announced address is
    # not in it" are always reported; "expires soon" only when asked for, so nobody gets a notice while the renewal is
    # simply not due yet.
    cert_expiry_warn_enabled: bool = False
    cert_expiry_warn_days: int = Field(14, ge=1)
    cert_name_check: bool = True
    # forget the names of printed files after some days (the rows, pages and states stay: quotas need them). Off by default.
    job_title_retention_enabled: bool = False
    job_title_retention_days: int = Field(90, ge=1)

    # eSCL scanner behind the gateway (ipp-usb serves it next to IPP); none = scanning disabled.
    # A comma-separated list of ADDRESS or NAME=ADDRESS (http:// or https://)
    scan_url: str = ""
    scan_timeout: float = Field(180.0, gt=0, allow_inf_nan=False)  # a flatbed scan can take a while
    # Scans made in the portal wait in memory until they are downloaded. At most this many MB for all the people together (when it is
    # exceeded the oldest ones are dropped; the newest is always kept), and each one for at most this many minutes.
    scan_keep_mb: int = Field(50, ge=1)
    scan_keep_minutes: int = Field(10, ge=1)
    # true = a scan leaves the memory and the portal as soon as its download has finished
    scan_delete_after_download: bool = False

    # What /healthz tells besides the status (it needs no login, so anyone who reaches the port can read it).
    # A comma-separated list; none (default) = only the status. Known items: version, printers
    healthz_info: str = ""
    # Tell administrators when a newer release exists (one request a day to the releases of update_repository, nothing about
    # this server is sent)
    update_check: bool = True
    # betas (GitHub pre-releases, x.y.z-beta.N) are offered like final releases: by the check, the portal and scripts/update.sh
    update_beta: bool = True
    # the GitHub repository (owner/name) whose releases are offered and downloaded, here and by scripts/update.sh
    update_repository: str = "deh-aleh/SpoolSteward"
    # folder shared with the optional "updater" container (docker compose --profile autoupdate): requests go in, status comes out
    update_dir: str = Field("/data/update", min_length=1)

    # Printers page: how long what the printers say is reused, and every how many seconds the page reloads itself when the
    # administrator switches "automatic refresh" on
    printers_refresh_seconds: int = Field(15, ge=5, le=3600)

    # Finding printers by itself: USB printers are served either by the ipp-usb bridge (IPP over USB, preferred) or, for
    # printers that have no IPP-over-USB, by CUPS' own usb backend. A queue is created for each new device.
    auto_discover: bool = True
    discovery_interval: float = Field(30.0, gt=0, allow_inf_nan=False)  # seconds between looks
    usb_bridge_host: str = "ipp-usb"  # none = do not look for ipp-usb printers
    usb_bridge_ports: str = "60000-60099"  # the ports ipp-usb gives its printers (http-min-port..http-max-port)
    usb_direct: bool = True  # also look for USB printers that ipp-usb does not serve (CUPS usb backend, needs a driver)
    # a USB queue whose device stays unreachable this long is removed by itself (0 = never); network queues never are
    offline_remove_hours: float = Field(8.0, ge=0, allow_inf_nan=False)

    # DNS-SD announcement (python -m spoolsteward.mdns, container with host networking) so phones/computers find the printers
    mdns_hostname: str = ""  # name in the TLS certificate; none = host part of portal_url, else this machine's name
    mdns_addresses: str = ""  # comma separated IPv4 to advertise; none = the address of the default route
    mdns_port: int = Field(0, ge=0, le=65535)  # 0 = the gateway port
    mdns_interval: float = Field(30.0, gt=0, allow_inf_nan=False)  # seconds between looks at the CUPS queue list

    # backups: administrators pick a sub-folder of backup_root (the only place the gateway may write backups);
    # the "general" backup below is the one set in .env, the administrators' own are made in the portal
    backup_root: str = Field("/backups", min_length=1)
    backup_host_path: str = ""  # shown in the portal only: where backup_root is on the host
    backup_config_dir: str = Field("/config", min_length=1)  # read-only copy of config/ (TLS); saved only in encrypted backups
    backup_env_file: str = Field("/project.env", min_length=1)  # read-only copy of .env (settings, secrets); saved only in encrypted backups
    backup_enabled: bool = False
    backup_dir: str = Field("general", pattern=BACKUP_FOLDER_RE.pattern)
    backup_hour: int = Field(3, ge=0, le=23)
    backup_require_marker: bool = False  # general backup: write only if the folder holds the marker file
    backup_keep_daily: int = Field(7, ge=1)
    backup_keep_weekly: int = Field(4, ge=0)
    backup_keep_monthly: int = Field(12, ge=0)
    backup_recipient: str = ""  # from `spoolsteward-admin backup-key`; none = no encryption (and no secrets in the file)

    # ---- the rules that apply to every setting -------------------------------------------------------------------
    @field_validator("*", mode="before")
    @classmethod
    def _spelling(cls, v, info):
        """Empty is an error, `none` is for the optional ones only, booleans are true/false, integers are plain digits."""
        if not isinstance(v, str):
            return v                                  # a Python value (tests, code): pydantic checks its type
        name, text = info.field_name, v.strip()
        field = cls.model_fields[name]
        if text == "":
            raise ValueError(f"is empty: write {NONE_WORD} to leave it unset" if name in OPTIONAL else "must not be empty")
        if text.lower() == NONE_WORD:
            if name not in OPTIONAL:
                raise ValueError(f"{NONE_WORD} is not accepted here, it needs a real value")
            return field.default
        if field.annotation is bool:
            if text not in ("true", "false"):
                raise ValueError("must be true or false (lower case)")
            return text == "true"
        if field.annotation is int and not re.fullmatch(r"-?[0-9]+", text):
            raise ValueError("must be a whole number")
        return v

    # ---- rules that need more than a range or a list --------------------------------------------------------------
    @field_validator("secret_key")
    @classmethod
    def _secret_key(cls, v: str | None) -> str | None:
        # docker/env.example copied by hand: the placeholder is public, a key everybody knows protects nothing
        if v and re.fullmatch(r"__[A-Z_]+__", v.strip()):
            raise ValueError("still holds the placeholder of docker/env.example: run scripts/docker-init.sh, "
                             "or write a long random value (openssl rand -hex 32)")
        # a short key can be guessed: 64 is what `openssl rand -hex 32` prints (none = a random key at every start)
        if v is not None and len(v) < SECRET_KEY_MIN:
            raise ValueError(f"is shorter than {SECRET_KEY_MIN} characters: write a random value of at least "
                             f"{SECRET_KEY_MIN} (the output of openssl rand -hex 32)")
        return v

    @field_validator("database_url")
    @classmethod
    def _database_url(cls, v: str) -> str:
        try:
            url = make_url(v)
        except Exception:
            raise ValueError("is not a database address, for example sqlite:////data/spoolsteward.db") from None
        if url.get_backend_name() != "sqlite":
            raise ValueError("must be an SQLite address like sqlite:////data/spoolsteward.db: SpoolSteward works only with SQLite")
        return v

    @field_validator("cups_url")
    @classmethod
    def _cups_url(cls, v: str) -> str:
        if not v.startswith(("unix:///", "http://", "https://")):
            raise ValueError("must start with unix:/// (a socket), http:// or https://")
        return v

    @field_validator("portal_url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return v

    @field_validator("update_repository")
    @classmethod
    def _repository(cls, v: str) -> str:
        if not REPOSITORY_RE.fullmatch(v) or v.split("/")[1] in (".", ".."):
            raise ValueError("must be a GitHub repository written owner/name, for example deh-aleh/SpoolSteward")
        return v

    @field_validator("mdns_hostname")
    @classmethod
    def _mdns_hostname(cls, v: str) -> str:
        if v and not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", v):
            raise ValueError("must be a host name: letters, digits, dots and dashes")
        return v

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("is not a time zone name, for example Europe/Rome or UTC") from None
        return v

    @field_validator("scan_url")
    @classmethod
    def _scan_url(cls, v: str) -> str:
        for item in (v.split(",") if v else []):
            if not re.fullmatch(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*=)?https?://\S+\s*", item):
                raise ValueError("must be a comma-separated list of http(s)://address or name=http(s)://address")
        return v

    @field_validator("healthz_info")
    @classmethod
    def _healthz_info(cls, v: str) -> str:
        bad = [i for i in (w.strip() for w in v.split(",")) if v and i not in HEALTHZ_ITEMS]
        if bad:
            raise ValueError(f"unknown item {bad[0]!r}: use a comma-separated list of {', '.join(HEALTHZ_ITEMS)}")
        return v

    @field_validator("usb_bridge_ports")
    @classmethod
    def _usb_ports(cls, v: str) -> str:
        m = re.fullmatch(r"\s*([0-9]{1,5})\s*-\s*([0-9]{1,5})\s*", v)
        if not m or not 1 <= int(m[1]) <= int(m[2]) <= 65535:
            raise ValueError("must be a range of ports like 60000-60099 (first not above last, 1 to 65535)")
        return v

    @field_validator("mdns_addresses")
    @classmethod
    def _mdns_addresses(cls, v: str) -> str:
        for a in (v.split(",") if v else []):
            try:
                ipaddress.IPv4Address(a.strip())
            except ValueError:
                raise ValueError("must be IPv4 addresses separated by commas") from None
        return v

    @model_validator(mode="after")
    def _together(self):
        if bool(self.radius_host) != bool(self.radius_secret):
            raise ValueError("SPOOLSTEWARD_RADIUS_HOST and SPOOLSTEWARD_RADIUS_SECRET: set both, or write none in both (local accounts only)")
        if self.escl_guest_username == self.bootstrap_admin_username:
            raise ValueError("SPOOLSTEWARD_ESCL_GUEST_USERNAME must not be the name of the emergency administrator "
                             "(SPOOLSTEWARD_BOOTSTRAP_ADMIN_USERNAME)")
        return self

    @property
    def radius_enabled(self) -> bool:
        return bool(self.radius_host and self.radius_secret)


def problems(exc: ValidationError) -> list[str]:
    """One readable line per wrong setting, with the environment variable name; secrets are never printed."""
    lines = []
    for e in exc.errors():
        msg = e["msg"].removeprefix("Value error, ")
        if not e["loc"]:                                   # a rule between settings: its message names them
            lines.append(f"  {msg}")
            continue
        name = str(e["loc"][0])
        shown = "" if name in SECRETS or e["type"] == "missing" else f" (found: {str(e.get('input'))[:60]!r})"
        lines.append(f"  SPOOLSTEWARD_{name.upper()}: {msg}{shown}")
    return lines


def load_settings() -> Settings:
    """The settings, or an exit with every wrong one listed (code 2), instead of a Python traceback."""
    try:
        return Settings()
    except ValidationError as exc:
        print("refusing to start: wrong settings (a setting is never empty: write none to leave an optional one unset)",
              file=sys.stderr)
        print("\n".join(problems(exc)), file=sys.stderr)
        raise SystemExit(2) from None


settings = load_settings()
