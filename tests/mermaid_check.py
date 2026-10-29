"""[LEC-RENDER] Mermaid validation helper for the render tests.

TWO levels, because the acceptance bar is "validate the Mermaid actually parses, do
not just string-match":

  1. STRUCTURAL INVARIANTS (always run, zero dependencies) — a small line-oriented
     grammar check over the exact shapes the emitter is allowed to produce:
       * the header is `flowchart <DIR>` (after an optional `--- title ---` block);
       * every line is a node declaration, an edge, a `subgraph`, or `end`;
       * subgraph/end are balanced;
       * every node id is a legal Mermaid identifier;
       * every id referenced by an edge is DECLARED (no phantom nodes);
       * shape delimiters are matched pairs;
       * every label is double-quoted, contains NO raw `"`, and every `#` in it
         opens a well-formed entity code.

  2. REAL PARSE (run when available) — shells out to `tools/mermaid_validate.mjs`,
     which drives the actual `mermaid.parse()` grammar headless under jsdom. Enabled
     by setting FACTLATTICE_MERMAID_NODE_MODULES to a directory containing an
     `npm i mermaid jsdom` install. Skipped silently when absent so the suite stays
     zero-third-party; `real_parse_available()` lets a test report which ran.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import List, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VALIDATOR = os.path.join(_REPO_ROOT, "tools", "mermaid_validate.mjs")
_ENV_NODE_MODULES = "FACTLATTICE_MERMAID_NODE_MODULES"

_ID = r"[A-Za-z_][A-Za-z0-9_]*"
_SHAPE_PAIRS = {"[[": "]]", "((": "))", "([": "])", "{{": "}}",
                "[": "]", "(": ")", "{": "}"}

_HEADER_RE = re.compile(r"^flowchart (TD|TB|BT|LR|RL)$")
_SUBGRAPH_RE = re.compile(rf'^subgraph (?P<id>{_ID})\["(?P<label>[^"]*)"\]$')
_NODE_RE = re.compile(
    rf'^(?P<id>{_ID})(?P<open>\[\[|\(\(|\(\[|\{{\{{|\[|\(|\{{)'
    r'"(?P<label>[^"]*)"'
    r'(?P<close>\]\]|\)\)|\]\)|\}\}|\]|\)|\})$')
_EDGE_RE = re.compile(
    rf'^(?P<a>{_ID}) -->(?:\|"(?P<label>[^"]*)"\|)? (?P<b>{_ID})$')
_ENTITY_RE = re.compile(r"#([0-9]+|[A-Za-z]+);")


class MermaidStructureError(AssertionError):
    pass


def _check_label(label: str, where: str) -> None:
    # A raw '"' cannot appear (the regex already guarantees it) — the real risk is a
    # '#' that is NOT an entity opener, which Mermaid would swallow as a code.
    for m in re.finditer(r"#", label):
        tail = label[m.start():]
        if not _ENTITY_RE.match(tail):
            raise MermaidStructureError(
                f"{where}: bare '#' (not a valid entity code) in label {label!r}")
    for ch in ("\n", "\r", "\t"):
        if ch in label:
            raise MermaidStructureError(f"{where}: raw whitespace control char in label")


def check_structure(text: str) -> None:
    """Raise MermaidStructureError if `text` violates the emitter's grammar."""
    if not isinstance(text, str) or not text.strip():
        raise MermaidStructureError("empty diagram")
    lines = text.split("\n")
    i = 0
    if lines and lines[0] == "---":
        try:
            close = lines.index("---", 1)
        except ValueError:
            raise MermaidStructureError("unterminated title frontmatter")
        for fm in lines[1:close]:
            if not re.match(r"^[a-z]+: .*$", fm):
                raise MermaidStructureError(f"bad title frontmatter line: {fm!r}")
        i = close + 1
    if i >= len(lines) or not _HEADER_RE.match(lines[i]):
        raise MermaidStructureError(
            f"missing/invalid flowchart header: {lines[i] if i < len(lines) else '<eof>'!r}")
    i += 1

    declared: set = set()
    referenced: List[str] = []
    depth = 0
    for lineno, raw in enumerate(lines[i:], start=i + 1):
        line = raw.strip()
        if not line:
            continue
        where = f"line {lineno}"
        if line.count('"') % 2 != 0:
            raise MermaidStructureError(f"{where}: unbalanced quotes: {line!r}")
        if line == "end":
            depth -= 1
            if depth < 0:
                raise MermaidStructureError(f"{where}: 'end' without 'subgraph'")
            continue
        m = _SUBGRAPH_RE.match(line)
        if m:
            depth += 1
            _check_label(m.group("label"), where)
            continue
        m = _NODE_RE.match(line)
        if m:
            if _SHAPE_PAIRS.get(m.group("open")) != m.group("close"):
                raise MermaidStructureError(
                    f"{where}: mismatched shape delimiters "
                    f"{m.group('open')!r}...{m.group('close')!r}")
            _check_label(m.group("label"), where)
            declared.add(m.group("id"))
            continue
        m = _EDGE_RE.match(line)
        if m:
            if m.group("label") is not None:
                _check_label(m.group("label"), where)
            referenced.extend([m.group("a"), m.group("b")])
            continue
        raise MermaidStructureError(f"{where}: unrecognised statement: {line!r}")

    if depth != 0:
        raise MermaidStructureError(f"unbalanced subgraph/end (depth {depth})")
    undeclared = [r for r in referenced if r not in declared]
    if undeclared:
        raise MermaidStructureError(
            f"edge references undeclared node id(s): {sorted(set(undeclared))}")


# --- level 2: the real Mermaid grammar ------------------------------------------
def real_parse_available() -> bool:
    """True when FACTLATTICE_MERMAID_NODE_MODULES points at an npm project directory
    holding `node_modules/mermaid` + `node_modules/jsdom`."""
    proj = os.environ.get(_ENV_NODE_MODULES)
    return bool(proj and os.path.isdir(os.path.join(proj, "node_modules", "mermaid"))
                and os.path.isdir(os.path.join(proj, "node_modules", "jsdom"))
                and os.path.isfile(_VALIDATOR))


def real_parse(diagrams: List[str]) -> Optional[List[dict]]:
    """Drive `mermaid.parse()` on each diagram. None when the validator is unavailable.

    The script is fed to `node --input-type=module -e` with cwd set to the npm project,
    because ESM `import 'mermaid'` resolves relative to the ENTRY POINT's directory and
    ignores NODE_PATH — running the repo's `tools/*.mjs` in place would never find the
    (deliberately un-vendored) node_modules.
    """
    if not real_parse_available():
        return None
    proj = os.environ[_ENV_NODE_MODULES]
    with open(_VALIDATOR, "r", encoding="utf-8") as fh:
        script = fh.read()
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps(diagrams), cwd=proj,
        capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise AssertionError(f"mermaid validator failed: {proc.stderr[-800:]}")
    return json.loads(proc.stdout)


def assert_valid_mermaid(text: str) -> None:
    """Structural invariants ALWAYS; the real Mermaid grammar when available."""
    check_structure(text)
    results = real_parse([text])
    if results is not None and not results[0]["ok"]:
        raise AssertionError(
            f"mermaid.parse() rejected the diagram: {results[0]['error']}\n---\n{text}")
