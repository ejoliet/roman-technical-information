"""Shared between rti_dump.py, serve.py, export_csv.py."""
import re

# IPAC param-db categories (roman.ipac.caltech.edu/page/param-db): page regex on Name, and the Name prefix
# we use for rows created from GitHub so that the page's regex puts them in the same table. First match wins.
IPAC_CATEGORIES = [
    ("Mission and Spacecraft", r"^(Mission|Spacecraft)", "Spacecraft_"),
    ("Telescope", r"^Telescope_", "Telescope_"),
    ("WFI Filter", r"WFI_(Filter|FILTER)", "WFI_Filter_"),
    ("WFI Grism", r"WFI_(Grism|GRISM)", "WFI_Grism_"),
    ("WFI Prism", r"WFI_(Prism|PRISM)", "WFI_Prism_"),
    ("WFI Sensitivity", r"Sensitivity|SENSITIVITY", "WFI_Sensitivity_"),
    ("WFI Imager", r"^WFI", "WFI_"),
    ("Coronagraph Observing Modes", r"^CGI_(Mode|Support_Mode|Best_Mode)", "CGI_Mode_"),
    ("Coronagraph Parameters", r"^CGI_Coronagraph_", "CGI_Coronagraph_"),
    ("Coronagraph Engineering Bandpass", r"Bandpass_Engineering|^CGI_Band_Engineering", "CGI_Bandpass_Engineering_"),
    ("Coronagraph Primary Bandpass", r"^CGI_Band(pass)?_", "CGI_Bandpass_"),
    ("Coronagraph Detector", r"(Coronagraph|CGI).*Detector", "CGI_Detector_"),
    ("Coronagraph Imaging", r"^CGI_Imaging_", "CGI_Imaging_"),
    ("Coronagraph Spectroscopy", r"^CGI_(Spectroscopy|IFS)_", "CGI_Spectroscopy_"),
    ("Coronagraph Polarimetry", r"^CGI_Polarimetry_", "CGI_Polarimetry_"),
    ("Coronagraph Predicted Performance", r"(Coronagraph|CGI).*Performance", "CGI_Performance_"),
    ("Coronagraph Observing Requirements", r"(Coronagraph|CGI).*Requirement", "CGI_Requirement_"),
]
CATEGORY_NAMES = [c[0] for c in IPAC_CATEGORIES]
PREFIX = {c[0]: c[2] for c in IPAC_CATEGORIES}

# Default IPAC category for a GitHub param, matched on "<dir>/<key_path>", first wins.
GITHUB_TO_IPAC = [
    ("Telescope", r"Telescope_Parameters"),
    ("Mission and Spacecraft", r"^Observatory/"),
    ("WFI Sensitivity", r"^WideFieldInstrument/.*Sensitivity"),
    ("WFI Grism", r"^WideFieldInstrument/Spectroscopy/.*[Gg]rism"),
    ("WFI Prism", r"^WideFieldInstrument/Spectroscopy/.*[Pp]rism"),
    ("WFI Filter", r"^WideFieldInstrument/Imaging/(FiltersSummary|BandEdges)"),
    ("WFI Imager", r"^WideFieldInstrument/"),
    ("Coronagraph Observing Modes", r"^CoronagraphInstrument/ObservingModes"),
    ("Coronagraph Parameters", r"^CoronagraphInstrument/CoronagraphParameters"),
    ("Coronagraph Engineering Bandpass", r"^CoronagraphInstrument/Filters/EngineeringFilters"),
    ("Coronagraph Primary Bandpass", r"^CoronagraphInstrument/Filters/"),
    ("Coronagraph Detector", r"^CoronagraphInstrument/Detector"),
    ("Coronagraph Imaging", r"^CoronagraphInstrument/Imaging"),
    ("Coronagraph Spectroscopy", r"^CoronagraphInstrument/Spectroscopy"),
    ("Coronagraph Polarimetry", r"^CoronagraphInstrument/Polarimetry"),
]

IPAC_COLS = ["name", "value", "unit", "basic", "scalar", "description", "source", "expression", "latex_label", "last_modified"]
CSV_HEADER = ["Name", "Value", "Unit", "Basic", "Scalar", "Description", "Source", "Expression", "LaTeX label", "Last modified"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY, commit_sha TEXT UNIQUE, tag TEXT, version TEXT, commit_date TEXT, dumped_at TEXT);
CREATE TABLE IF NOT EXISTS params(
  snapshot_id INTEGER, param_id TEXT, source_file TEXT, github_category TEXT,
  key_path TEXT, value TEXT, unit TEXT, description TEXT, ipac_category_default TEXT,
  PRIMARY KEY(snapshot_id, param_id));
