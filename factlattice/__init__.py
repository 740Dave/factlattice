"""factlattice — the standalone, deterministic FactLattice (`.lts`) parser.

ONE parser, N consumers (library contract §7). This package is the format-core
(`parser.py`, `index_builder.py`, `hashing.py`, `constants.py`, `build.py`), wrapped
by the §7 library API (`parse()` -> `ParsedCorpus`, with first-class `diagnostics`
and `.to_graph_index()`).

The single-core guarantee: a spec that LINTs clean here is a spec the query engine
INGESTS identically — because both run this exact core. No parsing/graph rule is
re-implemented in the wrapper; it composes the core and unifies its scattered
preserve-and-flag signals into the contract §3 taxonomy.

Primary entry point:

    import factlattice
    corpus = factlattice.parse("path/to/spec/corpus", profile=None)   # auto-discovers a profile
    corpus.diagnostics        # first-class §3 taxonomy (linter consumes THIS + .nodes/.edges)
    corpus.to_graph_index()   # the §4 graph_index.json dict (lts-engine's serialization)

Consume the LIBRARY/AST + diagnostics directly (contract §7): do NOT round-trip a linter
through `graph_index.json` — that is a lossy re-parse that can't carry diagnostics richly.
"""

from __future__ import annotations

# --- the format core (the one shared parser) ---
from .constants import FACTLATTICE_PROFILE_VERSION, GENERATOR
from .hashing import (CORPUS_FILE_CODES, CorpusFileError, content_hash, file_content_hash, files_hash,
                      list_lts_files)
from .index_builder import build_index, compute_snapshot_hash, write_index_atomic
from .parser import (
    CANONICAL_LAYERS,
    CORE_EDGE_LAYER,
    DEFAULT_REGISTERED_NS,
    ID_TABLE_PROPERTY,
    PROFILE_DOCUMENT_TAG,
    Edge,
    LineCoverage,
    Node,
    ParsedFile,
    is_profile_document,
    parse_file,
    parse_text,
    token_estimate,
)

# --- [FL-LINE-ACCOUNTING] the silent-drop invariant (side-output + classifier) ---
from .coverage import (
    IGNORABLE_KINDS,
    UNACCOUNTED_KINDS,
    LineVerdict,
    account_file,
    account_summary,
    classify_line,
    unaccounted_lines,
)

# --- net-new §7 wrapper + §5 profile hook ---
from .corpus import (
    CONTRACT_DIAGNOSTIC_CLASSES,
    Diagnostic,
    ParsedCorpus,
    parse,
)
from .profile import (
    DEFAULT_PROFILE,
    PROFILE_GLOB,
    PROFILE_GLOB_LTS,
    RESOLVED_AUTO_DISCOVERY,
    RESOLVED_CORPUS_CONFIG,
    RESOLVED_EXPLICIT,
    RESOLVED_NONE,
    LifecycleStages,
    PrefixSpec,
    Profile,
    ProfileResolution,
    discover_profile,
    load_corpus_config,
    load_profile,
    registered_profile_for_root,
    resolve_profile,
    resolve_profile_ex,
)

__version__ = "0.1.0"

__all__ = [
    # §7 wrapper (primary API)
    "parse",
    "ParsedCorpus",
    "Diagnostic",
    "CONTRACT_DIAGNOSTIC_CLASSES",
    # §5 profile hook
    "Profile",
    "load_profile",
    "discover_profile",
    "resolve_profile",
    "resolve_profile_ex",
    "ProfileResolution",
    "registered_profile_for_root",
    "load_corpus_config",
    "RESOLVED_EXPLICIT",
    "RESOLVED_CORPUS_CONFIG",
    "RESOLVED_AUTO_DISCOVERY",
    "RESOLVED_NONE",
    "DEFAULT_PROFILE",
    "PROFILE_GLOB",
    "PROFILE_GLOB_LTS",
    "PrefixSpec",
    "LifecycleStages",
    # copied core
    "parse_file",
    "parse_text",
    "Node",
    "Edge",
    "ParsedFile",
    # THE token estimator `Node.token_count` is produced with — public so the engine's
    # [ENGINE-BUDGET] slicer and everything it budgets cost items the same way.
    "token_estimate",
    # the intrinsic profile-document signal (vocabulary, never content)
    "PROFILE_DOCUMENT_TAG",
    "is_profile_document",
    # [FL-LINE-ACCOUNTING]
    "LineCoverage",
    "LineVerdict",
    "classify_line",
    "account_file",
    "account_summary",
    "unaccounted_lines",
    "IGNORABLE_KINDS",
    "UNACCOUNTED_KINDS",
    "CANONICAL_LAYERS",
    "CORE_EDGE_LAYER",
    "DEFAULT_REGISTERED_NS",
    # the derived property key an ID TABLE's rows land under
    "ID_TABLE_PROPERTY",
    "build_index",
    "compute_snapshot_hash",
    "write_index_atomic",
    "content_hash",
    "CORPUS_FILE_CODES",
    "CorpusFileError",
    "file_content_hash",
    "files_hash",
    "list_lts_files",
    "FACTLATTICE_PROFILE_VERSION",
    "GENERATOR",
]
