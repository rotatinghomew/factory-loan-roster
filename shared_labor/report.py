from __future__ import annotations

import argparse
import json
from pathlib import Path

from .service import Service

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "labor_20261203.json"


def build_report(at: str = "13:10", process_due: bool = True) -> dict:
    data = json.loads(DEFAULT_DATA.read_text(encoding="utf-8"))
    service = Service(data, clock=at)
    due = service.process_due_recalls(at) if process_due else None
    return {
        "as_of": at,
        "due_recall_processing": due,
        "shifts": [service.staffing(s["id"], at) for s in service.db["shifts"]],
        "recalls": [service.recall_view(r["id"], at) for r in service.db["recalls"]],
        "disputes": service.list_disputes(),
        "shortfalls": service.db["shortfalls"],
        "liabilities": service.db["liabilities"],
        "workers": {p["id"]: service.worker_record(p["id"], at=at) for p in service.db["people"]},
        "factory_boards": {fid: service.factory_board(fid, at=at) for fid in ("OR", "FB", "FC")},
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Print the reconciled seed-day operational report")
    parser.add_argument("--at", default="13:10")
    parser.add_argument("--no-process-due", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(build_report(args.at, not args.no_process_due), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
