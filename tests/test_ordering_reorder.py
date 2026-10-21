"""`reorder` against a real SQLite database: stale rows go, existing rows are updated, new queues are added."""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from spoolsteward.db.models import Base, PrinterOrder, User
from spoolsteward.policy.ordering import reorder


def _sm(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'order.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _rows(sm, uid):
    with sm() as db:
        return {r.printer: r.position for r in db.scalars(select(PrinterOrder).where(PrinterOrder.user_id == uid))}


def test_reorder_drops_gone_queues_updates_and_adds(tmp_path):
    sm = _sm(tmp_path)
    with sm() as db:
        a = User(username="a", provider="local")
        b = User(username="b", provider="local")
        db.add_all([a, b])
        db.commit()
        ua, ub = a.id, b.id
        db.add_all([
            PrinterOrder(user_id=ua, printer="gone1", position=0),
            PrinterOrder(user_id=ua, printer="kept", position=5),
            PrinterOrder(user_id=ua, printer="gone2", position=1),
            PrinterOrder(user_id=ub, printer="gone1", position=7),
        ])
        db.commit()
    # cards: [kept], [new, newalias], [other]; swap cards 0 and 2
    reorder(sm, ua, [["kept"], ["New", "newalias"], ["other"]], 0, 2)
    assert _rows(sm, ua) == {"kept": 2, "new": 1, "newalias": 1, "other": 0}
    assert _rows(sm, ub) == {"gone1": 7}  # nobody else's order changes


def test_reorder_with_no_rows_yet(tmp_path):
    sm = _sm(tmp_path)
    with sm() as db:
        u = User(username="a", provider="local")
        db.add(u)
        db.commit()
        uid = u.id
    reorder(sm, uid, [["x"], ["y"]], 0, 1)
    assert _rows(sm, uid) == {"x": 1, "y": 0}
