import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.timeutil import now_utc
from app.crypto_fields import SealedOptions, SealedText


class Base(DeclarativeBase):
    pass


class SubjectType(str, enum.Enum):
    user = "user"
    group = "group"


class Resource(str, enum.Enum):
    print = "print"
    scan = "scan"


class Metric(str, enum.Enum):
    jobs = "jobs"
    pages = "pages"


class Period(str, enum.Enum):
    weekly = "weekly"
    monthly = "monthly"


class JobKind(str, enum.Enum):
    print = "print"
    scan = "scan"


class JobState(str, enum.Enum):
    accepted = "accepted"
    denied = "denied"
    completed = "completed"
    failed = "failed"  # CUPS refused it
    canceled = "canceled"  # canceled before anything was printed
    held = "held"


class RequestKind(str, enum.Enum):
    quota = "quota"
    group_change = "group_change"
    permission = "permission"  # colour, one-sided, a printer or the hours: becomes a PermissionGrant


class RequestScope(str, enum.Enum):
    temporary = "temporary"  # until the end of the period of the rule that limits the user
    permanent = "permanent"  # in every following period, until revoked


class RequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"
    canceled = "canceled"


class Group(Base):
    __tablename__ = "group"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # users may ask to be moved into this group (see quota_request.kind=group_change)
    requestable: Mapped[bool] = mapped_column(default=False, server_default=false())

    users: Mapped[list["User"]] = relationship(back_populates="group")


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "local" = password stored here; anything else = that provider (e.g. "radius") decides
    provider: Mapped[str] = mapped_column(String(32), default="radius")
    username: Mapped[str] = mapped_column(String(128))
    is_admin: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("group.id"))
    password_hash: Mapped[str | None] = mapped_column(String(255))  # argon2id, local accounts only
    must_change_password: Mapped[bool] = mapped_column(default=False, server_default=false())
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    theme: Mapped[str] = mapped_column(String(8), default="auto", server_default="auto")  # auto | light | dark
    language: Mapped[str | None] = mapped_column(String(8))  # None = follow the browser
    notices_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # the bell shows what is newer than this
    own_notices_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # same, for the notices addressed to this person
    # seconds between automatic reloads of the audit page; 0 = off
    refresh_seconds: Mapped[int] = mapped_column(default=30, server_default="30")
    # minutes after which "secondary" notices (see app.notices.SECONDARY_KINDS) are marked as read by themselves; 0 = never
    secondary_read_minutes: Mapped[int] = mapped_column(default=20, server_default="20")

    group: Mapped[Group | None] = relationship(back_populates="users")

    # One name = one person, whatever the provider: CUPS and the audit only see "gw:<name>", so two
    # providers sharing a name would share an identity. Also stops a first-login race minting two users.
    __table_args__ = (UniqueConstraint("username", name="uq_user_username"), {"sqlite_autoincrement": True})


class PolicyRule(Base):
    __tablename__ = "policy_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_type: Mapped[SubjectType] = mapped_column(Enum(SubjectType))
    subject_id: Mapped[int]
    resource: Mapped[Resource] = mapped_column(Enum(Resource), default=Resource.print)
    metric: Mapped[Metric] = mapped_column(Enum(Metric), default=Metric.jobs)
    limit: Mapped[int]
    period: Mapped[Period] = mapped_column(Enum(Period), default=Period.weekly)
    enabled: Mapped[bool] = mapped_column(default=True)

    __table_args__ = (UniqueConstraint("subject_type", "subject_id", "resource", "metric"),)


class PermissionRule(Base):
    """What a group or a user may do, besides the quotas: colour, one-sided prints, which printers, at which hours.
    One row per subject. On a group a NULL field means "allowed / all / any time"; on a user it means "as the group"."""

    __tablename__ = "permission_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_type: Mapped[SubjectType] = mapped_column(Enum(SubjectType))
    subject_id: Mapped[int]
    color: Mapped[bool | None]
    one_sided: Mapped[bool | None]  # may print one-sided; without it the job is made double-sided
    printers_mode: Mapped[str | None] = mapped_column(String(8))  # "all" | "list"
    printers: Mapped[list | None] = mapped_column(JSON)  # lower-case queue names, used when printers_mode is "list"
    hours_mode: Mapped[str | None] = mapped_column(String(8))  # "any" | "window"
    hours_days: Mapped[str | None] = mapped_column(String(7))  # weekdays as digits, 0 = Monday ("01234")
    hours_start: Mapped[int | None]  # minutes after midnight, 0-1439
    hours_end: Mapped[int | None]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (UniqueConstraint("subject_type", "subject_id"),)


