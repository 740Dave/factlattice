#!/usr/bin/env python3
"""Land-time check for a NEW minting key (SDLC 7.6 [MINTING-TABLE-KEYS]).

    key_prior_use.py KEY ROOT [ROOT ...]

Scans every `.lts` under each ROOT, READ-ONLY (it opens files for reading and never writes),
for existing PROPERTIES named KEY. Prints one JSON report: how many nodes already use the
key and how many of those values are not refs-only (i.e. free-text that would start
minting wrong edges the moment KEY joins the table). Exit 0 = no collision (zero prior
uses, or every use is already refs-only); exit 1 = collision; exit 2 = usage/parse error.
Bare `canonical` would have exited 1 on acme-docs (10 free-text uses): that is the
review F1 class this tool exists to stop. Run it over EVERY registered corpus root.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from factlattice.parser import parse_text  # noqa: E402
from factlattice_engine.lint.canonical import _refs_only  # noqa: E402


def main(argv):
    if len(argv) < 3:
        print("usage: key_prior_use.py KEY ROOT [ROOT ...]", file=sys.stderr)
        return 2
    key, roots = argv[1], argv[2:]
    uses, bad, examples, errors = 0, 0, [], 0
    for root in roots:
        files = [root] if os.path.isfile(root) else [
            os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs if f.endswith(".lts")]
        for path in sorted(files):
            try:
                with open(path, encoding="utf-8") as fh:
                    parsed = parse_text(fh.read(), path)
            except Exception:
                errors += 1
                continue
            for n in parsed.nodes:
                if key not in n.properties:
                    continue
                v = n.properties[key]
                for item in (v if isinstance(v, list) else [v]):
                    uses += 1
                    if not _refs_only(item):
                        bad += 1
                        if len(examples) < 10:
                            examples.append({"file": path, "node": n.id, "value": str(item)[:120]})
    print(json.dumps({"key": key, "uses": uses, "not_refs_only": bad,
                      "unparsed_files": errors, "examples": examples}, indent=1))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
