"""Xu ly 5 vi tri: cho xe vao o, roi o va ket thuc phien."""

import time
from datetime import datetime

from .config import DISPLAY_TO_PHYSICAL, SLOT_NAMES
from .data_store import save_active, log_history, load_reservation_records
from .reservation_manager import booked_physical_slots, set_reservation_status

class ParkingLot:
    def __init__(self, active):
        self.active = active

    @staticmethod
    def free_slots(occupied):
        return [
            SLOT_NAMES[i]
            for i, is_occupied in enumerate(occupied)
            if not is_occupied
        ]

    def can_admit(self, occupied):
        # Khach walk-in khong duoc su dung o dang duoc giu dat cho.
        free = set(self.free_slots(occupied))
        held = booked_physical_slots()

        pending_walkins = sum(
            1
            for record in self.active.values()
            if isinstance(record, dict)
            and record.get("awaiting_slot")
            and record.get("parked_ts") is None
            and not record.get("reserved_slot")
        )

        return len(free - held) > pending_walkins

    def can_admit_reserved(
        self,
        occupied,
        physical_slot,
        uid,
        reservation_id,
    ):
        if physical_slot not in SLOT_NAMES:
            return False

        index = SLOT_NAMES.index(physical_slot)

        # O dat phai dang trong theo IR thuc te.
        if occupied[index]:
            return False

        # Khong cho phien khac chiem hoac giu cung o vat ly.
        for other_uid, record in self.active.items():
            if other_uid == uid:
                continue

            if (
                record.get("slot") == physical_slot
                or record.get("reserved_slot") == physical_slot
            ):
                return False

        # Kiem tra mot dat cho khac co dang giu o nay khong.
        for row in load_reservation_records():
            if not isinstance(row, dict):
                continue

            if row.get("id") == reservation_id:
                continue

            if row.get("status") not in ("BOOKED", "ARRIVED"):
                continue

            display_slot = str(row.get("slot", "")).upper()

            if DISPLAY_TO_PHYSICAL.get(display_slot) != physical_slot:
                continue

            try:
                start_ts = float(row.get("start_ts", 0))
                end_ts = float(row.get("end_ts", 0))
            except (TypeError, ValueError):
                continue

            if (
                row.get("status") == "ARRIVED"
                or start_ts <= time.time() < end_ts
            ):
                return False

        return True

    def on_slot_occupied(self, index):
        physical_slot = SLOT_NAMES[index]

        # Uu tien xe da check-in va phai vao dung o da dat.
        reserved_waiters = []

        for uid, record in self.active.items():
            if (
                record.get("awaiting_slot")
                and record.get("parked_ts") is None
                and record.get("reserved_slot") == physical_slot
            ):
                reserved_waiters.append((uid, record))

        if reserved_waiters:
            reserved_waiters.sort(
                key=lambda item: float(item[1].get("entry_ts", 0))
            )

            uid, record = reserved_waiters[0]
            now = time.time()

            record.update({
                "slot": physical_slot,
                "last_slot": physical_slot,
                "parked_ts": now,
                "start_ts": now,
                "awaiting_slot": False,
            })

            save_active(self.active)

            log_history(
                "PARKED",
                uid,
                physical_slot,
                now,
                now,
                0,
            )

            reservation_id = record.get("reservation_id")

            if reservation_id:
                set_reservation_status(
                    reservation_id,
                    "IN_USE",
                    {
                        "parked_ts": now,
                        "actual_physical_slot": physical_slot,
                    },
                )

            print(
                "Xe dat cho da do tai {}.".format(physical_slot),
                flush=True,
            )

            return

        # O nay dang duoc dat cho nguoi khac, khong gan nham phien.
        if physical_slot in booked_physical_slots():
            print(
                "Canh bao: IR {} co xe nhung o dang duoc giu dat cho.".format(
                    physical_slot
                ),
                flush=True,
            )
            return

        # Gan o cho xe khach khong dat cho.
        normal_waiters = [
            (uid, record)
            for uid, record in self.active.items()
            if (
                record.get("awaiting_slot")
                and record.get("parked_ts") is None
                and not record.get("reserved_slot")
            )
        ]

        if normal_waiters:
            normal_waiters.sort(
                key=lambda item: float(item[1].get("entry_ts", 0))
            )

            uid, record = normal_waiters[0]
            now = time.time()

            record.update({
                "slot": physical_slot,
                "last_slot": physical_slot,
                "parked_ts": now,
                "start_ts": now,
                "awaiting_slot": False,
            })

            save_active(self.active)

            log_history(
                "PARKED",
                uid,
                physical_slot,
                now,
                now,
                0,
            )

            print(
                "UID {} bat dau do tai {}.".format(
                    uid, physical_slot
                ),
                flush=True,
            )

            return

        # Xe da co phien va dang chuyen tu o nay sang o khac.
        for uid, record in self.active.items():
            if (
                record.get("parked_ts") is not None
                and record.get("slot") is None
                and not record.get("awaiting_slot")
            ):
                record["slot"] = physical_slot
                record["last_slot"] = physical_slot

                save_active(self.active)

                print(
                    "UID {} chuyen sang {}.".format(
                        uid, physical_slot
                    ),
                    flush=True,
                )

                return

        print(
            "IR phat hien xe tai {} nhung khong co phien dang cho.".format(
                physical_slot
            ),
            flush=True,
        )

    def on_slot_empty(self, index):
        name = SLOT_NAMES[index]
        changed = False

        for record in self.active.values():
            if record.get("slot") == name:
                record["last_slot"] = name
                record["slot"] = None
                changed = True

        if changed:
            save_active(self.active)

    def finish_exit(self, uid):
        record = self.active.pop(uid, None)

        if record is None:
            return None

        save_active(self.active)

        reservation_id = record.get("reservation_id")

        if reservation_id:
            set_reservation_status(
                reservation_id,
                "COMPLETED",
                {
                    "completed_at": datetime.now().astimezone().isoformat(
                        timespec="seconds"
                    ),
                },
            )

        return record
