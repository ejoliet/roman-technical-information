#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Dump RomanSpaceTelescope/roman-technical-information data into a versioned sqlite file.

usage: rti_dump.py DB [--repo URL_OR_PATH] [--ipac-csv RomanParameters.csv] [--max-cells N] [--force]

Each run = one snapshot keyed by git commit sha. Re-running on the same sha is a no-op
unless --force. The IPAC master table (ipac_params) is only seeded, never overwritten.
"""
import argparse, csv, io, re, sqlite3, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from common import GITHUB_TO_IPAC, IPAC_CATEGORIES, PREFIX, SCHEMA, first_match, suggest_links

UPSTREAM = "https://github.com/RomanSpaceTelescope/roman-technical-information.git"
CACHE = Path.home() / ".cache" / "rti-paramdb" / "repo"

def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def ensure_repo(spec):
    if spec and Path(spec).is_dir():
        return Path(spec)
    url = spec or UPSTREAM
    if CACHE.is_dir():
        subprocess.check_call(["git", "-C", str(CACHE), "pull", "-q", "--ff-only"])
    else:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        subprocess.check_call(["git", "clone", "-q", url, str(CACHE)])
    return CACHE


def flatten(d, prefix=""):
    for k, v in d.items():
        p = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            yield from flatten(v, p)
        elif isinstance(v, list):
            yield p, "; ".join(map(str, v))
        else:
            yield p, "" if v is None else str(v)


def parse_yaml(path):
    doc = yaml.safe_load(path.read_text().expandtabs(1)) or {}  # upstream yaml has stray tabs
    desc = str(doc.pop("Description", ""))
    for key, val in flatten(doc):
        yield key, val, "", desc


def parse_ecsv_header(header):
    """Upstream headers are not strict YAML (unquoted ':' and quotes in descriptions); fall back to regex."""
    try:
        return yaml.safe_load("\n".join(header))
    except yaml.YAMLError:
        cols = []
        for l in header:
            if l.startswith("- {name:"):
                m = re.match(r"- \{name: ([^,}]+)(?:, unit: (.*?))?, datatype: (\w+)(?:, description: (.*))?\}$", l)
                cols.append({"name": m[1].strip(), "unit": (m[2] or "").strip().strip("'"), "datatype": m[3], "description": (m[4] or "").strip()})
        d = re.search(r"^delimiter: '(.)'", "\n".join(header), re.M)
        return {"datatype": cols, "delimiter": d[1] if d else " "}


def parse_ecsv(path, max_cells):
    lines = path.read_text().splitlines()
    header = [l[2:] for l in lines if l.startswith("# ")]
    body = [l for l in lines if l.strip() and not l.startswith("#")]
    meta = parse_ecsv_header(header[1:])  # skip "%ECSV 1.0"
    cols = meta["datatype"]
    delim = meta.get("delimiter", " ")
    if delim == ",":
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO("\n".join(body)), skipinitialspace=True)]
    else:
        rows = [l.split() for l in body]
    rows = rows[1:]  # column header line
    if len(rows) * len(cols) > max_cells:  # curves / big grids are not parameters
        return
    # row key = leading non-float columns (e.g. SCA + Filter); fall back to first column
    nkey = 0
    while nkey < len(cols) - 1 and not cols[nkey]["datatype"].startswith("float"):
        nkey += 1
    nkey = max(nkey, 1)
    for r in rows:
        key = "/".join(r[:nkey])
        for col, val in zip(cols[nkey:], r[nkey:]):
            yield f"{key}.{col['name']}", val, str(col.get("unit", "") or ""), str(col.get("description", "") or "")


def collect(repo, max_cells):
    data = repo / "roman_technical_information" / "data"
    for f in sorted(data.rglob("*")):
        if f.suffix not in (".yaml", ".ecsv"):
            continue
        rel = f.relative_to(data).as_posix()
        cat = str(Path(rel).parent)
        parser = parse_yaml(f) if f.suffix == ".yaml" else parse_ecsv(f, max_cells)
        for key, val, unit, desc in parser:
            yield (f"{rel}::{key}", rel, cat, key, val, unit, desc, first_match(GITHUB_TO_IPAC, f"{cat}/{key}"))


def load_ipac_csv(con, csv_path):
    """Seed ipac_params from RomanParameters.csv. Existing names are left untouched (they hold user edits)."""
    with open(csv_path, newline="") as fh:
        rows = [r for r in list(csv.reader(fh))[1:] if r and r[0].strip()]
    n = 0
    for r in rows:
        r = [c.strip() for c in (r + [""] * 10)[:10]]
        n += con.execute("INSERT OR IGNORE INTO ipac_params(name,value,unit,basic,scalar,description,source,expression,latex_label,last_modified,ipac_category) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         r + [first_match(IPAC_CATEGORIES, r[0])]).rowcount
    print(f"ipac_params: {n} new rows from {csv_path} ({len(rows) - n} already present, kept)")


def autolink(con, sid):
    """Link IPAC rows without a GitHub param to one whose <prefix>_<key> equals the IPAC name (case-insensitive)."""
    targets = {r[0].lower(): r[0] for r in con.execute("SELECT name FROM ipac_params WHERE github_param_id IS NULL OR github_param_id=''")}
    n = 0
    for pid, key in con.execute("SELECT param_id, key_path FROM params WHERE snapshot_id=?", (sid,)):
        k = re.sub(r"\W+", "_", key).strip("_").lower()
        for pre in set(PREFIX.values()):
            name = targets.pop(pre.lower() + k, None)
            if name:
                n += con.execute("UPDATE ipac_params SET github_param_id=?, link_kind='exact' WHERE name=?", (pid, name)).rowcount
                break
    a, g = suggest_links(con, sid)
    print(f"linked IPAC rows to GitHub params: {n} by exact name, {a} by name+value; {g} suggestions left to confirm in the editor")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--repo", help="git URL or local clone (default: upstream, cached in ~/.cache)")
    ap.add_argument("--ipac-csv", help="seed ipac_params from RomanParameters.csv (existing rows kept)")
    ap.add_argument("--max-cells", type=int, default=400, help="skip ECSV tables with more cells (curves, per-SCA grids)")
    ap.add_argument("--force", action="store_true", help="re-dump even if this commit was already dumped")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    con.executescript(SCHEMA)
    if a.ipac_csv:
        load_ipac_csv(con, a.ipac_csv)

    repo = ensure_repo(a.repo)
    sha = git(repo, "rev-parse", "HEAD")
    tag = subprocess.run(["git", "-C", str(repo), "describe", "--tags", "--exact-match"], capture_output=True, text=True).stdout.strip()
    date = git(repo, "log", "-1", "--format=%cI")
    version = (repo / "VERSION.md").read_text().strip() if (repo / "VERSION.md").exists() else ""

    prev = con.execute("SELECT id FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    existing = con.execute("SELECT id FROM snapshots WHERE commit_sha=?", (sha,)).fetchone()
    if existing and not a.force:
        print(f"commit {sha[:10]} already dumped as snapshot {existing[0]}; use --force to redo")
        con.commit()
        return
    if existing:
        con.execute("DELETE FROM params WHERE snapshot_id=?", (existing[0],))
        con.execute("DELETE FROM changes WHERE snapshot_id=?", (existing[0],))
        con.execute("DELETE FROM snapshots WHERE id=?", (existing[0],))
        prev = con.execute("SELECT id FROM snapshots WHERE id<? ORDER BY id DESC LIMIT 1", (existing[0],)).fetchone()

    rows = list(collect(repo, a.max_cells))
    cur = con.execute("INSERT INTO snapshots(commit_sha,tag,version,commit_date,dumped_at) VALUES (?,?,?,?,?)",
                      (sha, tag, version, date, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    sid = cur.lastrowid
    con.executemany("INSERT INTO params VALUES (?,?,?,?,?,?,?,?,?)", [(sid, *r) for r in rows])

    n_changes = 0
    if prev:
        old = dict(con.execute("SELECT param_id, value FROM params WHERE snapshot_id=?", (prev[0],)).fetchall())
        new = {r[0]: r[4] for r in rows}
        ch = []
        for pid in old.keys() | new.keys():
            if pid not in old:
                ch.append((sid, pid, "added", None, new[pid]))
            elif pid not in new:
                ch.append((sid, pid, "removed", old[pid], None))
            elif old[pid] != new[pid]:
                ch.append((sid, pid, "changed", old[pid], new[pid]))
        con.executemany("INSERT INTO changes VALUES (?,?,?,?,?)", ch)
        n_changes = len(ch)
    autolink(con, sid)
    con.commit()
    print(f"snapshot {sid}: commit {sha[:10]} tag={tag or '-'} version={version} params={len(rows)} changes={n_changes}")


if __name__ == "__main__":
    main()
