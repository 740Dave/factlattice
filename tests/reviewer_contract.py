"""The reviewer's PINNED contracts, written out literally (X1/X4 VERSIONED-CONTRACTS).

These literals are the TEST's copy of the contract. The shipped profile, the selfcheck oracle, the REVPROC-CORE
text and the agent definition are each checked against them, so a rename or a removal in ANY of those fails a
test here until this file changes too, and a change here is a contract change: it bumps HOOK_CONTRACT_VERSION
(hooks) or REVIEW_FORMAT_VERSION (review fields). Source: SPEC-A-reviewer-v3 sections 4.2 and 5, the v4
addendum C3(b) (site-config, site-overrides), C4(b) and the ruling-(B) condition 2 (creation-ledger-state).
"""
HOOK_CONTRACT_VERSION = 1
REVIEW_FORMAT_VERSION = 2

CORE_HOOKS = ("REVHOOK-STRUCTURE", "REVHOOK-DETERMINISTIC", "REVHOOK-JUDGEMENT",
              "REVHOOK-CROSS-DOCUMENT", "REVHOOK-REPORT")

# prefix -> the properties the reviewer profile REQUIRES (lint V3)
REQUIRED = {
    "REVIEW": ("review-format-version", "verdict", "reviewed-corpus", "reviewed-document",
               "reviewed-document-hash", "node-count-reviewed", "route", "engine-build", "query-surface",
               "core-proc-source", "core-proc-hash", "extensions-applied", "findings-by-severity",
               "not-examined", "caller-context", "write-root-caveat", "reviewer-model",
               "creation-ledger-state", "site-config", "site-overrides", "registry-mcp", "registry-cli"),
    "UNREVIEWED": ("review-format-version", "reviewed-corpus", "reviewed-document",
                   "reviewed-document-hash", "stop-reason", "gate-output", "route", "engine-build",
                   "creation-ledger-state", "site-config", "site-overrides", "registry-mcp", "registry-cli"),
    "REVFINDING": ("rule", "rule-hash", "rule-reliability", "about-node", "about-node-hash", "severity",
                   "evidence", "evidence-lines", "hook", "kind", "part-of"),
    "REVCHECK": ("rule", "rule-hash", "outcome", "reason", "part-of"),
    "REVPROV": ("kind", "source", "hash", "loaded-via", "part-of"),
    "REVEXT": ("applies-to", "version", "authority", "hook-contract-version"),
    "REVHOOK": ("hook-contract-version",),
    "REVPROC": ("order",),
    "REVRULE": ("applies-to", "kind", "severity", "authority", "on-fail", "planted-violation", "clean-twin"),
}
RECOMMENDED = {"REVPROV": ("realpath",)}

# keys a review document may carry beyond the required ones (declared so `lts: "1.1"` does not reject them)
OPTIONAL_KEYS = ("self-gate", "realpath", "stub-of", "query", "expect", "question", "pass-criteria",
                 "examples", "command", "note", "status", "attaches-to-hook", "applies-rule", "default-output-root",
                 "corpus-output-overrides", "default-root-corpora", "title-note")

# REVIEWER-CONFIG / REVIEWER-SITE-CONFIG keys (REVIEWER-CONFIG-KEYS row)
CONFIG_KEYS = ("default-output-root", "corpus-output-overrides", "default-root-corpora")

PREFIXES = ("REVIEW-", "UNREVIEWED-", "REVFINDING-", "REVCHECK-", "REVPROV-", "REVEXT-", "REVPROC-",
            "REVRULE-", "REVHOOK-", "REVIEWER-")
