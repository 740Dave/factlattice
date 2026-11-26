"""The contradiction checker carries NO deployment id families. The ids that legitimately live outside
the checked corpus are declared by each deployment, in its registry entry (`external_id_prefixes`), and
reach the check as an explicit, required argument. (The checker is deliberately unwired: it is a
candidate generator, not a gate.)

Fixtures use invented families (ACME-, WIDGET-). The registry key is fixed in code and read by exactly
one helper, so a typo in it is a loud error, not a quiet change in what counts as a dangling reference.
"""
import json
import os

import pytest

from factlattice.authoring import contradiction as C

# Captured at import: the suite's autouse fixture removes the variable from every test's environment.
_REGISTRY_COPY = os.environ.get("FACTLATTICE_CORPUS_CONFIG")

DRAFT = """## [DRAFT-NODE] A draft
- summary: cites [ACME-RULE-ONE] and [WIDGET-RULE-TWO] and [DRAFT-OTHER]
"""
OTHER = "## [DRAFT-OTHER] Present in the corpus\n- summary: x\n"


def _corpus(tmp_path):
    (tmp_path / "c.lts").write_text(OTHER, encoding="utf-8")
    return tmp_path


def _dangling(res):
    return sorted(e for f in res["findings"] if f.check.startswith("C/") for e in f.evidence)


# -- the shipped default holds nothing ---------------------------------------------------------------
def test_shipped_default_is_empty_and_the_old_constant_is_gone():
    assert C.DEFAULT_EXTERNAL_PREFIXES == ()
    assert not hasattr(C, "_EXTERNAL_PREFIXES")


def test_with_no_prefixes_only_the_generic_short_ref_rule_applies():
    assert C._is_external("ACME-SOME-LONG-RULE", ()) is False
    assert C._is_external("A-Z", ()) is True           # <= 4 chars: scraped from a regex class or example, generic


def test_declared_prefix_is_external_and_an_undeclared_one_is_not():
    assert C._is_external("ACME-SOME-LONG-RULE", ("ACME-",)) is True
    assert C._is_external("WIDGET-SOME-LONG-RULE", ("ACME-",)) is False      # control


# -- the argument is required: no silent empty fallback ----------------------------------------------
def test_check_c_dangling_requires_the_prefixes():
    with pytest.raises(TypeError):
        C.check_c_dangling(C.parse_lts(DRAFT), {})


def test_check_requires_the_prefixes(tmp_path):
    with pytest.raises(TypeError):
        C.check(DRAFT, _corpus(tmp_path))


def test_check_honours_declared_prefixes(tmp_path):
    root = _corpus(tmp_path)
    assert _dangling(C.check(DRAFT, root, external_prefixes=("ACME-",))) == ["WIDGET-RULE-TWO"]
    assert _dangling(C.check(DRAFT, root, external_prefixes=("ACME-", "WIDGET-"))) == []
    assert _dangling(C.check(DRAFT, root, external_prefixes=())) == ["ACME-RULE-ONE", "WIDGET-RULE-TWO"]


def test_check_reports_how_many_prefixes_it_was_given_and_shouts_when_none(tmp_path):
    root = _corpus(tmp_path)
    some = C.check(DRAFT, root, external_prefixes=("ACME-",))
    none = C.check(DRAFT, root, external_prefixes=())
    assert some["denominators"]["external_prefixes_supplied"] == 1
    assert none["denominators"]["external_prefixes_supplied"] == 0
    assert "external_prefixes was EMPTY" in none["bound"]
    assert "external_prefixes was EMPTY" not in some["bound"]


# -- validation: a malformed declaration is refused, never reinterpreted ------------------------------
def test_a_bare_string_is_refused_not_iterated_into_characters():
    # "ACME-" iterated is 'A','C','M','E','-': five prefixes that together match almost every id.
    with pytest.raises(ValueError):
        C.parse_external_prefixes("ACME-")


@pytest.mark.parametrize("bad", [[""], ["ACME-", 3], [None], {"ACME-": 1}, 7])
def test_malformed_declarations_are_refused(bad):
    with pytest.raises(ValueError):
        C.parse_external_prefixes(bad)


def test_valid_declaration_is_a_deduplicated_tuple_in_order():
    assert C.parse_external_prefixes(["ACME-", "WIDGET-", "ACME-"]) == ("ACME-", "WIDGET-")
    assert C.parse_external_prefixes([]) == ()


# -- the registry reader ----------------------------------------------------------------------------
REG = {
    "one": {"roots": [], "external_id_prefixes": ["ACME-"]},
    "two": {"roots": [], "external_id_prefixes": ["WIDGET-", "ACME-"]},
    "three": {"roots": []},
}


def test_registry_reader_unions_the_selected_entries():
    assert C.external_prefixes_from_registry(REG, ["one"]) == ("ACME-",)
    assert C.external_prefixes_from_registry(REG, ["one", "two"]) == ("ACME-", "WIDGET-")
    assert C.external_prefixes_from_registry(REG, ["three"]) == ()
    assert C.external_prefixes_from_registry(REG) == ("ACME-", "WIDGET-")      # None = every entry


