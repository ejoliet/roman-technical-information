# paramdb — Roman spacecraft & instrument parameters, versioned in sqlite

Keeps the IPAC parameter table (https://roman.ipac.caltech.edu/page/param-db, served from `RomanParameters.csv`)
in sync with the GitHub source of truth,
[RomanSpaceTelescope/roman-technical-information](https://github.com/RomanSpaceTelescope/roman-technical-information).

```
GitHub main ──rti_dump.py──▶ params.sqlite ◀──serve.py + index.html──▶ export_csv.py ──▶ RomanParameters.csv
              one snapshot per commit      browser editor on the IPAC rows      same columns, same row order
```

## Design in one paragraph

The IPAC CSV is the master: its 350 rows, columns and order are what gets published, and only some of its columns
(Value, Unit) have a counterpart in GitHub. So the sqlite holds two separate things. The **GitHub side** (`snapshots`,
`params`, `changes`) is an append-only history written only by the dump, one full copy per upstream commit, so any value
can be diffed against the previous dump. The **IPAC side** (`ipac_params`, `category_map`, `history`) is written only by
the editor and survives every dump. The two are joined by one link per IPAC row, `github_param_id`, which the dump infers
where it safely can and the editor lets you set or fix. The export is simply `ipac_params` back to CSV.

## Requirements

- [`uv`](https://docs.astral.sh/uv/) — the only install. Each script declares its Python version and deps inline
  (PEP 723 header), `uv run <script>` fetches them. No venv, no requirements or lock file. Only `rti_dump.py` needs a
  library (`pyyaml`); the server and export are stdlib.
- `git` (to pull upstream).

## Files

| file | role |
|---|---|
| `rti_dump.py` | GitHub → sqlite: clone/pull upstream, parse YAML + small ECSV, add a snapshot, diff vs previous, seed IPAC rows from CSV, infer links |
| `serve.py` | HTTP server + JSON API for the editor; every edit is written to the sqlite immediately |
| `index.html` | the editor (vanilla JS, no build) |
| `export_csv.py` | sqlite → `RomanParameters.csv` |
| `common.py` | IPAC categories + their Name regexes/prefixes, GitHub-dir → category defaults, schema, link inference |
| `params.sqlite` | the one database (git-ignored) |

## Usage

### First time

```bash
cd paramdb
uv run rti_dump.py params.sqlite --ipac-csv ~/Downloads/RomanParameters.csv
uv run serve.py params.sqlite
```

The dump seeds `ipac_params` from the CSV (350 rows), takes snapshot 1 of upstream `main` and links IPAC rows to GitHub
params (see *Linking* below). The server prints two URLs: local and `http://<host>:8765/` for the rest of the network
(`--host 127.0.0.1` restricts to this machine, `--port N` changes the port). **No authentication**: anyone who can reach
the port can edit.

### Each iteration (upstream changed)

```bash
uv run rti_dump.py params.sqlite        # pulls main; new snapshot only if HEAD changed, else no-op
uv run serve.py params.sqlite           # review in the browser
uv run export_csv.py params.sqlite RomanParameters.csv
```

In the editor:

1. **IPAC rows** view, tick *GitHub changed in latest snapshot*: linked rows whose GitHub value moved since the previous
   dump (`changed` / `added` flag, tooltip shows the old value). GitHub value / GitHub unit cells are red when they differ
   from the IPAC cell; click one to copy it in. Last modified is stamped automatically on edits of Value, Unit,
   Description, Source, Expression, LaTeX label. Fix the text columns by hand.
2. Tick *not linked* for IPAC rows still without a GitHub param. Accept the ✓ in **Suggested link** or type/pick a
   `file::key` in **GitHub param**. Tick *auto-linked* to review the links the dump inferred.
3. **GitHub params** view: everything upstream has. Pick the **IPAC category** once per GitHub directory (applies to all
   its params, stored in `category_map`), then **+ add to IPAC** on a param to publish it. That creates the IPAC row
   (Name = category prefix + key, value/unit/description from GitHub, Source = repo tag + file) already linked.
   Params that already have an IPAC row show its name instead.
4. Set a row's IPAC category to *(none)* to keep it out of the export. ✕ deletes a row.
5. **↶ Undo** (or Ctrl/Cmd+Z) reverts the last edit, add or delete; the stack lives in the sqlite (`history`, last 500)
   so it survives reloads and restarts.
6. **Export CSV**: the updated `RomanParameters.csv`. **Download DB**: a consistent copy of `params.sqlite` (handy when
   the server runs on another machine). There is no save button: every change is already in the server's file.

Table chrome: free-text filter, IPAC category / GitHub category dropdowns, click a header to sort (▲/▼), drag a header's
right edge to resize, double-click it to auto-fit (widths remembered per browser), Enter saves, Esc reverts, ↑ back to top.

### Dump options

- `--repo PATH` use a local clone as-is (no pull); default pulls upstream into `~/.cache/rti-paramdb/repo`.
- `--max-cells N` (default 400) skips ECSV tables bigger than that: effective-area curves, dispersion, roll-angle grids,
  per-SCA zeropoints. Those are curves, not parameters; raise it to pull them in.
- `--force` re-dump the current commit.
- `--ipac-csv FILE` only inserts names not yet present; existing rows are never touched.

## What the dump stores and why

- **One snapshot per upstream commit**, full copy of all params. Simple, diffable, and the data is small
  (~1300 rows per snapshot). `changes` holds the added / removed / changed param_ids vs the previous snapshot with old and
  new value, so the editor can flag them without recomputing.
- **YAML** files → one row per leaf, key `Section.key`. **ECSV** tables → one row per cell, key `<row key>.<column>`,
  row key = the leading non-numeric columns (`F062`, `1/F062` for SCA + filter), unit and description from the ECSV
  header. Upstream headers are not strict YAML (unquoted colons / quotes in descriptions), so there is a regex fallback.
- `param_id` = `<file>::<key>`, stable across snapshots. A renamed upstream file therefore shows as removed + added.
- Each param also gets `ipac_category_default` from its GitHub directory (`common.GITHUB_TO_IPAC`), used when you add it
  to IPAC unless `category_map` says otherwise.

## Linking IPAC rows to GitHub params

The CSV carries no reference to GitHub, so the link has to be inferred. Three tiers, recorded in `link_kind`:

| tier | rule | rows (v1.6) |
|---|---|---|
| `exact` | Name equals `<category prefix>_<GitHub key>` (`WFI_Filter_F062_PSF_FWHM` ↔ `filter_parameters.ecsv::F062.PSF_FWHM`) | 28 |
| `auto` | name + description tokens overlap **and** the values agree (numeric, allowing ×1000 unit-prefix differences, or same string) | 175 |
| `manual` | set in the editor (typed, ✓ on a suggestion, or created by *+ add to IPAC*) | — |
| suggestion only | best token match but value differs or weak score; shown in *Suggested link* with its value for you to confirm | 127 |
| none | no plausible candidate (detector type, focal ratio, crosstalk, ... not in GitHub) | ~20 |

Value agreement is the guard against wrong matches in `auto`; still, review them with the *auto-linked* filter once.
The dump only ever touches rows that are still unlinked, so your manual links persist. The IPAC category of a row comes
from the page's own Name regexes (`common.IPAC_CATEGORIES`); new rows get a Name prefix chosen so those regexes put them
in the same table on the IPAC page.

## Tables

| table | written by | content |
|---|---|---|
| `snapshots` | dump | commit sha, tag, VERSION.md, commit date, dump time |
| `params` | dump | per snapshot: param_id, source_file, github_category (dir), key_path, value, unit, description, ipac_category_default |
| `changes` | dump | per snapshot: added / removed / changed param_ids with old and new value |
| `ipac_params` | editor (seeded by dump) | CSV columns + ipac_category, github_param_id, link_kind, suggested_param_id/score, updated_at |
| `category_map` | editor | github_category → ipac_category used when adding rows |
| `history` | editor | undo stack: edit (name, field, old) / add (name) / delete (full row) |

Handy query — latest GitHub value next to each IPAC row:

```sql
SELECT i.name, i.value, p.value AS github_value, c.kind
FROM ipac_params i
LEFT JOIN params p  ON p.param_id = i.github_param_id AND p.snapshot_id = (SELECT max(id) FROM snapshots)
LEFT JOIN changes c ON c.param_id = i.github_param_id AND c.snapshot_id = (SELECT max(id) FROM snapshots);
```

## Network access troubleshooting

`http://<host>:8765/` not loading from another machine:
- start without `--host 127.0.0.1` (default binds all interfaces; the start-up line prints the network URL);
- macOS firewall: System Settings → Network → Firewall; allow incoming connections for `python` when prompted;
- check locally first: `curl -s http://localhost:8765/api/data | head -c 200`.
