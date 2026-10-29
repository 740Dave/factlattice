"""Every agents/*.md is classified in PUBLISH-EXCLUDE (public, internal or pending), so a new agent definition
cannot reach a publication snapshot unreviewed. [PUBLISH-EXCLUDE-CLASSIFIED] in SDLC.md.

PUBLISH-EXCLUDE is itself excluded from a published tree, so there this file SKIPS (the snapshot has nothing to classify).
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXCLUDE = ROOT / "PUBLISH-EXCLUDE"
AGENTS = ROOT / "agents"


def classified_names(text):
    """Agent file names the exclude list classifies: '# PUBLIC|INTERNAL|MAINTAINER-PENDING <names>' comment lines, plus
    active and #PENDING 'agents/<name>.md' entries."""
    names = set()
    for line in text.splitlines():
        m = re.match(r"^#\s*(PUBLIC|INTERNAL|MAINTAINER-PENDING)\s+(.*)$", line)
        if m:
            names |= set(re.findall(r"[\w.-]+\.md", m.group(2).split("(")[0]))
        m = re.match(r"^(?:#PENDING\s+)?agents/([\w.-]+\.md)\s", line)
        if m:
            names.add(m.group(1))
    return names


def unclassified(agent_files, text):
    done = classified_names(text)
    return sorted(f for f in agent_files if f not in done)


def test_the_classifier_flags_an_unlisted_agent_definition():
    """Negative control: the check can fail."""
    text = "# PUBLIC  a.md   generic\nagents/b.md   # internal\n"
    assert unclassified(["a.md", "b.md", "new-agent.md"], text) == ["new-agent.md"]


@pytest.mark.skipif(not EXCLUDE.is_file() or not AGENTS.is_dir(), reason="PUBLISH-EXCLUDE is not part of a published tree")
def test_every_agent_definition_is_classified():
    files = [p.name for p in AGENTS.glob("*.md")]
    assert files, "no agent definitions found: the check would pass vacuously"
    assert unclassified(files, EXCLUDE.read_text(encoding="utf-8")) == []
