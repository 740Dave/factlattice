"""Run inside ONE tree (PYTHONPATH = that tree, cwd = a neutral folder). Reads shapes.json, prints {label: verdict} as JSON on stdout and `PKG <path>` on stderr.

verdict: 'registry-error' (`build_store` raised) | 'failed:<code>' | 'served' | 'no-roots' | 'none' (in no table); ' (OK NOT SERVED)' is appended when the healthy
corpus beside the entry was not served. It uses only what both trees have (`config.build_store`, the store's tables), so the SAME file judges the release
and the branch ([MECH-BR-BOOT] x-load-error, third amendment: no entry shape's boot verdict changes from release af4cfc2)."""
import json
import os
import pathlib
import sys
import tempfile

shapes = json.load(open(sys.argv[1]))
base = pathlib.Path(tempfile.mkdtemp())
ok, bad = base / "ok", base / "bad"
for d, n in ((ok, "OK"), (bad, "BAD")):
    d.mkdir()
    (d / f"{n.lower()}.lts").write_text(f'---\nlts: "1.1"\ntitle: t\ndomain: x.{n.lower()}\ndoctype: SPEC\n---\n\n# R [{n}-ROOT]\n\n- summary: r\n', encoding="utf-8")
os.environ.update({"FACTLATTICE_STATE_DIR": str(base / "st"), "LTS_ENGINE_STATE_DIR": str(base / "st"), "FACTLATTICE_CODE_WATCH": "0", "LTS_ROOT_DEADLINE_S": "5"})
os.chdir(base)
import factlattice_engine  # noqa: E402

print("PKG", factlattice_engine.__file__, file=sys.stderr)
from factlattice_engine import config  # noqa: E402


def sub(v):
    if isinstance(v, str):
        return v.replace("{BAD}", str(bad)).replace("{OK}", str(ok))
    if isinstance(v, list):
        return [sub(x) for x in v]
    if isinstance(v, dict):
        return {sub(k): sub(x) for k, x in v.items()}
    return v


out = {}
for label, entry in shapes.items():
    top = {}
    if isinstance(entry, dict) and "$registry" in entry:
        entry = dict(entry)
        top = sub(entry.pop("$registry", {}))                      # a shape may add TOP-LEVEL registry keys (a `named_roots` table) beside the two corpora
    if isinstance(entry, dict) and "$profile_dir" in entry:         # the ENGINE's own profile directory for this shape (files: name -> text), named by FACTLATTICE_PROFILE_DIR
        entry = dict(entry)
        pd = base / f"pdir-{len(out)}"
        pd.mkdir()
        for name, text in entry.pop("$profile_dir").items():
            (pd / name).write_text(text, encoding="utf-8")
        os.environ["FACTLATTICE_PROFILE_DIR"] = str(pd)
    else:
        os.environ.pop("FACTLATTICE_PROFILE_DIR", None)
    if isinstance(entry, dict) and "$profile" in entry:            # a shape may carry a profile BODY: written beside the registry and named by the entry's `profile` key (spec 39 shapes)
        entry = dict(entry)
        pf = base / f"prof-{len(out)}.json"                        # one file per shape: the engine memoizes a loaded profile by PATH, so a reused name would answer every later shape with the first one
        pf.write_text(json.dumps(entry.pop("$profile")))
        entry["profile"] = str(pf)
    reg = {"okc": {"root": str(ok), "domain_suffix": "x.ok"}, "badc": sub(entry), **top}
    rp = base / "reg.json"
    rp.write_text(json.dumps(reg))
    os.environ["FACTLATTICE_CORPUS_CONFIG"] = str(rp)
    try:
        st = config.build_store()
    except BaseException:
        out[label] = "registry-error"
        continue
    failed = getattr(st, "failed_corpora", None)
    if failed is None:
        failed = st.tables()[1]
        no_roots = st.tables()[2]
    else:
        no_roots = getattr(st, "no_roots_corpora", {})
    if "badc" in failed:
        f = failed["badc"]
        out[label] = ("failed:" + f["code"] + (":" + f["reason"] if f.get("code") == "corpus-load-error" and f.get("reason") else "")
                      + (":names=" + ",".join(f["names"]) if f.get("code") == "corpus-load-error" and f.get("names") else "")
                      + (":detail=" + str(f["detail"]) if f.get("code") == "corpus-load-error" and f.get("detail") else ""))
    elif "badc" in st.corpus_ids():
        out[label] = "served"
    elif "badc" in no_roots:
        out[label] = "no-roots"
    else:
        out[label] = "none"
    out[label] += "" if "okc" in st.corpus_ids() else " (OK NOT SERVED)"
print(json.dumps(out))
