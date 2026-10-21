"""The order printers are listed in, in the portal and in the print form: physical printers first, then virtual
(test) ones; inside each group the one connected first comes first. Each administrator can place printers in their own list
(up/down buttons): see `arrange`.

CUPS does not say when a queue was created, so the gateway notes the first time it sees each queue name. Queues
that already existed when this was introduced share the same moment and fall back to alphabetical order among
themselves; everything connected afterwards is ordered by connection time."""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from spoolsteward.timeutil import aware
from spoolsteward.db.models import PrinterControl, PrinterKeepalive, PrinterOffline, PrinterOrder, PrinterSeen

VIRTUAL_SCHEMES = ("cups-pdf", "file")  # queues that write a file instead of printing: no hardware behind them


def key(name: str) -> str:
    return name.strip().lower()


def is_virtual(device_uri: str) -> bool:
    return device_uri.split(":", 1)[0].lower() in VIRTUAL_SCHEMES


def note_seen(sm, names: list[str]) -> dict[str, datetime]:
    """Remember the first sighting of every name in `names` (blocking) and return {name key: first sighting}."""
    wanted = {key(n) for n in names}
    if not wanted:
        return {}
    with sm() as db:
        known = {r.printer: r.first_seen for r in db.scalars(select(PrinterSeen).where(PrinterSeen.printer.in_(wanted)))}
        missing = wanted - known.keys()
        if missing:
            now = datetime.now(UTC)
            db.add_all(PrinterSeen(printer=n, first_seen=now) for n in sorted(missing))
            try:
                db.commit()
            except IntegrityError:  # another request noted the same queue a moment ago
                db.rollback()
            known = {r.printer: r.first_seen for r in db.scalars(select(PrinterSeen).where(PrinterSeen.printer.in_(wanted)))}
    return {n: aware(t) for n, t in known.items()}


def positions(sm, user_id: int) -> dict[str, int]:
    """The places this administrator gave with the up/down arrows (blocking): {name key: position}."""
    with sm() as db:
        return {r.printer: r.position for r in db.scalars(select(PrinterOrder).where(PrinterOrder.user_id == user_id))}


def arrange(items: list, pos: dict[str, int], name=lambda x: x["name"], uri=lambda x: x.get("device_uri", "")) -> list:
    """`items` (already in the default order) as one person wants them: physical first, then their own places, then the
    default order for whatever they never moved. Stable."""
    ranked = sorted(enumerate(items), key=lambda t: (is_virtual(uri(t[1])), pos.get(key(name(t[1])), 1 << 30), t[0]))
    return [x for _, x in ranked]


def reorder(sm, user_id: int, cards: list[list[str]], i: int, j: int) -> None:
    """`cards` = the queue names of each printer card in the order this person sees. Gives every card its current place for
    them (their order is explicit from now on) and swaps cards i and j. Nobody else's order changes."""
    order = list(range(len(cards)))
    order[i], order[j] = order[j], order[i]
    where = {n: p for p, c in zip(order, cards) for n in map(key, c)}
    with sm() as db:
        have = {r.printer: r for r in db.scalars(select(PrinterOrder).where(PrinterOrder.user_id == user_id))}
        for name, row in list(have.items()):
            if name not in where:
                db.delete(row)  # a queue that is gone
                del have[name]
        for n, p in where.items():
            if n in have:
                have[n].position = p
            else:
                db.add(PrinterOrder(user_id=user_id, printer=n, position=p))
        db.commit()


def forget(db, name: str) -> None:
    """A deleted queue loses its place: a new queue with the same name starts at the end."""
    row = db.get(PrinterSeen, key(name))
    if row is not None:
        db.delete(row)
    for r in db.scalars(select(PrinterOrder).where(PrinterOrder.printer == key(name))):
        db.delete(r)


def rename(db, old: str, new: str) -> None:
    """Everything the gateway keeps under a queue name follows the queue to its new name (its history in the audit
    keeps the name it had when it happened)."""
    o, n = key(old), key(new)
    for model in (PrinterSeen, PrinterControl, PrinterKeepalive, PrinterOffline):
        db.execute(update(model).where(model.printer == o).values(printer=n))
    db.execute(update(PrinterOrder).where(PrinterOrder.printer == o).values(printer=n))


def sort_key(name: str, device_uri: str, seen: dict[str, datetime]):
    far = datetime.max.replace(tzinfo=UTC)
    return (is_virtual(device_uri), seen.get(key(name), far), key(name))
