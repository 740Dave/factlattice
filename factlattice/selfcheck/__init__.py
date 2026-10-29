"""The parser self-verification CONTROL PAIR — data only, shipped inside the package.

    control.lts            the control corpus. The system under test parses THIS.
    control.expected.json  the hand-authored oracle. Only `json` reads this — NEVER the
                           parser. If ground truth lived inside the corpus, a parser
                           broken enough to misread the corpus would misread its own
                           oracle and the check would agree with itself.

The pair lives beside the parser (rather than in `tests/`) because it must ship with the
INSTALLED package: the whole point is that a RUNNING `factlattice_engine.server` — which
may be a wheel on another machine, started weeks ago — can re-parse it on demand and be
asked what it got. A fixture in `tests/` proves the code in the repo; this proves the
code in the process.

The runner, the assertion vocabulary and the control digest live in
`factlattice_engine.selfcheck`. This module deliberately contains no logic: importing it
must not be able to affect the parse it is a control for.
"""
