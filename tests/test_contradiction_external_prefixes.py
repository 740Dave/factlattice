"""The contradiction checker's list of ids that legitimately live outside the checked corpus holds only
generic prefixes. A deployment's own id families belong in that deployment's tooling, not in the engine's list.

(The checker is deliberately unwired: it is a candidate generator, not a gate.)
"""
from factlattice.authoring import contradiction as C

GENERIC = {"INV-", "IH-", "DP", "CRP-", "SL-", "A-", "OP-", "LCB-", "MUT-",
           "SPEC:", "FLFP-", "REGISTRY:", "DS:", "PROC-", "FL-00"}


def test_the_external_prefix_list_is_exactly_the_generic_set():
    assert set(C._EXTERNAL_PREFIXES) == GENERIC


def test_generic_prefixes_are_still_external_and_unlisted_ones_are_not():
    assert C._is_external("INV-SOME-LONG-RULE") is True
    assert C._is_external("PROC-SOME-LONG-PROCEDURE") is True
    assert C._is_external("QQQ-SOME-LONG-NAME") is False      # control: an unlisted family is not external