CREATE TABLE IF NOT EXISTS changes(
  snapshot_id INTEGER, param_id TEXT, kind TEXT, old_value TEXT, new_value TEXT, PRIMARY KEY(snapshot_id, param_id));
-- the IPAC master table (RomanParameters.csv) + our link to GitHub; user-edited, never overwritten by a dump
CREATE TABLE IF NOT EXISTS ipac_params(
  name TEXT PRIMARY KEY, value TEXT, unit TEXT, basic TEXT, scalar TEXT, description TEXT,
  source TEXT, expression TEXT, latex_label TEXT, last_modified TEXT,
  ipac_category TEXT, github_param_id TEXT, link_kind TEXT, suggested_param_id TEXT, suggested_score REAL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS category_map(github_category TEXT PRIMARY KEY, ipac_category TEXT);
-- undo stack for the editor: kind = edit (name, field, old) | add (name) | delete (full row)
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, payload TEXT);
"""


def first_match(rules, text, default=""):
    for rule in rules:
        if re.search(rule[1], text):
            return rule[0]
    return default


def key_to_name(ipac_category, key_path):
    return PREFIX.get(ipac_category, "") + re.sub(r"\W+", "_", key_path).strip("_")


# --- linking IPAC rows to GitHub params
STOP = {"wfi", "cgi", "the", "of", "in", "a", "and", "for", "to", "per", "is", "at", "with", "roman", "parameters",
        "properties", "overview", "instrument", "coronagraph", "spacecraft", "mission", "telescope", "s"}
SYN = {"readnoise": "read_noise", "fwhm": "full_width_half_max", "iwa": "inner_working_angle", "owa": "outer_working_angle",
       "hlc": "hybrid_lyot", "spc": "shaped_pupil", "psf": "point_spread_function", "qe": "quantum_efficiency",
       "dc": "dark_current", "mas": "milliarcsecond", "um": "micron", "nm": "nanometer", "min": "minimum", "max": "maximum",
       "sca": "sensor_chip_assembly", "deg": "degree"}


def _toks(s):
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", s or "")
    out = set()
    for t in set(re.findall(r"[a-z0-9]+", s.lower())) - STOP:
        out.add(t)
        out |= set(SYN.get(t, "").split("_")) - {""}
    return out


def _num(v):
    m = re.search(r"-?\d+(\.\d+)?([eE]-?\d+)?", v or "")
    return float(m[0]) if m else None


def _same_value(a, b):
    if a and b and a.strip().lower() == b.strip().lower():
        return True
    x, y = _num(a), _num(b)
    if x is None or y is None:
        return False
    return any(abs(x - y * f) <= 1e-9 * max(1, abs(x)) for f in (1, 1e3, 1e-3))  # unit prefix differences (nm/um, s/ms)


def suggest_links(con, snapshot_id):
    """For IPAC rows without a GitHub link: auto-link when name tokens overlap AND the value agrees;
    otherwise store the best guess as a suggestion. Returns (auto, suggested) counts."""
    gh = [(pid, key, f, val, cat, _toks(key) | _toks(f.rsplit("/", 1)[-1]) | _toks(desc))
          for pid, key, f, val, desc, cat in con.execute(
              "SELECT param_id, key_path, source_file, value, description, ipac_category_default FROM params WHERE snapshot_id=?", (snapshot_id,))]
    n_auto = n_sugg = 0
    for name, val, desc, cat in con.execute(
            "SELECT name, value, description, ipac_category FROM ipac_params WHERE github_param_id IS NULL OR github_param_id=''").fetchall():
        pre = PREFIX.get(cat, "")
        core = name[len(pre):] if name.startswith(pre) else name
        ct, nt = _toks(core), _toks(core) | _toks(desc)
        best = (0, False, None)
        for pid, key, f, gval, gcat, gt in gh:
            j = len(nt & gt) / len(nt | gt) if nt | gt else 0
            vm = _same_value(val, gval)
            score = j + 0.5 * len(ct & _toks(key)) / max(1, len(ct)) + (0.6 if vm else 0) + (0.2 if gcat == cat else 0)
            if score > best[0]:
                best = (score, vm, pid)
        score, vm, pid = best
        if pid and vm and score >= 1.2:
            con.execute("UPDATE ipac_params SET github_param_id=?, link_kind='auto', suggested_param_id=NULL WHERE name=?", (pid, name))
            n_auto += 1
        elif pid and score >= 0.6:
            con.execute("UPDATE ipac_params SET suggested_param_id=?, suggested_score=? WHERE name=?", (pid, round(score, 2), name))
            n_sugg += 1
    return n_auto, n_sugg