def test_registry_reader_refuses_an_unknown_corpus_and_a_malformed_entry():
    with pytest.raises(KeyError):
        C.external_prefixes_from_registry(REG, ["nope"])
    with pytest.raises(ValueError) as e:
        C.external_prefixes_from_registry({"bad": {"external_id_prefixes": "ACME-"}})
    assert "ACME-" not in str(e.value)                                   # never echo the value


def test_registry_reader_refuses_a_misspelt_key_it_can_recognise():
    # A near-miss key would otherwise leave the prefixes empty and change what the check reports.
    for typo in ("external_id_prefix", "external-id-prefixes", "external_prefixes"):
        with pytest.raises(ValueError):
            C.external_prefixes_from_registry({"x": {typo: ["ACME-"]}})


def test_live_registry_route_reads_the_registry_the_engine_resolves(tmp_path, monkeypatch):
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"one": {"roots": [{"path": str(tmp_path), "area": "specs"}],
                                       "domain_suffix": "t.one", "external_id_prefixes": ["ACME-"]}}))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(reg))
    assert C.external_prefixes_from_live_registry(["one"]) == ("ACME-",)
    assert C.external_prefixes_from_live_registry() == ("ACME-",)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(Exception):                                       # a broken registry never falls back
        C.external_prefixes_from_live_registry()


# -- round 2 -----------------------------------------------------------------------------------------
def test_check_c_dangling_refuses_a_bare_string_directly():
    # Called directly (not through check()), a bare string would be iterated into one-character prefixes.
    with pytest.raises(ValueError):
        C.check_c_dangling(C.parse_lts(DRAFT), {}, "ACME-")


def test_check_c_dangling_accepts_a_list_and_a_tuple():
    nodes = C.parse_lts(DRAFT)
    for pre in (["ACME-"], ("ACME-",)):
        got = sorted(e for f in C.check_c_dangling(nodes, {}, pre) for e in f.evidence)
        assert got == ["DRAFT-OTHER", "WIDGET-RULE-TWO"]


def test_a_note_key_starting_with_external_is_not_refused():
    corpora = {"x": {"external_note": "n", "external_id_prefixes": ["ACME-"]}}
    assert C.external_prefixes_from_registry(corpora) == ("ACME-",)


@pytest.mark.parametrize("typo", ["External_Id_Prefixes", "external id prefixes", "external-id-prefix",
                                  "EXTERNAL-PREFIXES", "external_id_prefixes "])
def test_separator_and_case_variants_are_still_refused(typo):
    with pytest.raises(ValueError):
        C.external_prefixes_from_registry({"x": {typo: ["ACME-"]}})


# -- round 3: an exact-key allowlist ------------------------------------------------------------------
@pytest.mark.parametrize("typo", ["external_id_prefxies", "external_id_prefixs", "ext_id_prefixes",
                                  "external_id_prefix"])
def test_character_typos_are_refused_and_name_the_nearest_key(typo):
    with pytest.raises(ValueError) as e:
        C.external_prefixes_from_registry({"x": {typo: ["ACME-"]}})
    assert "external_id_prefixes" in str(e.value)


def test_a_key_outside_the_allowlist_is_refused_even_with_no_near_match():
    with pytest.raises(ValueError) as e:
        C.external_prefixes_from_registry({"x": {"external_ref_count": 3}})
    assert "external_ref_count" in str(e.value)


@pytest.mark.parametrize("key", ["profile_note", "exclusion_note", "placement_note", "anything_at_all_note"])
def test_note_keys_are_comments_and_never_refused(key):
    assert C.external_prefixes_from_registry({"x": {key: "free text", "external_id_prefixes": ["ACME-"]}}) == ("ACME-",)


def test_every_key_the_registry_readers_use_is_allowed():
    entry = {"roots": [{"path": "p", "area": "specs", "only_file": "f"}], "root": "r", "domain_suffix": "d",
             "profile": "pf", "private": True, "external_id_prefixes": ["ACME-"]}
    assert C.external_prefixes_from_registry({"x": entry}) == ("ACME-",)


def test_the_allowlist_is_defined_once():
    assert set(C.REGISTRY_ENTRY_KEYS) == {"roots", "root", "domain_suffix", "profile", "private",
                                          "external_id_prefixes"}
    assert C.NOTE_KEY_SUFFIX == "_note"


def test_a_copy_of_the_live_registry_has_zero_refusals():
    path = _REGISTRY_COPY
    if not path or not os.path.isfile(path):
        pytest.skip("no registry available (FACTLATTICE_CORPUS_CONFIG unset or not a file)")
    from factlattice import registry
    corpora = registry.entries_from(path)
    assert corpora
    refusals = []
    for cid in corpora:
        try:
            C.external_prefixes_from_registry(corpora, [cid])
        except ValueError as exc:
            refusals.append(str(exc))
    assert refusals == []
