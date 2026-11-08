"""Demo data for the freeze video: daily order exports that keep arriving.

    python3 demo/demo_data.py setup ~/freeze-demo      # 5 days of orders in ~/freeze-demo/data
    python3 demo/demo_data.py next-day ~/freeze-demo   # one more day arrives
    python3 demo/demo_data.py break ~/freeze-demo      # the export lands somewhere else
    python3 demo/demo_data.py fix ~/freeze-demo        # undo "break"

The same seed always gives the same files, so takes can be re-recorded.
"""
from __future__ import annotations

import datetime as dt
import random
import sys
from pathlib import Path

START = dt.date(2026, 11, 2)
# Refunds per day: a clear leader in the first five days, then a spike on the day that "arrives".
REFUNDS = [2, 4, 1, 3, 2, 7, 1, 2]
OTHER = ["paid"] * 14 + ["pending"] * 3 + ["failed"]


def write_day(data: Path, day: dt.date) -> Path:
    rng = random.Random(day.toordinal())
    offset = (day - START).days
    refunds = REFUNDS[offset] if 0 <= offset < len(REFUNDS) else rng.randint(1, 4)
    total = rng.randint(28, 40)
    statuses = ["refunded"] * refunds + [rng.choice(OTHER) for _ in range(total - refunds)]
    rng.shuffle(statuses)
    path = data / f"orders_{day.isoformat()}.csv"
    rows = ["order_id,customer,amount,status"]
    for i, status in enumerate(statuses):
        rows.append(f"{day:%m%d}-{i:03d},c{rng.randint(100, 999)},{rng.randint(5, 300)}.{rng.randint(0, 99):02d},"
                    f"{status}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def main(cmd: str, root: str) -> None:
    root_p = Path(root).expanduser()
    data = root_p / "data"
    if cmd == "setup":
        data.mkdir(parents=True, exist_ok=True)
        for old in data.glob("orders_*.csv"):
            old.unlink()
        for n in range(5):
            write_day(data, START + dt.timedelta(days=n))
        (root_p / "refunds.md").unlink(missing_ok=True)
    elif cmd == "next-day":
        days = sorted(data.glob("orders_*.csv"))
        last = dt.date.fromisoformat(days[-1].stem.split("_")[1]) if days else START
        print("new file:", write_day(data, last + dt.timedelta(days=1)).name)
    elif cmd == "break":
        data.rename(root_p / "data-moved")
        print("moved ./data to ./data-moved")
    elif cmd == "fix":
        (root_p / "data-moved").rename(data)
        print("moved ./data-moved back to ./data")
    else:
        sys.exit(__doc__)
    for p in sorted(data.glob("orders_*.csv")) if data.exists() else []:
        n = sum(1 for ln in p.read_text().splitlines() if ln.endswith(",refunded"))
        print(f"  {p.name}: {n} refunded")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
