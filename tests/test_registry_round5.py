"""U1a round 5: persisted text is reduced by KNOWN paths first, regex second, and a RegistryError carries a
`persist_text` built from structured parts. Paths with spaces and quotes (macOS: 'Application Support') must not leak."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

SECRETS = ("zz private dir", "zz-secret", "Application Support", "zz it's dir", "My Private Folder")


@pytest.fixture()
def w(tmp_path, monkeypatch):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def no_secret(text, label=""):
    bad = [s for s in SECRETS if s in text]
    assert not bad, f"{label}: leaked {bad} in {text[:300]!r}"


def broken_env(w, monkeypatch, dirname):
    d = w / dirname; d.mkdir(parents=True)
    f = d / "reg.json"; f.write_text("{ torn")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f))
    return f


@pytest.mark.parametrize("dirname", ["zz private dir", "zz it's dir"])
def test_env_registry_with_spaces_and_quotes_is_reduced_everywhere_persisted(w, monkeypatch, dirname):
    from factlattice import registry as R
    from factlattice_engine import server, tools
    from factlattice_engine.reviewer import compare_registries
    f = broken_env(w, monkeypatch, dirname)
    eng = server.boot_engine(retry_delay_s=0.0, watch=False)
    live = tools.dispatch("lts_find", {"corpus": "x", "prefix": "N"}, tools=eng)
    assert dirname in live["error"]                                  # terminal/MCP text: FULL path, as ruled
    choice = tools.dispatch("lts_list_corpora", {}, tools=eng)["registry_choice"]
    assert dirname in choice["error"] and "reg.json" in choice["error_persist"]
    no_secret(choice["error_persist"], "error_persist")
    cli = {"source": "env", "path": str(f), "sha256": "0" * 64}
    rec = compare_registries(choice, cli)
    no_secret(json.dumps(rec), "reviewer record (error-mode server)")
    # the same via a choice that carries NO error_persist (an older server): known paths first, then the pattern
    old = {k: v for k, v in choice.items() if k != "error_persist"}
    no_secret(json.dumps(compare_registries(old, cli)), "reviewer record (no error_persist)")
    # the query log
    log = Path(os.environ["FACTLATTICE_QUERY_LOG"]).read_text()
    assert log.strip(); no_secret(log, "query log")
    # RegistryError itself
    with pytest.raises(R.RegistryError) as e:
        R.resolve_registry()
    assert dirname in str(e.value) and "reg.json" in e.value.persist_text
    no_secret(e.value.persist_text, "persist_text")


def test_pointer_in_application_support_is_reduced(w, monkeypatch):
    from factlattice import registry as R
    from factlattice_engine import server, tools
    home = w / "home"; xdg = home / "Library" / "Application Support" / "zz-secret"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "Library" / "Application Support" / "zz-secret"))
    ptr = Path(R.pointer_path()); ptr.parent.mkdir(parents=True)
    ptr.write_text(json.dumps({"corpus_config": str(w / "gone dir" / "nowhere.json")}))
    with pytest.raises(R.RegistryError) as e:
        R.resolve_registry()
    assert "Application Support" in str(e.value) and "zz-secret" in str(e.value)       # full for the terminal
    no_secret(e.value.persist_text, "pointer persist_text")
    assert "config.json" in e.value.persist_text
    eng = server.boot_engine(retry_delay_s=0.0, watch=False)
    tools.dispatch("lts_find", {"corpus": "x", "prefix": "N"}, tools=eng)
    no_secret(Path(os.environ["FACTLATTICE_QUERY_LOG"]).read_text(), "query log (pointer)")
    ch = tools.dispatch("lts_list_corpora", {}, tools=eng)["registry_choice"]
    no_secret(ch["error_persist"], "registry_choice.error_persist (pointer)")


def test_written_review_and_unreviewed_documents_hold_no_private_name(w, monkeypatch):
    from factlattice_engine import server, tools
    from factlattice_engine.reviewer import compare_registries
    f = broken_env(w, monkeypatch, "zz private dir")
    eng = server.boot_engine(retry_delay_s=0.0, watch=False)
    choice = tools.dispatch("lts_list_corpora", {}, tools=eng)["registry_choice"]
    rec = compare_registries(choice, {"source": "env", "path": str(f), "sha256": "0" * 64})
    out = w / "reviews"; out.mkdir()
    for kind in ("REVIEW", "UNREVIEWED"):
        doc = out / f"{kind}-1.lts"
        r = tools.dispatch("lts_add_node", {"path": str(doc), "node_id": f"{kind}-X", "title": "t", "create": True,
                                           "create_parent": True, "doctype": "review",
                                           "properties": {"registry-mcp": rec["registry_mcp"], "registry-cli": rec["registry_cli"],
                                                          "stop-reason": rec["registry_mcp"]}}, tools=None)
        assert r.get("success") is True or r.get("written") is True, r
        no_secret(doc.read_text(), kind)
        assert "reg.json" in doc.read_text()


def test_persist_text_is_built_from_parts_with_basenames_only(w, monkeypatch):
    from factlattice import registry as R
    for d in ("zz private dir", "zz it's dir"):
        f = broken_env(w, monkeypatch, d)
        with pytest.raises(R.RegistryError) as e:
            R.resolve_registry()
        assert e.value._persist is not None and "reg.json" in e.value.persist_text
        no_secret(e.value.persist_text)
        assert str(f) in str(e.value) and e.value.path == str(f)
    # a missing file and a non-object file too
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w / "zz private dir" / "gone.json"))
    with pytest.raises(R.RegistryError) as e:
        R.resolve_registry()
    assert e.value._persist is not None; no_secret(e.value.persist_text)