class PrinterFormer(Base):
    """A queue name that was used before (deleted, removed because its USB device stayed away, or renamed). The rename
    dialog offers these names: giving a replacement printer the old name makes the old addresses and permissions work again."""

    __tablename__ = "printer_former"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)  # lower-case
    display: Mapped[str] = mapped_column(String(128))  # as it was written
    removed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    reason: Mapped[str] = mapped_column(String(16))  # "deleted" | "removed_offline" | "renamed"


class JobLog(Base):
    __tablename__ = "job_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), index=True)
    # name at the time of the job: stays readable after the account is deleted (user_id then becomes NULL)
    user_name: Mapped[str | None] = mapped_column(String(128))
    kind: Mapped[JobKind] = mapped_column(Enum(JobKind), default=JobKind.print)
    printer: Mapped[str | None] = mapped_column(String(128))
    cups_job_id: Mapped[int | None]
    title: Mapped[str | None] = mapped_column(SealedText(512))  # a file name: encrypted in the database (app/crypto_fields.py)
    pages: Mapped[int | None]
    state: Mapped[JobState] = mapped_column(Enum(JobState))
    denied_reason: Mapped[str | None] = mapped_column(String(256))
    options_json: Mapped[dict | None] = mapped_column(SealedOptions())  # "document_name" inside is encrypted

    user: Mapped[User | None] = relationship()

    # ids never restart, even after archiving empties the table: they identify rows across archive files
    __table_args__ = {"sqlite_autoincrement": True}


class PrinterControl(Base):
    """Administrator switches for one CUPS queue. Lower-case queue name is the key: CUPS ignores case."""

    __tablename__ = "printer_control"

    printer: Mapped[str] = mapped_column(String(128), primary_key=True)
    blocked: Mapped[bool] = mapped_column(default=False, server_default=false())  # nobody prints, administrators included
    mdns_hidden: Mapped[bool] = mapped_column(default=False, server_default=false())  # not announced on the network
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class PrinterSeen(Base):
    """When the gateway first saw a queue: the order printers are listed in (physical ones first, oldest first)."""

    __tablename__ = "printer_seen"

    printer: Mapped[str] = mapped_column(String(128), primary_key=True)  # lower-case queue name
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class PrinterOffline(Base):
    """Since when a USB printer's queue has been unreachable: after `offline_remove_hours` the queue is removed."""

    __tablename__ = "printer_offline"

    printer: Mapped[str] = mapped_column(String(128), primary_key=True)  # lower-case queue name
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class PrinterOrder(Base):
    """Where one administrator placed a printer in their own list (the up/down arrows). Everybody's list is their own:
    moving a printer changes nothing for the others. Printers never moved have no row and follow the default order."""

    __tablename__ = "printer_order"

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)
    printer: Mapped[str] = mapped_column(String(128), primary_key=True)  # lower-case queue name
    position: Mapped[int] = mapped_column(Integer)


class PrinterKeepalive(Base):
    """Periodic maintenance print of one physical printer (keeps rollers clean and ink from drying). One row per
    queue, shared by every administrator; the schedule is only run while `enabled`."""

    __tablename__ = "printer_keepalive"

    printer: Mapped[str] = mapped_column(String(128), primary_key=True)  # lower-case queue name
    enabled: Mapped[bool] = mapped_column(default=False, server_default=false())
    interval_hours: Mapped[int] = mapped_column(default=168, server_default="168")
    color: Mapped[bool] = mapped_column(default=False, server_default=false())
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(8))  # ok | error | offline
    last_error: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_by: Mapped[str] = mapped_column(String(128), default="")


class GatewaySetting(Base):
    """Small key/value store for gateway-wide switches (maintenance mode)."""

    __tablename__ = "gateway_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")


class PrintKey(Base):
    """Personal print password: what clients store instead of the domain password. Only its hash is kept."""

    __tablename__ = "print_key"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    label: Mapped[str] = mapped_column(String(64))
    key_prefix: Mapped[str] = mapped_column(String(16))  # shown in the portal to tell keys apart
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 of a >=128-bit random secret
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_ip: Mapped[str | None] = mapped_column(String(64))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    single_use: Mapped[bool] = mapped_column(default=False, server_default=false())  # valid for one print
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # single-use: when its print was accepted
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set by an administrator; expires_at keeps running
    expiry_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # the "about to expire" notice was sent


