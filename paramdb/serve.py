#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Local editor for a paramdb sqlite file: uv run serve.py params.sqlite [--host 0.0.0.0] [--port 8765]

Serves index.html and a tiny JSON API; edits go straight into the same file.
"""
import argparse, csv, io, json, socket, sqlite3, sys, webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import CATEGORY_NAMES, CSV_HEADER, IPAC_COLS, SCHEMA, key_to_name
from export_csv import export_rows

HERE = Path(__file__).parent
EDITABLE = set(IPAC_COLS[1:]) | {"ipac_category", "github_param_id"}
TOUCH = {"value", "unit", "description", "source", "expression", "latex_label"}  # edits that bump last_modified

LATEST = "(SELECT max(id) FROM snapshots)"
IPAC_SQL = f"""
SELECT i.*, p.key_path AS github_key, p.value AS github_value, p.unit AS github_unit, p.github_category,
       c.kind AS change_kind, c.old_value, sp.value AS suggested_value
FROM ipac_params i
LEFT JOIN params sp ON sp.param_id = i.suggested_param_id AND sp.snapshot_id = {LATEST}
LEFT JOIN params p ON p.param_id = i.github_param_id AND p.snapshot_id = {LATEST}
LEFT JOIN changes c ON c.param_id = i.github_param_id AND c.snapshot_id = {LATEST}
ORDER BY i.rowid
"""
GITHUB_SQL = f"""
SELECT p.*, c.kind AS change_kind, c.old_value, i.name AS ipac_name
FROM params p
LEFT JOIN changes c ON c.param_id = p.param_id AND c.snapshot_id = {LATEST}
LEFT JOIN ipac_params i ON instr(i.github_param_id, p.param_id) > 0
WHERE p.snapshot_id = {LATEST} ORDER BY p.rowid
"""


def ipac_rows(c):
    """IPAC rows with their GitHub value; a 'min_id + max_id' link composes the GitHub value as 'lo-hi'."""
    rows = [dict(r) for r in c.execute(IPAC_SQL)]
    gh = {r["param_id"]: r for r in c.execute(f"SELECT param_id, value, unit FROM params WHERE snapshot_id = {LATEST}")}
    for r in rows:
        ids = (r["github_param_id"] or "").split(" + ")
        if len(ids) == 2 and all(i in gh for i in ids):
            r["github_value"] = "-".join(gh[i]["value"] for i in ids)
            r["github_unit"] = gh[ids[0]]["unit"]
    return rows


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class H(BaseHTTPRequestHandler):
    db = None

    def con(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def send(self, body, ctype, extra=()):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index.html"):
            self.send((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/data":
            with self.con() as c:
                self.send_json({
                    "db": str(self.db),
                    "snapshots": [dict(r) for r in c.execute("SELECT * FROM snapshots ORDER BY id")],
                    "categories": CATEGORY_NAMES,
                    "category_map": dict(c.execute("SELECT github_category, ipac_category FROM category_map").fetchall()),
                    "ipac": ipac_rows(c),
                    "undo": c.execute("SELECT count(*) FROM history").fetchone()[0],
                    "github": [dict(r) for r in c.execute(GITHUB_SQL)],
                })
        elif self.path == "/api/db":  # consistent copy of the whole sqlite (backup API, safe while edits happen)
            mem = sqlite3.connect(":memory:")
            with self.con() as c:
                c.backup(mem)
            body = mem.serialize()
            self.send(body, "application/vnd.sqlite3", [("Content-Disposition", f'attachment; filename="{self.db.stem}-{datetime.now():%Y%m%d-%H%M}.sqlite"')])
        elif self.path == "/api/export.csv":
            buf = io.StringIO()
            w = csv.writer(buf, quoting=csv.QUOTE_NONNUMERIC)
            w.writerow(CSV_HEADER)
            with self.con() as c:
                w.writerows(export_rows(c))
            self.send(buf.getvalue().encode(), "text/csv", [("Content-Disposition", "attachment; filename=RomanParameters.csv")])
        else:
            self.send_error(404)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.con() as c:
            if self.path == "/api/ipac":  # edit one cell of an IPAC row
                name, field, val = req.get("name"), req.get("field"), req.get("value") or ""
                if field not in EDITABLE:
                    return self.send_json({"error": "bad field"}, 400)
                old = c.execute(f"SELECT {field}, last_modified, link_kind FROM ipac_params WHERE name=?", (name,)).fetchone()
                if not old:
                    return self.send_json({"error": "unknown row"}, 404)
                self.push(c, "edit", {"name": name, "field": field, "old": old[0], "last_modified": old[1], "link_kind": old[2]})
                t = now()
                sets = f"{field}=?, updated_at=?" + (", last_modified=?" if field in TOUCH else "") + (", link_kind='manual'" if field == "github_param_id" else "")
                args = [val, t] + ([t[:19]] if field in TOUCH else []) + [name]
                c.execute(f"UPDATE ipac_params SET {sets} WHERE name=?", args)
                return self.send_json({"ok": True, "updated_at": t, "last_modified": t[:19] if field in TOUCH else None})
            if self.path == "/api/ipac/add":  # new IPAC row from a GitHub param
                p = c.execute(f"SELECT * FROM params WHERE param_id=? AND snapshot_id={LATEST}", (req["param_id"],)).fetchone()
                s = c.execute("SELECT tag, commit_sha FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
                if not p:
                    return self.send_json({"error": "unknown param"}, 404)
                cat = c.execute("SELECT ipac_category FROM category_map WHERE github_category=?", (p["github_category"],)).fetchone()
                cat = cat[0] if cat else p["ipac_category_default"]
                name = req.get("name") or key_to_name(cat, p["key_path"])
                if c.execute("SELECT 1 FROM ipac_params WHERE name=?", (name,)).fetchone():
                    return self.send_json({"error": f"{name} already exists"}, 409)
                t = now()
                c.execute("INSERT INTO ipac_params(name,value,unit,basic,scalar,description,source,expression,latex_label,last_modified,ipac_category,github_param_id,link_kind,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (name, p["value"], p["unit"], "true", "true", p["description"],
                           f"roman-technical-information {s['tag'] or s['commit_sha'][:10]} {p['source_file']}", "", "", t[:19], cat, p["param_id"], "manual", t))
                self.push(c, "add", {"name": name})
                return self.send_json({"ok": True, "name": name})
            if self.path == "/api/ipac/delete":
                row = c.execute("SELECT * FROM ipac_params WHERE name=?", (req["name"],)).fetchone()
                if row:
                    self.push(c, "delete", {"row": dict(row), "rowid": c.execute("SELECT rowid FROM ipac_params WHERE name=?", (req["name"],)).fetchone()[0]})
                    c.execute("DELETE FROM ipac_params WHERE name=?", (req["name"],))
                return self.send_json({"ok": True})
            if self.path == "/api/undo":
                h = c.execute("SELECT id, kind, payload FROM history ORDER BY id DESC LIMIT 1").fetchone()
                if not h:
                    return self.send_json({"ok": False, "msg": "nothing to undo"})
                p = json.loads(h["payload"])
                if h["kind"] == "edit":
                    c.execute(f"UPDATE ipac_params SET {p['field']}=?, last_modified=?, link_kind=?, updated_at=? WHERE name=?",
                              (p["old"], p["last_modified"], p["link_kind"], now(), p["name"]))
                    msg = f"undid {p['field']} of {p['name']}"
                elif h["kind"] == "add":
                    c.execute("DELETE FROM ipac_params WHERE name=?", (p["name"],))
                    msg = f"removed added row {p['name']}"
                else:
                    r = p["row"]
                    cols = ", ".join(["rowid"] + list(r))
                    c.execute(f"INSERT INTO ipac_params({cols}) VALUES ({','.join('?' * (len(r) + 1))})", [p["rowid"]] + list(r.values()))
                    msg = f"restored row {r['name']}"
                c.execute("DELETE FROM history WHERE id=?", (h["id"],))
                return self.send_json({"ok": True, "msg": msg})
            if self.path == "/api/category":  # GitHub dir -> IPAC category default for new rows; None = dump default
                gh, ip = req.get("github_category"), req.get("ipac_category")
                if ip is None:
                    c.execute("DELETE FROM category_map WHERE github_category=?", (gh,))
                else:
                    c.execute("INSERT OR REPLACE INTO category_map VALUES (?,?)", (gh, ip))
                return self.send_json({"ok": True})
        self.send_error(404)

    @staticmethod
    def push(c, kind, payload):
        c.execute("INSERT INTO history(ts, kind, payload) VALUES (?,?,?)", (now(), kind, json.dumps(payload)))
        c.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 500)")

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from the network (anyone there can edit); 127.0.0.1 = this machine only")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    H.db = Path(a.db).resolve()
    if not H.db.exists():
        raise SystemExit(f"{H.db} not found; run rti_dump.py first")
    with sqlite3.connect(H.db) as c:
        c.executescript(SCHEMA)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"paramdb editor on {url}  (db: {H.db})  Ctrl-C to stop")
    if a.host == "0.0.0.0":
        print(f"also reachable on the network at http://{socket.getfqdn()}:{a.port}/  (no auth: anyone on the network can edit)")
    if not a.no_browser:
        webbrowser.open(url)
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()


if __name__ == "__main__":
    main()
