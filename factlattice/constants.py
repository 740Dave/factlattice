"""Shared constants for the lts-engine engine (kept dependency-free to avoid import cycles)."""

FACTLATTICE_PROFILE_VERSION = "factlattice-1.0-draft"
GENERATOR = "factlattice-engine v0.1 (phase-1)"

# SPEC §1.5 — the frontmatter keys every FactLattice document must carry.
#
# ⛔ IT LIVES HERE BECAUSE TWO SURFACES MUST AGREE ABOUT IT AND ONE OF THEM IS A WRITER.
# The linter's V9 required these three; `authoring.schema.Frontmatter` was `extra="forbid"`
# over a DISJOINT set (`title/id/domain/author/date/status`). The intersection was EMPTY:
# ⛔ THE SANCTIONED WRITER COULD NOT EMIT A DOCUMENT THAT PASSED THE SANCTIONED LINTER,
# and neither side could see it because each was internally consistent. Measured with
# probes 2026-08-13. One definition, imported by both, so the next divergence is a test
# failure rather than an impossibility nobody can name.
REQUIRED_FRONTMATTER = ("scope", "audience", "boundary")
