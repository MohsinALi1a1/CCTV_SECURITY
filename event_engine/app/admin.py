"""Privacy commands, run inside the container:

  python -m app.admin delete-person "Ahmed"   delete all Smart Tech events + snapshots for one person
  python -m app.admin cleanup                  apply retention_days now
  python -m app.admin delete-all               delete ALL Smart Tech events + snapshots
"""
import os
import sys
import time

from .config import Rules, Settings
from .database import Database


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("delete-person", "cleanup", "delete-all"):
        print(__doc__)
        return 1
    settings = Settings()
    db = Database(settings.data_dir / "events.db")
    snapshot_dir = settings.data_dir / "snapshots"

    if argv[0] == "delete-person":
        if len(argv) < 2:
            print("Give the person's name, e.g. delete-person \"Ahmed\"")
            return 1
        removed = db.delete_person(argv[1])
    elif argv[0] == "cleanup":
        days = Rules(settings.rules_file)["retention_days"]
        removed = db.delete_older_than(time.time() - days * 86400)
    else:
        removed = db.delete_all()

    for path in removed:
        (snapshot_dir / os.path.basename(path)).unlink(missing_ok=True)
    if argv[0] == "delete-all":
        for file in snapshot_dir.glob("*.jpg"):
            file.unlink(missing_ok=True)
    print(f"Done. Deleted the matching events and {len(removed)} snapshot file(s).")

    if argv[0] in ("delete-person", "delete-all"):
        from .cloud import CloudSync
        cloud = CloudSync(settings, Rules(settings.rules_file), snapshot_dir, str)
        if cloud.enabled:
            try:
                cloud._delete_where("person", argv[1]) if argv[0] == "delete-person" else cloud._delete_where(None, None)
                print("Also deleted from Firebase (mobile app).")
            except Exception as exc:
                print(f"Could not delete from Firebase: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
