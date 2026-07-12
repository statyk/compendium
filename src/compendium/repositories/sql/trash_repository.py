from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from compendium.domain.enums import FineStatus
from compendium.domain.models import (
    Creator,
    CuratedList,
    CuratedListEntry,
    DeletedEntity,
    Fine,
    Hold,
    Item,
    ItemNote,
    Loan,
    Notification,
    ScanEvent,
    ScanPendingItem,
    Work,
    WorkCreator,
)

PAYLOAD_VERSION = 1


def _row_dict(obj) -> dict:
    """All scalar columns of an ORM row, JSON-safe (datetimes → ISO strings)."""
    out: dict = {}
    for attr in sa_inspect(obj).mapper.column_attrs:
        value = getattr(obj, attr.key)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        out[attr.key] = value
    return out


def _build_kwargs(model_cls, data: dict) -> dict:
    """Payload dict → constructor kwargs: drop 'id', drop unknown/removed
    columns, coerce ISO strings back to datetime/date per column type."""
    out: dict = {}
    for attr in sa_inspect(model_cls).column_attrs:
        key = attr.key
        if key == "id" or key not in data:
            continue
        value = data[key]
        if isinstance(value, str):
            col_type = attr.columns[0].type
            try:
                py = col_type.python_type
            except NotImplementedError:
                # TypeDecorators (e.g. UtcDateTime) don't proxy python_type;
                # fall back to the wrapped impl's type.
                impl = getattr(col_type, "impl", None)
                py = getattr(impl, "python_type", None) if impl is not None else None
            if py is datetime:
                value = datetime.fromisoformat(value)
            elif py is date:
                value = date.fromisoformat(value)
        out[key] = value
    return out


class SqlTrashRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    # -- trash-row CRUD -------------------------------------------------

    def add(self, entity: DeletedEntity) -> DeletedEntity:
        self._s.add(entity)
        self._s.flush()
        return entity

    def get(self, trash_id: int) -> DeletedEntity | None:
        return self._s.get(DeletedEntity, trash_id)

    def list(self, *, entity_type: str, limit: int = 50) -> list[DeletedEntity]:
        return (
            self._s.query(DeletedEntity)
            .filter(DeletedEntity.entity_type == entity_type)
            .order_by(DeletedEntity.deleted_at.desc(), DeletedEntity.id.desc())
            .limit(limit)
            .all()
        )

    def delete(self, entity: DeletedEntity) -> None:
        self._s.delete(entity)
        self._s.flush()

    def delete_older_than(self, entity_type: str, cutoff: datetime) -> int:
        # synchronize_session="fetch": a plain "False" leaves stale rows in
        # the session identity map, so a subsequent get() on a just-deleted
        # id would incorrectly return the cached (deleted) instance.
        n = (
            self._s.query(DeletedEntity)
            .filter(
                DeletedEntity.entity_type == entity_type,
                DeletedEntity.deleted_at < cutoff,
            )
            .delete(synchronize_session="fetch")
        )
        self._s.flush()
        return n

    # -- deletability blockers ------------------------------------------

    def count_active_loans(self, work_id: int) -> int:
        return (
            self._s.query(Loan)
            .join(Item, Loan.item_id == Item.id)
            .filter(Item.work_id == work_id, Loan.returned_at.is_(None))
            .count()
        )

    def count_outstanding_fines(self, work_id: int) -> int:
        item_ids = self._item_ids(work_id)
        if not item_ids:
            return 0
        loan_ids = [
            r[0]
            for r in self._s.query(Loan.id).filter(Loan.item_id.in_(item_ids)).all()
        ]
        q = self._s.query(Fine).filter(Fine.status == FineStatus.OUTSTANDING.value)
        clause = Fine.item_id.in_(item_ids)
        if loan_ids:
            clause = clause | Fine.loan_id.in_(loan_ids)
        return q.filter(clause).count()

    def _item_ids(self, work_id: int) -> list[int]:
        return [
            r[0]
            for r in self._s.query(Item.id).filter(Item.work_id == work_id).all()
        ]

    # -- snapshot / graph delete ----------------------------------------

    def snapshot_work_graph(self, work: Work) -> dict:
        s = self._s
        items = s.query(Item).filter(Item.work_id == work.id).order_by(Item.id).all()
        item_ids = [i.id for i in items]
        loans = (
            s.query(Loan).filter(Loan.item_id.in_(item_ids)).order_by(Loan.id).all()
            if item_ids
            else []
        )
        holds = s.query(Hold).filter(Hold.work_id == work.id).order_by(Hold.id).all()
        notes = (
            s.query(ItemNote)
            .filter(ItemNote.item_id.in_(item_ids))
            .order_by(ItemNote.id)
            .all()
            if item_ids
            else []
        )
        entries = (
            s.query(CuratedListEntry, CuratedList.slug)
            .join(CuratedList, CuratedListEntry.list_id == CuratedList.id)
            .filter(CuratedListEntry.work_id == work.id)
            .order_by(CuratedListEntry.list_id)
            .all()
        )
        return {
            "version": PAYLOAD_VERSION,
            "work": _row_dict(work),
            "creators": [
                {
                    "display_name": wc.creator.display_name,
                    "sort_name": wc.creator.sort_name,
                    "role": wc.role,
                    "display_order": wc.display_order,
                }
                for wc in work.creators
            ],
            "items": [_row_dict(i) for i in items],
            "loans": [_row_dict(loan) for loan in loans],
            "holds": [_row_dict(h) for h in holds],
            "item_notes": [_row_dict(n) for n in notes],
            "curated_lists": [
                {"slug": slug, "annotation": e.annotation, "display_order": e.display_order}
                for e, slug in entries
            ],
        }

    def delete_work_graph(self, work: Work) -> None:
        s = self._s
        work_id = work.id
        item_ids = self._item_ids(work_id)
        loan_ids = (
            [r[0] for r in s.query(Loan.id).filter(Loan.item_id.in_(item_ids)).all()]
            if item_ids
            else []
        )
        hold_ids = [r[0] for r in s.query(Hold.id).filter(Hold.work_id == work_id).all()]

        # Nullable FKs on surviving rows → SET NULL (not relinked on restore).
        if loan_ids:
            s.query(Fine).filter(Fine.loan_id.in_(loan_ids)).update(
                {"loan_id": None}, synchronize_session=False
            )
            s.query(Notification).filter(Notification.loan_id.in_(loan_ids)).update(
                {"loan_id": None}, synchronize_session=False
            )
        if hold_ids:
            s.query(Notification).filter(Notification.hold_id.in_(hold_ids)).update(
                {"hold_id": None}, synchronize_session=False
            )
        if item_ids:
            s.query(Fine).filter(Fine.item_id.in_(item_ids)).update(
                {"item_id": None}, synchronize_session=False
            )
            s.query(ScanEvent).filter(ScanEvent.item_id.in_(item_ids)).update(
                {"item_id": None}, synchronize_session=False
            )
            s.query(ScanPendingItem).filter(
                ScanPendingItem.created_item_id.in_(item_ids)
            ).update({"created_item_id": None}, synchronize_session=False)

        # Children, FK-safe order.
        if item_ids:
            s.query(ItemNote).filter(ItemNote.item_id.in_(item_ids)).delete(
                synchronize_session=False
            )
            s.query(Loan).filter(Loan.item_id.in_(item_ids)).delete(
                synchronize_session=False
            )
        s.query(Hold).filter(Hold.work_id == work_id).delete(synchronize_session=False)
        if item_ids:
            s.query(Item).filter(Item.id.in_(item_ids)).delete(synchronize_session=False)
        s.query(CuratedListEntry).filter(CuratedListEntry.work_id == work_id).delete(
            synchronize_session=False
        )
        s.query(WorkCreator).filter(WorkCreator.work_id == work_id).delete(
            synchronize_session=False
        )
        s.flush()
        # Bulk deletes bypass the identity map, so the loaded Work still holds a
        # stale creators collection (cascade="all, delete-orphan"). Expiring
        # everything forces the ORM to re-read empty child collections before the
        # ORM-level Work delete, which fires the FTS delete trigger.
        s.expire_all()
        target = s.get(Work, work_id)
        if target is not None:
            s.delete(target)
        s.flush()

    # -- restore --------------------------------------------------------

    def find_restore_collisions(self, payload: dict) -> list[str]:
        """Live-catalog uniqueness clashes that would block re-inserting the
        snapshot. Checked before any write so restore is all-or-nothing."""
        s = self._s
        conflicts: list[str] = []
        w = payload["work"]
        if w.get("isbn") and s.query(Work.id).filter(Work.isbn == w["isbn"]).first():
            conflicts.append(f"ISBN {w['isbn']}")
        if w.get("upc") and s.query(Work.id).filter(Work.upc == w["upc"]).first():
            conflicts.append(f"UPC {w['upc']}")
        barcodes = [i["barcode"] for i in payload["items"]]
        if barcodes:
            for (bc,) in s.query(Item.barcode).filter(Item.barcode.in_(barcodes)):
                conflicts.append(f"barcode {bc}")
        accessions = [i["accession_number"] for i in payload["items"]]
        if accessions:
            for (acc,) in s.query(Item.accession_number).filter(
                Item.accession_number.in_(accessions)
            ):
                conflicts.append(f"accession number {acc}")
        return conflicts

    def restore_work_graph(self, payload: dict) -> Work:
        """Re-insert the snapshotted graph under fresh primary keys, remapping
        every internal FK to the new ids. Caller guarantees no collisions."""
        s = self._s
        work = Work(**_build_kwargs(Work, payload["work"]))
        s.add(work)
        s.flush()

        for c in payload["creators"]:
            creator = s.query(Creator).filter_by(sort_name=c["sort_name"]).first()
            if creator is None:
                creator = Creator(
                    display_name=c["display_name"], sort_name=c["sort_name"]
                )
                s.add(creator)
                s.flush()
            s.add(
                WorkCreator(
                    work_id=work.id,
                    creator_id=creator.id,
                    role=c["role"],
                    display_order=c["display_order"],
                )
            )

        item_id_map: dict[int, int] = {}
        for row in payload["items"]:
            item = Item(**_build_kwargs(Item, row))
            item.work_id = work.id
            s.add(item)
            s.flush()
            item_id_map[row["id"]] = item.id

        for row in payload["loans"]:
            loan = Loan(**_build_kwargs(Loan, row))
            loan.item_id = item_id_map[row["item_id"]]
            s.add(loan)

        for row in payload["holds"]:
            hold = Hold(**_build_kwargs(Hold, row))
            hold.work_id = work.id
            hold.held_item_id = item_id_map.get(row.get("held_item_id"))
            s.add(hold)

        for row in payload["item_notes"]:
            note = ItemNote(**_build_kwargs(ItemNote, row))
            note.item_id = item_id_map[row["item_id"]]
            s.add(note)

        for entry in payload["curated_lists"]:
            cl = s.query(CuratedList).filter_by(slug=entry["slug"]).first()
            if cl is not None:
                s.add(
                    CuratedListEntry(
                        list_id=cl.id,
                        work_id=work.id,
                        display_order=entry["display_order"],
                        annotation=entry["annotation"],
                    )
                )

        s.flush()
        return work
