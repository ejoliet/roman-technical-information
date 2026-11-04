#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Export ipac_params as RomanParameters.csv (rows whose IPAC category is empty are left out).

usage: export_csv.py params.sqlite [out.csv]
"""
import argparse, csv, sqlite3, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import CSV_HEADER, IPAC_COLS


def export_rows(con):
    return con.execute(f"SELECT {','.join(IPAC_COLS)} FROM ipac_params WHERE ipac_category != '' ORDER BY rowid").fetchall()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("out", nargs="?", help="default: stdout")
    a = ap.parse_args()
    rows = export_rows(sqlite3.connect(a.db))
    out = open(a.out, "w", newline="") if a.out else sys.stdout
    w = csv.writer(out, quoting=csv.QUOTE_NONNUMERIC)
    w.writerow(CSV_HEADER)
    w.writerows(rows)
    if a.out:
        out.close()
        print(f"{len(rows)} rows -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
