"""How one audit row is shown in /admin/jobs: shortened texts with a way to read them in full, the device the
action happened on, and the specifications of a print."""

from spoolsteward.db.models import JobKind, JobLog
from spoolsteward.i18n import N_, _

FILE_MAX = 36     # characters of a file name / document name shown in the table
COMMENT_MAX = 30  # characters of the comment shown in the table
NOTE_MAX = 40     # characters of the note (denial reason...) shown in the table

COLOR = {"color": N_("colour"), "monochrome": N_("black and white"), "bi-level": N_("black and white"),
         "process-monochrome": N_("black and white"), "auto": N_("automatic"), "auto-monochrome": N_("automatic (black and white)")}
SIDES = {"one-sided": N_("normal"), "two-sided-long-edge": N_("double-sided (long edge)"), "two-sided-short-edge": N_("double-sided (short edge)")}
VIA = {"web": N_("web portal"), "escl": N_("eSCL client")}
ADJUSTED = {"color": N_("colour"), "one_sided": N_("one-sided")}  # what the permissions corrected in a job
ORIENTATION = {"portrait": N_("portrait"), "landscape": N_("landscape")}


def _word(table: dict, value) -> str:
    """The translated word for a stored value; a value we do not know is shown as it is."""
    return _(table[value]) if value in table else str(value)


def clip(text: str | None, limit: int) -> tuple[str, bool]:
    """(text to show, whether something was cut)."""
    text = text or ""
    return (text, False) if len(text) <= limit else (text[:limit].rstrip(), True)


def print_specs(opts: dict) -> list[tuple[str, str]]:
    """Specifications of a print. Only what the client asked for is known: the rest is the printer's default."""
    color, sides = opts.get("color"), opts.get("sides")
    return [(_("Colour"), _word(COLOR, color) if color else _("printer default")),
            (_("Sides"), _word(SIDES, sides) if sides else _("normal"))]


def file_details(j: JobLog) -> list[tuple[str, str]]:
    o = j.options_json or {}
    rows = [(_("File name"), j.title or "—")]
    if o.get("document_name") and o["document_name"] != j.title:
        rows.append((_("Document name"), str(o["document_name"])))
    if o.get("comment"):
        rows.append((_("Comment"), str(o["comment"])))
    if o.get("document_format"):
        rows.append((_("Format"), str(o["document_format"])))
    if o.get("copies"):
        rows.append((_("Copies"), str(o["copies"])))
    if o.get("media"):
        rows.append((_("Paper"), str(o["media"])))
    if o.get("orientation"):
        rows.append((_("Orientation"), _word(ORIENTATION, o["orientation"])))
    if o.get("page_ranges"):
        rows.append((_("Pages chosen"), str(o["page_ranges"])))
    if o.get("pages_counted"):
        rows.append((_("Pages counted"), str(o["pages_counted"])))
    if o.get("adjusted"):
        rows.append((_("Corrected by the permissions"), ", ".join(_word(ADJUSTED, c) for c in o["adjusted"])))
    rows.append((_("Sent from"), _word(VIA, o.get("via")) if o.get("via") in VIA else _("IPP client")))
    return rows


def audit_row(j: JobLog, name: str | None, scanner: str | None) -> dict:
    """Everything the template needs for one row. `scanner` = print queue on the scanner's device, if known."""
    o = j.options_json or {}
    scan = j.kind is JobKind.scan
    title, title_cut = clip(j.title, FILE_MAX)
    doc = o.get("document_name") if o.get("document_name") != j.title else None
    doc, doc_cut = clip(doc, FILE_MAX)
    comment, comment_cut = clip(o.get("comment"), COMMENT_MAX)
    note, note_cut = clip(j.denied_reason, NOTE_MAX)
    return {
        "j": j, "name": name, "scan": scan,
        # the audit stores the literal "scanner" for scans: show the device it belongs to instead
        "device": (scanner or j.printer or "—") if scan else (j.printer or "—"),
        "adf": scan and o.get("source") == "adf",
        "specs": [] if scan else print_specs(o),
        "title": title, "doc": doc, "comment": comment,
        "title_cut": title_cut, "doc_cut": doc_cut, "comment_cut": comment_cut,
        "file_cut": title_cut or doc_cut or comment_cut, "details": file_details(j),
        "note": note, "note_cut": note_cut,
        # scan sent without credentials: the only thing known about the sender is where it came from
        "guest": {"ip": o["guest_ip"], "host": o.get("guest_host") or ""} if o.get("guest_ip") else None,
        # scan through a scan window (the person pressed the button): where it came from; or refused because it came from
        # another address than the window's
        "win": ({"expected": o["window_ip"], "got": o["got_ip"]} if o.get("got_ip") and o.get("window_ip") else
                {"ip": o["window_ip"], "host": o.get("window_host") or ""} if o.get("window_ip") else None),
    }