class QuotaRequest(Base):
    """A user asks for more quota or for a different group; an admin decides."""

    __tablename__ = "quota_request"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    kind: Mapped[RequestKind] = mapped_column(Enum(RequestKind))
    scope: Mapped[RequestScope | None] = mapped_column(Enum(RequestScope))  # quota requests only
    params_json: Mapped[dict | None] = mapped_column(JSON)  # quota: {metric, amount}; group_change: {target_group_id}; permission: {perm, printer}
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[RequestStatus] = mapped_column(Enum(RequestStatus), default=RequestStatus.pending, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(500))


class QuotaGrant(Base):
    """Extra quota on top of the user's rule. Never touches job_log: the history stays intact."""

    __tablename__ = "quota_grant"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    metric: Mapped[Metric] = mapped_column(Enum(Metric))
    amount: Mapped[int]
    scope: Mapped[RequestScope] = mapped_column(Enum(RequestScope))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # NULL = permanent
    request_id: Mapped[int | None] = mapped_column(ForeignKey("quota_request.id"))
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))


class PermissionGrant(Base):
    """A permission added on top of what the user's group and own rule give (colour, one-sided, one more printer, any hours),
    for a while or for good. Only widens: it never takes a permission away."""

    __tablename__ = "permission_grant"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    perm: Mapped[str] = mapped_column(String(16))  # color | one_sided | printer | hours
    printer: Mapped[str | None] = mapped_column(String(128))  # lower-case queue name, only for perm == "printer"
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # NULL = permanent
    request_id: Mapped[int | None] = mapped_column(ForeignKey("quota_request.id"))
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    expiry_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # the "it ended" notice was sent


class AdminAudit(Base):
    """Who did what in the portal / CLI. Append-only, like job_log."""

    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    actor_name: Mapped[str] = mapped_column(String(128))  # kept even if the actor row changes
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[int | None]
    detail_json: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = {"sqlite_autoincrement": True}


class Notice(Base):
    """Something administrators should know about (the bell in the portal). One row per kind and source; repeats of the
    same thing only raise `count` (see app.notices for the rate limit), so nobody can flood the list."""

    __tablename__ = "notice"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)  # a fixed code, translated when shown
    key: Mapped[str] = mapped_column(String(128))  # the source, for example an IP address
    user_id: Mapped[int | None] = mapped_column(index=True)  # NULL = for the administrators; else for this user only
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    count: Mapped[int] = mapped_column(default=1)
    detail_json: Mapped[dict | None] = mapped_column(JSON)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_by: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = {"sqlite_autoincrement": True}


class AuditArchive(Base):
    """Registry of compressed audit files. Rows are never updated or deleted by the application."""

    __tablename__ = "audit_archive"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_table: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(String(512))
    first_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_id: Mapped[int]
    last_id: Mapped[int]
    rows: Mapped[int]
    bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class BackupTarget(Base):
    """A scheduled backup: where it goes (a folder under the backup root), when, how many to keep, how it is encrypted.
    Each administrator owns their own; the row with `is_global` mirrors the settings of .env."""

    __tablename__ = "backup_target"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"))
    owner_name: Mapped[str] = mapped_column(String(128), default="")  # kept if the owner is deleted
    is_global: Mapped[bool] = mapped_column(default=False, server_default=false())
    name: Mapped[str] = mapped_column(String(80))
    folder: Mapped[str] = mapped_column(String(200))  # relative to the backup root
    enabled: Mapped[bool] = mapped_column(default=True)
    # write only if the folder holds the marker file (see app.backup.core.MARKER): protects against a network share that is not mounted
    require_marker: Mapped[bool] = mapped_column(default=False, server_default=false())
    hour: Mapped[int] = mapped_column(default=3)  # local hour of the daily run
    keep_daily: Mapped[int] = mapped_column(default=7)
    keep_weekly: Mapped[int] = mapped_column(default=4)
    keep_monthly: Mapped[int] = mapped_column(default=12)
    mode: Mapped[str] = mapped_column(String(16), default="none")  # none | passphrase | key
    recipient: Mapped[str | None] = mapped_column(Text)  # public + wrapped private key; never the passphrase
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(8))  # ok | error
    last_error: Mapped[str | None] = mapped_column(Text)
    last_file: Mapped[str | None] = mapped_column(String(200))
    last_size: Mapped[int | None]

    __table_args__ = {"sqlite_autoincrement": True}
