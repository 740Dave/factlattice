"""factlattice-reviewer: the shipped reviewer corpus and the pure code the reviewer's procedure obeys.

This package is PACKAGE DATA first (SDLC 7.6 X8): `shipped/*.lts` (the core procedure, the hooks, the generic
rules, the config and the reviewer profile) and `fixtures/*.lts` (each rule's planted violation and clean twin)
ship in the wheel, so a deployment without a checkout can read the core procedure by file when the engine does
not serve the `factlattice-reviewer` corpus (SPEC-A-reviewer-v3 A-E6, fetch order step 2).

The Python here is deliberately small and PURE. Where a review is WRITTEN is decided by `resolve_output`, in code,
because a model that chooses its own output root can be told to write somewhere else by the document under review
(addendum C5), and a site whose overrides silently went missing would write restricted-corpus reviews into the
general folder (addendum C3). The reviewer obeys what `resolve_output` returns; the deployment's go-live check
calls the same function through `python -m factlattice_engine.reviewer resolve-output`.

NOTHING in this module writes: not a file, not a directory, not the ledger.
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from ..privacy import private_reason

# ---------------------------------------------------------------------------------------------- contracts
# X1/X4 VERSIONED-CONTRACTS. Renaming or removing a hook bumps HOOK_CONTRACT_VERSION; renaming or removing a
# review field bumps REVIEW_FORMAT_VERSION. `tests/reviewer_contract.py` holds the TEST's copy of these literals
# and `tests/test_reviewer_contracts.py` pins them against the shipped corpus, the profile and the agent def.
HOOK_CONTRACT_VERSION = 1
REVIEW_FORMAT_VERSION = 2          # 2: REVIEW/UNREVIEWED gain `registry-mcp` and `registry-cli` (SPEC-U1a)
CORE_HOOKS: Tuple[str, ...] = ("REVHOOK-STRUCTURE", "REVHOOK-DETERMINISTIC", "REVHOOK-JUDGEMENT",
                               "REVHOOK-CROSS-DOCUMENT", "REVHOOK-REPORT")
CORPUS_ID = "factlattice-reviewer"
SITE_CONFIG_NODE = "REVIEWER-SITE-CONFIG"
CONFIG_KEYS: Tuple[str, ...] = ("default-output-root", "corpus-output-overrides", "default-root-corpora")

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
CORPUS_DIR = os.path.join(PACKAGE_DIR, "shipped")
FIXTURES_DIR = os.path.join(PACKAGE_DIR, "fixtures")
PROFILE_PATH = os.path.join(CORPUS_DIR, "reviewer-profile.lts")

# ---------------------------------------------------------------------------------------------- site config
UNSET_WORDS = ("", "none")          # the shipped REVIEWER-CONFIG says `none`: an empty `- key:` is a nested-block head
_ARROW = re.compile(r"\s*(?:->|→)\s*")


class Override(NamedTuple):
    """`<corpus-id>[:<path-prefix>] -> <root>`. `prefix` is relative to a root of the corpus ('' = whole corpus)."""
    corpus: str
    prefix: str
    root: str


class SiteConfig:
    """The parsed REVIEWER-SITE-CONFIG (or the shipped REVIEWER-CONFIG), plus HOW it was found.

    `status`: `ok` (exactly one node found and parsed) | `none` (zero found) | `ambiguous` (two or more) |
    `unreadable` (the lookup itself failed). Only `ok` supplies values. `ref` is `<corpus>:REVIEWER-SITE-CONFIG@<hash>`
    for an `ok` config and `none` otherwise (the literal the REVIEW node's `site-config` property carries).
    """

    def __init__(self, status: str = "none", ref: str = "none", default_output_root: Optional[str] = None,
                 default_root_corpora: Sequence[str] = (), overrides: Sequence[Override] = (),
                 unparsed_overrides: Sequence[str] = (), found: Sequence[str] = ()):
        self.status = status
        self.ref = ref
        self.default_output_root = default_output_root
        self.default_root_corpora = tuple(default_root_corpora)
        self.overrides = tuple(overrides)
        self.unparsed_overrides = tuple(unparsed_overrides)
        self.found = tuple(found)

    @classmethod
    def none(cls) -> "SiteConfig":
        return cls("none")

    @classmethod
    def ambiguous(cls, found: Sequence[str]) -> "SiteConfig":
        return cls("ambiguous", found=found)

    @classmethod
    def unreadable(cls) -> "SiteConfig":
        return cls("unreadable")


def _items(value: object) -> List[str]:
    """A property value as a list of strings: a nested list verbatim, an inline string comma-split; the word
    `none` (and empties) mean 'not set'."""
    if value is None:
        return []
    raw = list(value) if isinstance(value, (list, tuple)) else [p for p in str(value).split(",")]
    out = []
    for item in raw:
        t = str(item).strip()
        if t.lower() in UNSET_WORDS:
            continue
        out.append(t)
    return out


def parse_site_config(props: Dict[str, object], *, source: str) -> SiteConfig:
    """Project a config node's properties onto a `SiteConfig`. `source` is the ref string for the REVIEW node."""
    root_items = _items(props.get("default-output-root"))
    overrides: List[Override] = []
    bad: List[str] = []
    for item in _items(props.get("corpus-output-overrides")):
        parts = _ARROW.split(item, maxsplit=1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            bad.append(item)
            continue
        key, root = parts[0].strip(), parts[1].strip()
        corpus, _, prefix = key.partition(":")
        overrides.append(Override(corpus.strip(), prefix.strip(), root))
    return SiteConfig("ok", source, root_items[0] if root_items else None,
                      _items(props.get("default-root-corpora")), overrides, bad)


def lookup_site_config(find: Callable[..., dict]) -> SiteConfig:
    """Addendum C3(a): `find(prefix="REVIEWER-SITE-CONFIG")`, corpus-OMITTED (so private corpora are skipped by the
    engine's own fan-out rule), and EXACTLY ONE node must exist. Zero is `none`; two or more is `ambiguous`; a
    failed or raising lookup is `unreadable`. Every non-`ok` outcome supplies no value (fail closed)."""
    try:
        res = find(prefix=SITE_CONFIG_NODE)
    except Exception:                                    # noqa: BLE001 — an unreadable lookup fails closed
        return SiteConfig.unreadable()
    if not isinstance(res, dict) or not res.get("success"):
        return SiteConfig.unreadable()
    nodes = [n for n in (res.get("nodes") or []) if isinstance(n, dict) and n.get("id") == SITE_CONFIG_NODE]
    if not nodes:
        return SiteConfig.none()
    if len(nodes) > 1:
        return SiteConfig.ambiguous([f"{n.get('corpus')}:{SITE_CONFIG_NODE}" for n in nodes])
    n = nodes[0]
    return parse_site_config(n.get("properties") or {},
                             source=f"{n.get('corpus')}:{SITE_CONFIG_NODE}@{n.get('content_hash')}")


# ---------------------------------------------------------------------------------------------- folder names
def output_folder(corpus_id: Optional[str]) -> str:
    """Addendum C4(d): the corpus id AS GIVEN, sanitised to `[a-z0-9-]` (lower-cased; every other run of characters
    becomes one `-`; leading and trailing `-` dropped; at most 120 characters). Nothing usable (or no corpus
    given) is `_no-corpus`. A path-shaped id cannot escape the output root: `..` and `/` are not in the alphabet."""
    if not isinstance(corpus_id, str):
        return "_no-corpus"
    s = re.sub(r"[^a-z0-9]+", "-", corpus_id.lower()).strip("-")[:120].strip("-")
    return s or "_no-corpus"


# ---------------------------------------------------------------------------------------------- review key
# REVIEW-KEY-GRAMMAR (SDLC 7.6): `<CORPUSSLUG>-<YYYYMMDDTHHMMSSZ>-<4 hex>`, uppercase and id-grammar safe, carried by
# EVERY node of one review (`REVIEW-<KEY>`, `REVFINDING-<KEY>-<n>`, ...) so two reviews never collide (F9). Uniqueness
# is enforced where it matters: the file is `REVIEW-<KEY>.lts` and its creation is exclusive (A-E1), so a repeated key
# cannot silently share a file.
REVIEW_KEY_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*-[0-9]{8}T[0-9]{6}Z-[0-9A-F]{4}$")


def review_key(corpus_id: Optional[str], now=None, rand: Optional[int] = None) -> str:
    """A fresh review key. `now` (a datetime, UTC) and `rand` (0..65535) are test seams."""
    import datetime
    import secrets
    t = now or datetime.datetime.now(datetime.timezone.utc)
    slug = re.sub(r"[^A-Z0-9]+", "-", output_folder(corpus_id).upper()).strip("-") or "NO-CORPUS"
    r = secrets.randbelow(65536) if rand is None else int(rand) % 65536
    return f"{slug}-{t.strftime('%Y%m%dT%H%M%SZ')}-{r:04X}"


# ---------------------------------------------------------------------------------------------- resolution
TERMINAL_NOTE = "terminal_only is for the terminal only: NEVER persist it in a review document"

# ⛔ `reason` of a refused resolution is a CODE from this CLOSED table; the sentence beside it is `explanation`, and it is
# the table's own FIXED text: nothing is interpolated into it (no corpus id, no root, no count), so it is path-free by
# CONSTRUCTION, not by a pattern over free text. What an operator needs to see that is a path or a name goes in
# `terminal_only`. The table holds no slash, quote, brace or digit (tests/test_reviewer_reasons.py checks it).
RESOLVE_REFUSALS = {
    "ambiguous-site-config": "two or more REVIEWER-SITE-CONFIG nodes were found and exactly one may exist (their ids are in terminal_only)",
    "document-not-under-corpus-root": "the document is not under a root of the named corpus: routing trusts the location of the "
                                      "document, not the corpus id the caller gave (wrong corpus id, a symlink out of the corpus, "
                                      "or a path elsewhere), so it is not routed",
    "document-outside-override-prefix": "the document is not under a root of the corpus and the corpus has path-prefix overrides: "
                                        "it cannot be shown to be outside them, so it is not routed",
    "conflicting-overrides": "overrides for the corpus at the same prefix length name different output roots; a root is a path "
                             "and is not echoed",
    "profile-review-output-relative": "the review-output of the profile is relative and the profile has no directory",
    "override-root-not-absolute": "the override output root is not an absolute path",
    "profile-root-not-absolute": "the profile output root is not an absolute path",
    "default-root-not-absolute": "the default output root is not an absolute path",
    "override-root-inside-corpus": "the override output root lies inside the reviewed corpus (a review is never written into the corpus it reviews)",
    "profile-root-inside-corpus": "the profile output root lies inside the reviewed corpus (a review is never written into the corpus it reviews)",
    "default-root-inside-corpus": "the default output root lies inside the reviewed corpus (a review is never written into the corpus it reviews)",
    "private-corpus-needs-override": "the corpus is private to the engine and a private corpus never uses the default root: it needs "
                                    "an override or its own profile review-output",
    "corpus-not-on-default-root-list": "the corpus is not on default-root-corpora, and no override or profile review-output applies",
    "no-output-root-configured": "no override, no profile review-output and no default root applies (see site_config and site_overrides)",
}


class Resolution(tuple):
    """`(root, rule_used)` -- unpacks as the pair the addendum names -- plus what the reviewer must record.

    `root`: an absolute realpath, or None for `no-output-root`. `rule_used`: `override` | `profile` | `default` |
    `no-output-root`. Attributes: `reason` (why, always set when root is None), `folder` (the corpus sub-folder),
    `site_config` (`<corpus>:REVIEWER-SITE-CONFIG@<hash>` | `none`), `site_overrides` (`<n> loaded` | `MISSING`),
    `checks` (REVCHECK records the reviewer MUST write: `site-overrides-missing` when the default root was used
    and no override was loaded).
    """

    def __new__(cls, root: Optional[str], rule_used: str, **attrs) -> "Resolution":
        self = super().__new__(cls, (root, rule_used))
        self.reason = attrs.get("reason", "")
        self.explanation = attrs.get("explanation", "")
        self.terminal_only = dict(attrs.get("terminal_only") or {})
        self.folder = attrs.get("folder", "_no-corpus")
        self.site_config = attrs.get("site_config", "none")
        self.site_overrides = attrs.get("site_overrides", "MISSING")
        self.checks = list(attrs.get("checks", ()))
        return self

    @property
    def root(self) -> Optional[str]:
        return self[0]

    @property
    def rule_used(self) -> str:
        return self[1]

    def to_dict(self) -> dict:
        out = {"root": self.root, "rule_used": self.rule_used, "folder": self.folder,
               "directory": os.path.join(self.root, self.folder) if self.root else None,
               "reason": self.reason, "explanation": self.explanation, "site_config": self.site_config,
               "site_overrides": self.site_overrides, "checks": self.checks}
        if self.terminal_only:
            out["terminal_only"] = self.terminal_only
            out["terminal_only_note"] = TERMINAL_NOTE
        return out


def _spec_roots(registry_spec: Optional[dict]) -> List[str]:
    out: List[str] = []
    for r in (registry_spec or {}).get("roots") or []:
        p = r.get("path") if isinstance(r, dict) else r
        if p:
            out.append(os.path.realpath(os.path.expanduser(str(p))))
    return out


def corpora_containing(document: str, corpora: Dict[str, dict]) -> List[str]:
    """The ids of the registered corpora one of whose roots contains `document` (the engine's own containment test:
    realpath, case-fold and filesystem identity). Sorted; more than one means nested or overlapping roots."""
    from ..tools import _path_in_any_root
    real = os.path.realpath(os.path.expanduser(str(document)))
    return sorted(cid for cid, spec in (corpora or {}).items() if _path_in_any_root(real, _spec_roots(spec)))


def _fold(text: str) -> str:
    """FAIL-SAFE comparison form for an override's path prefix: NFC-normalised, then casefolded. An over-match
    can only route MORE documents to the restricted folder; an under-match leaks a restricted review into the general
    one. So case and Unicode normalisation (macOS hands back NFD names) never decide a miss."""
    return unicodedata.normalize("NFC", text).casefold()


def _components(path: str) -> List[str]:
    return [c for c in path.replace("\\", "/").split("/") if c not in ("", ".")]


def _doc_rel(document: Optional[str], roots: Sequence[str]) -> Optional[List[str]]:
    """The document's path components, folded, relative to the (first) root that contains it (compared on the
    realpath of both sides, folded); None if no root contains it."""
    if not document:
        return None
    d = _fold(os.path.realpath(os.path.expanduser(str(document))))
    for r in roots:
        fr = _fold(r)
        try:
            if os.path.commonpath([fr, d]) == fr:
                return [_fold(c) for c in _components(os.path.relpath(d, fr))]
        except ValueError:
            continue
    return None


def _inside_reviewed_corpus(candidate_dirs: Iterable[str], roots: Sequence[str]) -> bool:
    if not roots:
        return False
    from ..tools import _path_in_any_root    # the engine's own containment test: realpath, case-fold, st_ino ancestry
    return any(_path_in_any_root(c, roots) for c in candidate_dirs)


def resolve_output(corpus_id: Optional[str], document_realpath: Optional[str], registry_spec: Optional[dict],
                   profile, site_config: Optional[SiteConfig]) -> Resolution:
    """Addendum C3(c). Pure and read-only. Precedence, highest first:

      1. `site_config.overrides` keyed `corpus[:path-prefix]` (longest matching prefix wins; a corpus-wide key is the
         shortest; two matching keys of equal length with different roots are `conflicting`, which fails closed);
      2. the corpus profile's `review-output`;
      3. `site_config.default_output_root`, ONLY for a corpus on `default_root_corpora`, and NEVER for a corpus the
         engine treats as private (`privacy.private_reason`), whether or not it is listed.
      Otherwise `no-output-root` with a named `reason`. An ambiguous site config (two nodes) stops everything.

    Every chosen root must be absolute (after `~`), and neither it nor `<root>/<folder>` may lie inside a root of the
    reviewed corpus (realpath, case-folded and inode-ancestry compared, never a string prefix). A chosen root that
    fails a guard is `no-output-root`, NOT a fall-through to a lower rule: a guard that falls through is a guard
    the next rule can be used to defeat.
    """
    folder = output_folder(corpus_id)
    cfg = site_config if site_config is not None else SiteConfig.none()
    ok = cfg.status == "ok"
    ref = cfg.ref if ok else "none"
    n_over = len(cfg.overrides) if ok else 0
    over_text = f"{n_over} loaded" if n_over else "MISSING"
    roots = _spec_roots(registry_spec)

    def refuse(code: str, **terminal_only) -> Resolution:
        return Resolution(None, "no-output-root", reason=code, explanation=RESOLVE_REFUSALS[code], folder=folder,
                          site_config=ref, site_overrides=over_text, terminal_only=terminal_only)

    def accept(raw_root: str, rule: str) -> Resolution:
        r = os.path.expanduser(str(raw_root))
        if not os.path.isabs(r):
            return refuse(f"{rule}-root-not-absolute")
        real = os.path.realpath(r)
        if _inside_reviewed_corpus((real, os.path.join(real, folder)), roots):
            return refuse(f"{rule}-root-inside-corpus")
        checks = []
        if rule == "default" and not n_over:
            # `reason` is a CODE; the site-config ref (it carries a corpus id and a content hash) is its own field
            checks.append({"name": "site-overrides-missing", "outcome": "errored",
                           "reason": "default-root-used-without-overrides", "site_config": ref})
        return Resolution(real, rule, folder=folder, site_config=ref, site_overrides=over_text, checks=checks)

    if cfg.status == "ambiguous":
        return refuse("ambiguous-site-config", site_config_found=list(cfg.found))

    # P2-A: the document must lie under a root of the NAMED corpus. A restricted document passed under the wrong corpus id
    # (one with no overrides of its own) would otherwise take that corpus's default root.
    if registry_spec is not None and roots and document_realpath and _doc_rel(document_realpath, roots) is None:
        return refuse("document-not-under-corpus-root")
    # 1. overrides
    if ok and corpus_id is not None:
        rel = _doc_rel(document_realpath, roots)
        mine = [o for o in cfg.overrides if o.corpus == corpus_id]
        if rel is None and any(_components(o.prefix) for o in mine):
            # the document is not under any root of the corpus (a symlink out, a different spelling of a root, a
            # file elsewhere) and the corpus HAS path-prefix overrides: it cannot be shown NOT to be under one, so
            # it must not fall through to a lower rule (P1-1, P3-1)
            return refuse("document-outside-override-prefix")
        best, best_len = [], -1
        for o in cfg.overrides:
            if o.corpus != corpus_id:
                continue
            pre = [_fold(c) for c in _components(o.prefix)]
            if pre:
                if rel is None or rel[:len(pre)] != pre:
                    continue
            if len(pre) > best_len:
                best, best_len = [o], len(pre)
            elif len(pre) == best_len:
                best.append(o)
        if best:
            distinct = sorted({os.path.expanduser(o.root) for o in best})
            if len(distinct) > 1:
                return refuse("conflicting-overrides")
            return accept(best[0].root, "override")

    # 2. the corpus profile's review-output
    out_raw = getattr(profile, "review_output", None) if profile is not None else None
    if out_raw:
        resolved = profile.resolve_path(out_raw)
        if not resolved:
            return refuse("profile-review-output-relative")
        return accept(resolved, "profile")

    # 3. the default root, for listed and non-private corpora only
    is_private = bool(corpus_id) and private_reason(str(corpus_id), registry_spec) is not None
    listed = ok and corpus_id in cfg.default_root_corpora
    if ok and cfg.default_output_root:
        if is_private:
            return refuse("private-corpus-needs-override")
        if listed:
            return accept(cfg.default_output_root, "default")
        return refuse("corpus-not-on-default-root-list")
    return refuse("no-output-root-configured")


# ---------------------------------------------------------------------------------------------- the self-gate
SEVERITIES = ("info", "minor", "major", "blocker")
_SEVERITY_COUNTS = re.compile(r"(info|minor|major|blocker)=(\d+)")


def parse_severity_counts(text: object) -> Optional[Dict[str, int]]:
    """`info=0 minor=1 major=0 blocker=0` -> counts; None unless every severity appears exactly once."""
    found = _SEVERITY_COUNTS.findall(str(text))
    out = {k: int(v) for k, v in found}
    if len(found) != len(SEVERITIES) or set(out) != set(SEVERITIES):
        return None
    return out


def self_gate(review_path: str, call: Callable[[str, dict], dict], profile_path: Optional[str] = None) -> dict:
    """Spec section 4.5 and addendum C4(b), as CODE. `call(tool, arguments) -> result` is the engine door.

    The gate PASSES only if every one of these holds, each reported in `checks`:
      profile-available         the reviewer profile was in force for the lint (else every V-rule was inert);
      lint-errors-zero          lts_lint errors == 0;
      no-missing-required       ZERO V3 findings, although V3's severity is WARNING (F3): the gate counts them itself;
      no-lost-properties        lts_property_loss `totals.headline_property_lines_lost` == 0 and files_scanned >= 1;
      path-find-proof           the path find that read the nodes carried `file_source` and the one-file scope
                                (the stale-server guard: a server that ignores `path` answers across every corpus);
      severities-match          for a REVIEW (not an UNREVIEWED) document, its `findings-by-severity` equals the
                                REVFINDING nodes actually present;
      no-secret-in-review       no property value of the FINISHED review matches the secret detector (`scan_secrets`, the same
                                one that builds evidence); the gate names node, key and line, never the value;
      findings-have-loaded-rules  every REVFINDING's `rule` was LOADED in this run (a REVPROV of kind `rule` whose
                                `source` is that rule) with `rule-hash` equal to the REVPROV's `hash`, and has no
                                REVCHECK of outcome `errored` (it was validated), unless it is `engine:lts_lint:*`.
                                A finding with no loaded rule behind it fails the review.
    Orphan warnings are expected on review nodes (delta N2) and are not gated. Never raises: a tool that fails is a
    failed check.
    """
    prof = profile_path or PROFILE_PATH
    checks: List[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        lint = call("lts_lint", {"source": review_path, "dangle": True, "profile": prof})
    except Exception as exc:                              # noqa: BLE001
        lint = {"success": False, "error": type(exc).__name__}
    avail = bool(isinstance(lint, dict) and (lint.get("profile") or {}).get("available"))
    add("profile-available", avail, "" if avail else "the reviewer profile was not in force")
    lint_ok = isinstance(lint, dict) and lint.get("success") is True
    errors = _count(lint.get("summary") or {}, "errors") if lint_ok else None
    add("lint-errors-zero", lint_ok and errors == 0, f"errors={errors}")
    v3 = [f for f in (lint.get("findings") or []) if f.get("kind") == "missing_required_property"] \
        if isinstance(lint, dict) else []
    if isinstance(lint, dict) and lint.get("truncated"):
        add("no-missing-required", False, "the lint answer was truncated: the V3 count is not complete")
    else:
        add("no-missing-required", lint_ok and not v3,
            "; ".join(f"{f.get('node_id')} lacks {((f.get('detail') or {}).get('property'))}" for f in v3[:50]))
    try:
        loss = call("lts_property_loss", {"roots": [review_path]})
        tot = (loss.get("totals") or {}) if isinstance(loss, dict) else {}
        lost, scanned = tot.get("headline_property_lines_lost"), tot.get("files_scanned")
        add("no-lost-properties", isinstance(lost, int) and lost == 0 and isinstance(scanned, int) and scanned >= 1,
            f"lost={lost} files_scanned={scanned}")
    except Exception as exc:                              # noqa: BLE001
        add("no-lost-properties", False, type(exc).__name__)

    def path_find(prefix: str) -> Optional[dict]:
        try:
            r = call("lts_find", {"path": review_path, "prefix": prefix})
        except Exception:                                 # noqa: BLE001
            return None
        ok = (isinstance(r, dict) and r.get("success") is True and "file_source" in r
              and ((r.get("extent") or {}).get("scope") or {}).get("corpus") == "(one file, by path)")
        return r if ok else None

    review_nodes = path_find("REVIEW-")
    findings = path_find("REVFINDING-")
    unreviewed = path_find("UNREVIEWED-")
    provs = path_find("REVPROV-")
    rchecks = path_find("REVCHECK-")
    proof = all(x is not None for x in (review_nodes, findings, unreviewed, provs, rchecks))
    add("path-find-proof", proof, "" if proof else "a path find lacked file_source or the one-file scope "
                                                   "(a server that predates `path` ignores it)")
    if proof and review_nodes.get("nodes"):
        claimed = parse_severity_counts((review_nodes["nodes"][0].get("properties") or {}).get("findings-by-severity"))
        actual = {k: 0 for k in SEVERITIES}
        for n in findings.get("nodes") or []:
            sev = (n.get("properties") or {}).get("severity")
            if sev in actual:
                actual[sev] += 1
            else:
                actual.setdefault("unknown", 0)
                actual["unknown"] += 1
        add("severities-match", claimed is not None and claimed == actual,
            f"claimed={claimed} actual={actual}")
        loaded = {}
        for n in provs.get("nodes") or []:
            pp = n.get("properties") or {}
            if str(pp.get("kind")) == "rule":
                loaded[str(pp.get("source"))] = str(pp.get("hash"))
        errored = {str((n.get("properties") or {}).get("rule")) for n in rchecks.get("nodes") or []
                   if (n.get("properties") or {}).get("outcome") == "errored"}
        bad = []
        for n in findings.get("nodes") or []:
            pp = n.get("properties") or {}
            rule, rh = str(pp.get("rule")), str(pp.get("rule-hash"))
            if rule.startswith("engine:lts_lint"):
                continue
            if rule not in loaded:
                bad.append(f"{n.get('id')}: rule {rule} was never loaded in this run (no REVPROV of kind rule)")
            elif loaded[rule] != rh:
                bad.append(f"{n.get('id')}: rule-hash {rh} differs from the loaded rule's hash {loaded[rule]}")
            elif rule in errored:
                bad.append(f"{n.get('id')}: rule {rule} errored in validation and was not applied")
        add("findings-have-loaded-rules", not bad, "; ".join(bad[:20]))
    try:
        leaks = scan_secrets(review_path)
        add("no-secret-in-review", not leaks,
            "; ".join(f"{h['node']}.{h['key']} line {h['line']} ({h['reason']})" for h in leaks[:20]))
    except Exception as exc:                              # noqa: BLE001 -- a review that cannot be scanned does not pass
        add("no-secret-in-review", False, f"could not scan: {type(exc).__name__}")
    return {"passed": all(c["ok"] for c in checks), "checks": checks}


def check_extension_paths(ext_paths: Iterable[str], document_realpath: str) -> List[dict]:
    """v3 section 4.4: an extension path equal to the reviewed document is refused (compared by realpath, and by
    inode when both exist, so a symlink or a `..` spelling is the same file). Returns findings, `[]` if fine."""
    doc = os.path.realpath(os.path.expanduser(str(document_realpath)))
    out = []
    for p in ext_paths:
        real = os.path.realpath(os.path.expanduser(str(p)))
        same = real == doc
        if not same:
            try:
                same = os.path.samefile(real, doc)
            except OSError:
                same = False
        if same:
            out.append({"kind": "extension_is_the_reviewed_document", "path": str(p),
                        "message": f"extension path {p!r} is the document under review ({doc})"})
    return out


# ---------------------------------------------------------------------------------------------- P1-2: no-document stops
# THE COMPLETE, CLOSED LIST of stops that leave NO review document (the architect's ruling on the review's P1-2).
# `registry-not-found` is the SIXTH entry (SPEC-U1a Q-1, P1's literal words): no corpus registry resolves, so there is
# no corpus to review and no site configuration to say where a review would go. It is NOT folded into
# `no-output-root`, and the helpers exit 2 on it (the configuration class, as `call`).
# Every other stop writes an UNREVIEWED document. A new no-document stop cannot appear without changing this tuple,
# which `tests/test_reviewer_post_review.py` pins and which the definition and the procedure both quote.
NO_DOCUMENT_STOPS: Tuple[str, ...] = ("no-route", "no-output-root", "server-too-old", "no-corpus-given",
                                      "first-create-refused-or-unknown", "registry-not-found")
NO_DOCUMENT_MARKER = "NO-REVIEW-DOCUMENT"
_DETAIL_RE = re.compile(r"^[a-z0-9_.-]{1,80}$")


def no_document_reply(reason: str, detail: Optional[str] = None, inspect_path: Optional[str] = None) -> str:
    """The reply when NO document exists: `NO-REVIEW-DOCUMENT: <reason>[ (<detail>)]`, never path-shaped, so a caller
    cannot mistake it for the path a successful run returns. The ONE exception is the first-create stop, which carries
    the single path a person must inspect, labelled `inspect-path: <path>` (it MAY be partial or absent)."""
    if reason not in NO_DOCUMENT_STOPS:
        raise ValueError(f"{reason!r} is not a no-document stop; the closed list is {NO_DOCUMENT_STOPS}")
    if detail is not None and not _DETAIL_RE.match(str(detail)):
        raise ValueError("detail must be a short code ([a-z0-9_.-]), never prose or a path")
    first = reason == "first-create-refused-or-unknown"
    if first and not inspect_path:
        raise ValueError("the first-create stop must name the path a person has to inspect")
    if inspect_path is not None and not first:
        raise ValueError("only the first-create stop may carry a path")
    out = f"{NO_DOCUMENT_MARKER}: {reason}" + (f" ({detail})" if detail else "")
    if first:
        out += f" inspect-path: {inspect_path}"
    return out



# ---------------------------------------------------------------------------------------------- the registry check
# SPEC-U1a P2: a review records WHICH registry each route used, with its hash, and a mismatch stops the run.
# Pure and read-only. `mcp` is what the MCP server's `lts_list_corpora.registry_choice` said:
#   a dict            the server reported a choice (or, with an `error` key, that it is in registry-error mode)
#   None              the server did not report one: it predates the field ("server too old")
#   False             the MCP route was not used at all
def registry_value(source: object, path: object, sha256: object) -> str:
    """The one-line form PERSISTED on a REVIEW/UNREVIEWED: `<source> <file name> sha256:<hex>`. The path is reduced to
    its LAST component (a review lands in a committed corpus and the directory may be a private name); the hash stays
    full, and it is what the staleness and mismatch checks use. Terminal and MCP text keep full paths."""
    return f"{source} {os.path.basename(str(path).rstrip('/')) if path else '-'} sha256:{sha256}"


def _short(text: object, limit: int = 120) -> str:
    return " ".join(str(text).split())[:limit]


def compare_registries(mcp: object, cli: Dict[str, object]) -> Dict[str, object]:
    """`{registry_mcp, registry_cli, mcp_used, mismatch, stale, reason}`.

    * the CLI route is always used by the procedure's helper commands; its value is `{source, path, sha256}`;
    * MCP not used (recorded, never a mismatch): no MCP route; an OLD server (no `registry_choice`, R-9); a server
      in REGISTRY-ERROR MODE; a STALE server (`sha256_at_boot` != `sha256_now`: it serves an older registry than
      the file, R-8 -- its reads are not used this run and it is NOT a mismatch stop, so a legitimate runtime
      registration never wedges later reviews);
    * both used: {source, path, sha256_now} vs the CLI's {source, path, sha256}; any difference is a MISMATCH and
      `reason` is the code `registry-mismatch` and `explanation` a fixed sentence (the run stops, UNREVIEWED). ONLY sha256 decides; source and path are recorded.
    """
    reg_cli = registry_value(cli.get("source"), cli.get("path"), cli.get("sha256"))
    out: Dict[str, object] = {"registry_cli": reg_cli, "registry_mcp": "", "mcp_used": False,
                              "mismatch": False, "stale": False, "reason": None}
    if mcp is False:
        out["registry_mcp"] = "not-used (no MCP route)"
        return out
    if not isinstance(mcp, dict) or not ("error" in mcp or {"source", "sha256_now", "sha256_at_boot"} <= set(mcp)):
        out["registry_mcp"] = "not-used (server too old)"
        return out
    if "error" in mcp:
        out["registry_mcp"] = f"not-used (registry error mode: {_short(mcp.get('error_persist') or 'registry-not-found: no persisted detail from this server')})"
        return out
    boot, now = mcp.get("sha256_at_boot"), mcp.get("sha256_now")
    if boot != now:
        out["stale"] = True
        out["registry_mcp"] = (f"not-used (stale server: it loaded sha256:{str(boot)[:12]} and the registry now "
                               f"resolves to {'sha256:' + str(now)[:12] if now else 'nothing'}; restart it)")
        return out
    out["mcp_used"] = True
    out["registry_mcp"] = registry_value(mcp.get("source"), mcp.get("path"), now)
    # ⛔ THE DECISION IS THE HASH ALONE (round 3, P1-1). `source` and `path` are RECORDED for the audit trail and never
    # stop a run: the planned deployment has the MCP server on `env` and the shell on the pointer (so `source` always
    # differs), and a symlinked env path differs in `path`, while every reader resolves the same corpora. The hash
    # already encodes what matters (the bytes, and the cwd/root/HOME/profile dir when a relative path needs them).
    if now != cli.get("sha256"):
        out["mismatch"] = True
        out["reason"] = "registry-mismatch"
        out["explanation"] = ("the MCP route and the CLI route resolve different registries (sha256 differs); "
                              "registry_mcp and registry_cli above are the two values")
    return out


# ---------------------------------------------------------------------------------------------- P2-2: the tripwire
_REG_KEY = re.compile(r"""(?<![A-Za-z0-9_])ext[-_]id["']?\s*:""")


def count_registrations(profile_path: str) -> int:
    """How many review-extension registrations the RAW profile file declares, whatever its format: `- ext-id:` lines in
    a `.lts` profile, `ext_id` / `ext-id` keys in YAML and JSON. A raw text count on purpose: it must not go through
    the loader whose projection it exists to check."""
    from factlattice.hashing import safe_read_text      # a FIFO/socket/device is a CorpusFileError, never a block
    return len(_REG_KEY.findall(safe_read_text(profile_path, os.path.basename(profile_path), errors="replace")))


def check_registrations(profile_path: str, audit_answer: Optional[dict]) -> dict:
    """The stale-server tripwire (see REVPROC-CORE). `lts_profile_audit` OMITS `review_extensions` when nothing is
    registered, so a server that predates the section looks exactly like "no extensions". Compare what the file
    declares with what the answer reports; the answer reporting FEWER is a stop, `registrations-not-projected`."""
    raw = count_registrations(profile_path)
    sec = (audit_answer or {}).get("review_extensions") if isinstance(audit_answer, dict) else None
    reported = int(sec.get("registered_count") or 0) if isinstance(sec, dict) else 0
    ok = reported >= raw
    out = {"ok": ok, "raw_count": raw, "reported": reported, "reason": ""}
    if not ok:
        # a CODE (the stop-reason the reviewer WRITES is `extension-invalid:` + this code); the counts are the fields above
        out["reason"] = "registrations-not-projected"
        out["explanation"] = ("the profile file declares more registrations than the audit answer reports"
                              + ("" if isinstance(sec, dict) else " (the answer has no review_extensions section at all)"))
    return out


# ---------------------------------------------------------------------------------------------- S2: lint groups, ceiling
LINT_EXAMPLES = 5
REVIEW_NODE_CEILING = 250        # review nodes written besides the REVIEW root: findings + checks + provenance
_NOTE_LIMIT = 160


def _one_line(text: object, limit: int = _NOTE_LIMIT) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()[:limit]


def _count(summary: dict, name: str) -> int:
    """`summary.errors` / `.warnings`, or the `*_observed` figure the lint reports instead when profile rules were
    inert (then the first is None, and a bare `or 0` would read "no errors")."""
    v = summary.get(name)
    return int(v if isinstance(v, int) else summary.get(name + "_observed") or 0)


def _lint_pages(path: str, call: Callable[[str, dict], dict], profile: Optional[str] = None) -> Tuple[List[dict], dict, bool]:
    """Every finding of `lts_lint(source=path, dangle=true)`, paged to the end. `(findings, summary, complete)`:
    `complete` is False if any page could not be read or the cursor never reached the end."""
    found: List[dict] = []
    summary: dict = {}
    offset, complete = 0, True
    for _ in range(500):
        args = {"source": path, "dangle": True, "offset": offset, "token_budget": 200000}
        if profile:
            args["profile"] = profile
        r = call("lts_lint", args)
        if not isinstance(r, dict) or r.get("success") is not True:
            complete = False
            break
        summary = r.get("summary") or summary
        found.extend(r.get("findings") or [])
        total = r.get("total_matched", len(found))
        if not r.get("truncated"):
            break
        offset += int(r.get("count") or 0)
        if offset >= total or not r.get("count"):
            break
    else:
        complete = False
    return found, summary, complete


def lint_groups(path: str, call: Callable[[str, dict], dict], examples: int = LINT_EXAMPLES) -> dict:
    """Lint warnings as ONE finding per warning kind (S2), with the TRUE count and the first `examples` instances,
    instead of one node per warning (a real decision graph measured 2,455 warnings). Errors are not grouped: they
    stop the review and are reported as a count. `complete` is False if the lint answer could not be read to the end,
    and the caller must then treat the structure gate as failed."""
    found, summary, complete = _lint_pages(path, call)
    warns = [f for f in found if f.get("severity") == "warning"]
    groups: Dict[tuple, dict] = {}
    for f in warns:
        g = groups.setdefault((f.get("origin"), f.get("kind")), {
            "origin": f.get("origin"), "kind": f.get("kind"), "severity": "warning", "count": 0, "examples": []})
        g["count"] += 1
        if len(g["examples"]) < examples:
            msg = str(f.get("message", ""))
            g["examples"].append({"node_id": f.get("node_id"), "source_line": f.get("source_line"),
                                  "message": "(message withheld)" if secret_reason(msg) else _one_line(msg)})
    glist = sorted(groups.values(), key=lambda g: (-g["count"], str(g["origin"]), str(g["kind"])))
    findings = []
    for g in glist:
        ex = "; ".join(f"{e['node_id']}@{e['source_line']}: {e['message']}" for e in g["examples"])
        first = g["examples"][0] if g["examples"] else {}
        findings.append({"rule": f"engine:lts_lint:{g['origin']}.{g['kind']}", "rule-hash": "none",
                         "rule-reliability": "validated", "kind": "lint-warning",
                         "severity": "info" if g["kind"] == "orphan" else "minor", "count": g["count"],
                         "about_node": first.get("node_id"),
                         "evidence_lines": f"{first.get('source_line')}-{first.get('source_line')}",
                         "evidence": _one_line(f"{g['count']} lint warnings of kind {g['origin']}.{g['kind']}; "
                                               f"first {len(g['examples'])}: {ex}", 1200)})
    errors = _count(summary, "errors")
    claimed = _count(summary, "warnings")
    return {"errors": errors, "warnings": len(warns), "groups": glist, "findings": findings,
            "complete": complete and (len(warns) == claimed)}


UNVERIFIABLE_PRIVATE = "unverifiable-private"     # the status `resolve` gives a reference into a private corpus
DANGLE_LIST_LIMIT = 50           # items listed per section; the true count is always stated beside them


def dangle_check(path: str, call: Callable[[str, dict], dict], resolve: Callable[[str, Optional[str]], Tuple[bool, str]],
                 profile: Optional[str] = None, limit: int = DANGLE_LIST_LIMIT) -> dict:
    """The structure gate's lint at DOCUMENT scope, with each forward dangle resolved against the CORPUS.

    A file-scope lint cannot see sibling files, so it reports every reference into one as a forward dangle (measured on a
    real market document: 9 errors to 6 targets, all resolving in the corpus). `resolve(target, target_domain)` is the
    ENGINE's own resolution (`Partition.get_node`, or the router for a domain-qualified ref) and returns
    `(resolved, status)` (status `unverifiable-private` for a reference into a private corpus, which is neither); this function never re-derives an id or a domain. Only a `forward` finding of origin `dangle` is
    resolved; every other error is returned unchanged in `other_errors`. `complete` is False if the lint could not be read to
    the end or its error count disagrees with the findings read; the caller must then treat the gate as failed."""
    found, summary, complete = _lint_pages(path, call, profile)
    errors = [f for f in found if f.get("severity") == "error"]
    true_dangles: List[dict] = []
    elsewhere_targets: set = set()
    elsewhere = 0
    private_targets: set = set()
    private_domains: set = set()
    private_n = 0
    other: List[dict] = []
    for f in errors:
        if f.get("origin") == "dangle" and f.get("kind") == "forward":
            det = f.get("detail") or {}
            target = str(det.get("target") or f.get("node_id") or "")
            domain = det.get("target_domain") or None
            ok, status = resolve(target, domain)
            if status == UNVERIFIABLE_PRIVATE:
                # the reviewer does not read a private corpus, so it can neither confirm nor refute this reference: it is
                # counted apart (never as resolved, never as a dangle) and only the authored domain suffix is kept
                private_n += 1
                private_targets.add(target)
                if domain:
                    private_domains.add(domain)
            elif ok:
                elsewhere += 1
                elsewhere_targets.add(target)
            else:
                true_dangles.append({"node": det.get("source"), "target": target, "target_domain": domain,
                                     "line": f.get("source_line"), "status": status})
        else:
            msg = str(f.get("message", ""))
            other.append({"origin": f.get("origin"), "kind": f.get("kind"), "node_id": f.get("node_id"),
                          "source_line": f.get("source_line"),
                          "message": "(message withheld)" if secret_reason(msg) else _one_line(msg)})
    return {"true_dangle_count": len(true_dangles), "true_dangles": true_dangles[:limit],
            "resolved_elsewhere": {"count": elsewhere, "target_count": len(elsewhere_targets),
                                   "targets": sorted(elsewhere_targets)[:limit]},
            "unverifiable_private": {"count": private_n, "target_count": len(private_targets),
                                     "targets": sorted(private_targets)[:limit], "domains": sorted(private_domains)[:limit]},
            "other_error_count": len(other), "other_errors": other[:limit], "list_limit": limit,
            "complete": complete and len(errors) == _count(summary, "errors")}


def plan_findings(findings: Sequence[dict], other_nodes: int = 0, ceiling: int = REVIEW_NODE_CEILING) -> dict:
    """Apply the review's NODE CEILING (S2). Findings are kept worst-severity first (stable within a severity); the
    remainder is counted, by severity, and `not_examined` carries the sentence for the review's `not-examined`."""
    rank = {s: i for i, s in enumerate(reversed(SEVERITIES))}
    ordered = sorted(enumerate(findings), key=lambda t: (rank.get(t[1].get("severity"), 99), t[0]))
    room = max(0, int(ceiling) - int(other_nodes))
    keep = [f for _, f in ordered[:room]]
    drop = [f for _, f in ordered[room:]]
    by: Dict[str, int] = {}
    for f in drop:
        by[f.get("severity")] = by.get(f.get("severity"), 0) + 1
    return {"write": keep, "omitted": {"count": len(drop), "by_severity": by},
            "node_ceiling": ceiling,
            "not_examined": (f"{len(drop)} findings were not written because the review node ceiling of {ceiling} was "
                             f"reached ({', '.join(f'{k}={v}' for k, v in sorted(by.items()))})") if drop else ""}


# ---------------------------------------------------------------------------------------------- S3: the frame ceiling
FRAME_NODE_CEILING = 200
FRAME_TOKEN_CEILING = 60000


def plan_frame(nodes: Sequence[dict], node_ceiling: int = FRAME_NODE_CEILING, token_ceiling: int = FRAME_TOKEN_CEILING,
               exclude_real_sources: Sequence[str] = ()) -> dict:
    """What a review may READ (S3). Nodes of registered extension documents are excluded by SOURCE FILE realpath
    (`real_source`), never by prefix. The rest are read in DOCUMENT ORDER (source line, then id) until the node or
    token ceiling would be passed. Anything unread is counted, and when there is any, `verdict_cap` is `inconclusive`:
    a verdict never covers nodes that were not read."""
    ex = {os.path.realpath(p) for p in exclude_real_sources}
    keep = [n for n in nodes if not (n.get("real_source") and os.path.realpath(n["real_source"]) in ex)]
    excluded = len(nodes) - len(keep)
    keep.sort(key=lambda n: ((n.get("line_range") or [0])[0], str(n.get("id"))))
    read, tokens = [], 0
    for n in keep:
        t = int(n.get("token_count") or 0)
        if len(read) >= node_ceiling or tokens + t > token_ceiling:
            break
        read.append(n["id"])
        tokens += t
    unread = [n["id"] for n in keep[len(read):]]
    total_tokens = sum(int(n.get("token_count") or 0) for n in keep)
    exceeded = bool(unread)
    return {"read": read, "unread_count": len(unread), "unread_ids": unread[:50], "exceeded": exceeded,
            "verdict_cap": "inconclusive" if exceeded else None, "excluded_count": excluded,
            "nodes_total": len(keep), "tokens_total": total_tokens, "tokens_read": tokens,
            "node_ceiling": node_ceiling, "token_ceiling": token_ceiling,
            "order": "document order (source line ascending, then id)",
            "not_examined": (f"{len(unread)} of {len(keep)} frame nodes were not read: the frame ceiling is "
                             f"{node_ceiling} nodes or {token_ceiling} tokens, read in document order") if exceeded else ""}


# ---------------------------------------------------------------------------------------------- S4: the verdict
def review_verdict_inputs(path: str) -> dict:
    """The `verdict` command's inputs, built from the review FILE (not by hand): every REVFINDING's `{rule, severity}`,
    every REVCHECK's `{rule, outcome}`, and `other_nodes` = the REVCHECK and REVPROV nodes written (what shares the node
    ceiling with the findings)."""
    from factlattice.parser import parse_file
    nodes = parse_file(path).nodes
    pick = lambda n, k: (n.properties or {}).get(k)                                    # noqa: E731
    return {"findings": [{"rule": pick(n, "rule"), "severity": pick(n, "severity")} for n in nodes if n.id.startswith("REVFINDING-")],
            "checks": [{"rule": pick(n, "rule"), "outcome": pick(n, "outcome")} for n in nodes if n.id.startswith("REVCHECK-")],
            "other_nodes": sum(1 for n in nodes if n.id.startswith(("REVCHECK-", "REVPROV-")))}


# Checks about the SITE CONFIGURATION, not about a rule: recorded as REVCHECKs (C3b's visibility, and the `site-overrides`
# property) but outside the verdict. A customer with no overrides by design must not get `inconclusive` on every review.
CONFIG_CHECKS: Tuple[str, ...] = ("site-overrides-missing",)


def decide_verdict(findings: Sequence[dict], checks: Sequence[dict], frame_exceeded: bool = False,
                   omitted: int = 0) -> dict:
    """The verdict rule, as code (S3, S4 and the base rule).

      * an UNRELIABLE rule that produced no finding adds ONE info finding ("failed its own fixture this run"), so a
        trusted-nothing review is never a clean `pass` (S4b);
      * `fail` if any finding is major or blocker, REGARDLESS of errored rules or ceilings: a found defect is certain
        and an errored rule or an over-ceiling frame cannot un-find it (the architect's ruling; a fail is not a pass);
      * else `inconclusive` if ANY check about a rule is `errored`, whatever its rule-id form (a bare `GEN-3`, `<corpus>:REVRULE-..`,
        `<ext-id>:REVRULE-..`, `path-find-proof`) (S4a); the site-configuration checks in `CONFIG_CHECKS` are recorded but
        never decide the verdict, or the
        frame was over its ceiling (S3): a pass never hides an untrusted rule or an unread node;
      * else `pass-with-findings` if any finding, else `pass`.
    ORDER: fail > inconclusive > pass-with-findings > pass. `reasons` lists the incompleteness even beside a `fail`,
    and `would_be` is the verdict the findings alone give.
    `checks` are `{rule, outcome}`. The label of a rule is `<corpus>:REVRULE-...` (and `<ext-id>:REVRULE-...` for an
    extension outside any corpus), but this function does NOT depend on the label: every `errored` check counts."""
    added = []
    found_rules = {f.get("rule") for f in findings}
    for c in checks:
        if c.get("outcome") == "unreliable" and c.get("rule") not in found_rules:
            added.append({"rule": c["rule"], "kind": "unreliable-rule-no-findings", "severity": "info",
                          "rule-reliability": "unreliable-rule",
                          "evidence": f"rule {c['rule']} failed its own fixture this run and found nothing; "
                                      "its silence is not evidence of absence"})
            found_rules.add(c["rule"])
    allf = list(findings) + added
    counts = {s: 0 for s in SEVERITIES}
    for f in allf:
        if f.get("severity") in counts:
            counts[f["severity"]] += 1
    if counts["major"] or counts["blocker"]:
        base = "fail"
    elif allf:
        base = "pass-with-findings"
    else:
        base = "pass"
    reasons = []
    errored = sorted({str(c.get("rule")) for c in checks
                      if c.get("outcome") == "errored" and str(c.get("rule")) not in CONFIG_CHECKS})
    if errored:                         # EVERY errored check about a rule, whatever its label form; not site-config checks
        reasons.append("a check errored: " + ", ".join(errored))
    if frame_exceeded:
        reasons.append("the frame was over its ceiling: some nodes were not read")
    verdict = base if base == "fail" else ("inconclusive" if reasons else base)
    return {"verdict": verdict, "would_be": base, "reasons": reasons, "added_findings": added,
            "findings_by_severity": " ".join(f"{k}={counts[k]}" for k in SEVERITIES), "omitted": int(omitted)}


# ---------------------------------------------------------------------------------------------- S5: secrets never quoted
_KEY_WORDS = {"authorization", "password", "passwd", "pwd", "passphrase", "secret", "secrets", "token", "tokens", "apikey", "credential",
              "credentials", "passcode"}
_KEY_PAIRS = {("api", "key"), ("private", "key"), ("customer", "key"), ("auth", "key"), ("access", "key"),
              ("client", "secret"), ("secret", "key"), ("signing", "key"), ("encryption", "key")}


def _sensitive_key(key: str) -> bool:
    """Whole-segment match on the key name (`how-it-was-bypassed-anyway` is not a `pass` key), plus ANY key with a
    segment starting `licen` (licence / license), whose value may be licence text."""
    segs = [x for x in re.split(r"[-_ .]+", str(key).lower()) if x]
    if any(x in _KEY_WORDS or x.startswith("licen") for x in segs):
        return True
    return any((segs[i], segs[i + 1]) in _KEY_PAIRS for i in range(len(segs) - 1))


_KW = r"(?:pass(?:word|wd|phrase)?|pwd|secret|token|api[-_ ]?key|credential|private[-_ ]?key|client[-_]?secret|access[-_]?key|auth[-_]?key)"
_SECRET_PATTERNS = (
    ("pem-block", re.compile(r"-----BEGIN [A-Z0-9 ]+-----")),
    ("aws-access-key", re.compile(r"(?<![A-Z0-9])(AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")),
    ("stripe-key", re.compile(r"(?<![A-Za-z0-9])[sr]k_(?:live|test)_[A-Za-z0-9]{8,}")),
    ("api-token-prefix", re.compile(r"(?<![A-Za-z0-9])(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                                    r"xox[abprs]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|npm_[A-Za-z0-9]{30,})")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    # ANY Authorization value, in any form (header line, JSON or dict key, `=` kwarg), whatever the token length
    ("authorization-header", re.compile(r"(?i)(?<![A-Za-z0-9])authorization[\"']?\s*[:=]\s*[\"']?[^\s\"',}]")),
    ("bearer-token", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("url-userinfo-password", re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")),
    ("azure-key", re.compile(r"(?i)\b(?:AccountKey|SharedAccessKey|SharedAccessSignature)\s*=\s*[A-Za-z0-9+/=%-]{16,}")),
    ("credential-assignment", re.compile(r"(?i)(?<![A-Za-z0-9])" + _KW + r"[\"']?\s*[:=]\s*[\"']?[^\s\"',;]{6,}")),
    ("licence-text", re.compile(r"(?i)\blicen[cs]e[-_ ]?(?:key|code|text)\b\s*[:=]\s*\S{6,}|GNU (?:LESSER |AFFERO )?GENERAL PUBLIC LICENSE|"
                                r"Permission is hereby granted, free of charge|Licensed under the Apache License|"
                                r"THE SOFTWARE IS PROVIDED \"AS IS\"")),
)
_ID_SHAPED = re.compile(r"^(?:[A-Z][A-Z0-9_-]+|[a-z0-9][a-z0-9._-]*(?::[A-Z][A-Z0-9_-]+)+|[A-Z][A-Z0-9_-]+(?::[A-Z][A-Z0-9_-]+)+)$")   # NODE-ID, CORPUS:NODE-ID, corp-id:NODE-ID
_GIT_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_SERIAL = re.compile(r"^(?:[A-Z0-9]{4,6}-){3,}[A-Z0-9]{4,6}$")
_TOKEN = re.compile(r"[A-Za-z0-9+/_=.~:-]{19,}")
_HEX = re.compile(r"^[0-9a-fA-F]+$")


def _pathlike(tok: str) -> bool:
    """Only a value that STARTS like a path (`/`, `~/` or `./`) and carries no `+` and no `=` padding. A random base64
    string that merely contains `/` is NOT a path (the review measured 9.6% of such secrets passing as paths)."""
    if not (tok.startswith(("/", "~/", "./")) and "+" not in tok and "=" not in tok):
        return False
    # a random base64 string can START with `/`: a real path has depth or a file extension
    segs = [x for x in re.split(r"/", tok.lstrip("~.")) if x]
    wordy = [x for x in segs if re.fullmatch(r"[a-z0-9._-]{3,}|[A-Z][a-z0-9._-]+", x)]
    return len(segs) >= 2 and len(wordy) * 10 >= len(segs) * 6


_GIT_CONTEXT_KEYS = {"commit", "sha", "sha1", "head", "rev", "revision", "tree", "git-sha", "commit-sha", "gitsha"}
_GIT_CONTEXT_TEXT = re.compile(r"(?i)(?<![A-Za-z0-9])(?:commit|sha1?|head|rev(?:ision)?|tree)(?:[-_ ]?sha)?(?:\s+(?:is|was|at|of))?[\s:=#@(\[]*$")


def _names_a_commit(key: Optional[str], before: str) -> bool:
    """Does the key, or the text just before the value, NAME it a commit / sha / head / rev / tree?"""
    if key:
        k = re.sub(r"([a-z])([A-Z])", r"\1-\2", str(key)).lower()
        if k in _GIT_CONTEXT_KEYS or any(seg in _GIT_CONTEXT_KEYS for seg in re.split(r"[-_ ]+", k)):
            return True
    return bool(_GIT_CONTEXT_TEXT.search(before[-24:]))


def secret_reason(text: object, key: Optional[str] = None) -> Optional[str]:
    """Why `text` (a property value or a line of body) must NOT be quoted, or None.

    Over-inclusive on SHAPES that carry credentials (PEM blocks of any kind, cloud and payment keys, tokens, URL
    userinfo passwords, Authorization headers, `pwd=`/`secret=`/`client_secret` assignments, licence text and any value
    under a key naming a secret or a licence), and deliberately NOT withholding what only LOOKS long: node ids and
    qualified ids (`^[A-Z][A-Z0-9_-]+(:[A-Z]...)*$`), git SHAs (7 to 40 hex), `sha256`-prefixed hashes and filesystem
    paths. STATED LIMIT: prose passwords ("the password is hunter2") and secrets in unusual shapes are not detected; the
    detector is a net, not a proof."""
    t = str(text)
    if key and _sensitive_key(key) and t.strip():
        return "sensitive-key-name"
    for name, rx in _SECRET_PATTERNS:
        if rx.search(t):
            return name
    for m in _TOKEN.finditer(t):
        raw = m.group(0)
        tok = raw.strip(".:~-")
        if _SERIAL.match(tok):
            if sum(1 for seg in tok.split("-") if re.search(r"[0-9]", seg)) >= 2:
                return "serial-pattern"
            continue
        if len(tok) < 32:
            continue
        if _ID_SHAPED.match(tok) or _pathlike(tok):
            continue
        if _HEX.match(tok):
            before = t[:m.start()]
            if before.endswith("sha256:"):
                continue                                   # the one prefixed form every review writes
            if len(tok) == 40 and _names_a_commit(key, before):
                continue                                   # a git commit id, named as one
            return "long-hex"                              # any other 32+ hex string: withheld (fail safe)
        if re.search(r"[a-z]", tok) and re.search(r"[A-Z]", tok) and re.search(r"[0-9+/]", tok) \
                and not re.search(r"[A-Za-z]{4,}-[A-Za-z]{4,}-[A-Za-z]{4,}", tok):
            return "long-opaque-string"
    return None


def scan_secrets(path: str) -> List[dict]:
    """GEN-7: every line of the document that the S5 detector would withhold, as `{node, key, line, reason}`. NEVER the
    value. A bullet `- key: value` is judged with its key; any other line as body text."""
    from factlattice.hashing import safe_read_text
    lines = safe_read_text(path, os.path.basename(path), errors="replace").splitlines()
    from factlattice.parser import parse_file
    spans = sorted(((n.line_range[0], n.id) for n in parse_file(path).nodes if n.line_range), key=lambda x: x[0])
    hits = []
    in_front = lines[:1] == ["---"]
    for i, line in enumerate(lines, 1):
        if in_front:
            if i > 1 and line.strip() == "---":
                in_front = False
            continue
        m = re.match(r"^\s*[-*]\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$", line)
        key, val = (m.group(1), m.group(2)) if m else (None, line)
        why = secret_reason(val, key) or (secret_reason(line) if m else None)
        if why:
            node = next((nid for start, nid in reversed(spans) if start <= i), None)
            hits.append({"node": node, "key": key, "line": i, "reason": why})
    return hits


# ---------------------------------------------------------------------------------------------- GEN-6
# GEN-6 is an AID TO A HUMAN READER, NOT A CONTROL. It flags text in the reviewed document that addresses the REVIEW
# ITSELF (the reviewer, the verdict or a pass, the review's rules, where the review is written, what to reply to the
# caller) so a person reading the review can see it. It does not protect the reviewer: the control is the definition's
# data-not-instructions line. It deliberately does NOT target instructions to a reader in general ("you must", "never",
# "stop and report" in a procedure are not hits). Its severity is this ONE constant. MEASURED on real procedure-heavy
# files it still fires on ordinary procedure prose (12 hits in one operating-procedures file, 3 in a worker
# definition, all false positives), so it is `info`: a GEN-6 hit alone can only yield pass-with-findings. Do not
# narrow it with more phrase heuristics.
GEN6_SEVERITY = "info"
_G6 = (
    r"\breviewers?\s*[:,]",                                                         # "Reviewer: ...", "Reviewer, ..."
    r"\b(?:note|message|attention|instructions?|dear|hello|hi)\s+(?:to|for)?\s*(?:the\s+|any\s+)?(?:automated\s+|ai\s+)?reviewers?\b",
    r"\bto\s+(?:the|any)\s+(?:automated\s+|ai\s+)?reviewers?\b",
    r"\b(?:you\s+are|as)\s+(?:the|a|an)\s+(?:automated\s+|ai\s+)?(?:reviewers?|review\s+agents?)\b",
    r"\bif\s+you\s+are\s+(?:a|an|the)\s+(?:reviewers?|review\s+agents?|ai|llm|language\s+model|assistant)\b",
    r"\bthe\s+reviewers?\s+(?:should|must|shall|will|needs?\s+to|has\s+to)\b",
    r"\b(?:record|give|assign|set|return|issue|mark|grant|award)\s+(?:this\s+|it\s+|the\s+)?(?:review\s+|document\s+)?(?:as\s+)?(?:a\s+|an\s+)?(?:pass(?:ed)?|approved|verdict)\b",
    r"\bverdict\s*(?:of|:|=|is|to|must\s+be|should\s+be)\s*(?:pass|fail|approved)",
    r"\b(?:must|should|shall)\s+(?:receive|get|be\s+given)\s+a\s+pass\b",
    r"\bapprove\s+this\s+(?:document|review|file|spec)\b",
    r"\b(?:skip|ignore|disregard|omit|bypass|suppress)\s+(?:the\s+|all\s+|any\s+|these\s+)?(?:rules?|checks?|revrule[-\w]*|findings?|previous\s+instructions|prior\s+instructions|earlier\s+instructions)\b",
    r"\bignore\s+(?:all\s+)?(?:previous|prior|earlier|above)\s+instructions\b",
    r"\bdo\s+not\s+(?:apply|run|flag|report|raise)\s+(?:the\s+|any\s+)?(?:rules?|checks?|findings?)\b",
    r"\b(?:write|save|put|store|output|send)\s+(?:your|the)\s+(?:review|output|report|findings)\s+(?:to|in|at|into)\b",
    r"\bwrite\s+(?:your|the)\s+review\b",
    r"\b(?:reply|respond|answer)\s+(?:to\s+the\s+caller\s+)?with\b.{0,40}\b(?:verdict|pass|approved)\b",
    r"\btell\s+the\s+caller\b",
    r"\bin\s+your\s+(?:reply|response|answer)\b",
)
_G6_RX = re.compile("|".join(f"(?:{p})" for p in _G6), re.I)


def gen6_scan(path: str) -> dict:
    """GEN-6: the lines of the reviewed document, BODY PROSE INCLUDED, that address the review itself. Returns
    `{count, severity, hits: [{line, text}]}`. A hit line that matches the secret detector is not printed."""
    from factlattice.hashing import safe_read_text
    lines = safe_read_text(path, os.path.basename(path), errors="replace").splitlines()
    hits = []
    for i, line in enumerate(lines, 1):
        if _G6_RX.search(line):
            hits.append({"line": i, "text": "(line withheld: it matches the secret detector)" if secret_reason(line)
                         else _one_line(line, 200)})
    return {"count": len(hits), "severity": GEN6_SEVERITY, "hits": hits}


def evidence(node: str, key: str, line: object, text: object) -> dict:
    """The ONLY way a finding's `evidence` is built. A multi-line quotation is joined with ` ⏎ `; a value that
    looks like a credential, key, token or licence text is NEVER quoted: the evidence names the node, the key and the
    line and says the value was withheld (S5)."""
    why = secret_reason(text, key)
    if why:
        return {"withheld": True, "reason": why,
                "evidence": f"value withheld (looks like {why}): node {node}, key {key}, line {line}"}
    parts = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    return {"withheld": False, "reason": None, "evidence": " ⏎ ".join(parts)}


__all__ = ["HOOK_CONTRACT_VERSION", "REVIEW_FORMAT_VERSION", "CORE_HOOKS", "CORPUS_ID", "CONFIG_KEYS",
           "Override", "SiteConfig", "Resolution", "parse_site_config", "lookup_site_config", "output_folder",
           "resolve_output", "check_extension_paths", "review_key", "REVIEW_KEY_RE", "self_gate",
           "parse_severity_counts", "SEVERITIES", "NO_DOCUMENT_STOPS", "NO_DOCUMENT_MARKER", "no_document_reply",
           "compare_registries", "registry_value",
           "count_registrations", "check_registrations", "scan_secrets", "gen6_scan", "GEN6_SEVERITY", "review_verdict_inputs", "lint_groups", "plan_findings", "plan_frame", "decide_verdict",
           "secret_reason", "evidence", "LINT_EXAMPLES", "REVIEW_NODE_CEILING", "FRAME_NODE_CEILING", "FRAME_TOKEN_CEILING", "PACKAGE_DIR", "CORPUS_DIR", "FIXTURES_DIR", "PROFILE_PATH"]
