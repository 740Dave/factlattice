/**
 * [LEC-RENDER] REAL Mermaid parse-validation, offline.
 *
 * `mermaid.parse()` is the grammar the renderer must satisfy; string-matching the
 * emitted text only proves we emitted what we intended, not that Mermaid accepts it.
 * Mermaid is browser-oriented, so a minimal jsdom DOM is installed on globalThis
 * before the (ESM) import — after that `parse` runs headless with no network.
 *
 * Usage (stdin = JSON array of diagram strings; stdout = JSON array of results).
 * ESM resolves `import 'mermaid'` relative to the ENTRY POINT, so run it FROM the
 * npm project directory rather than in place:
 *
 *     cd /path/to/proj && npm i mermaid jsdom
 *     node --input-type=module -e "$(cat /path/to/tools/mermaid_validate.mjs)" \
 *          <<< '["flowchart TD\n  A[\"x\"]"]'
 *
 * mermaid/jsdom are deliberately NOT vendored — lts-engine's Python deliverable
 * stays zero-third-party. The pytest suite ALWAYS asserts structural invariants and
 * ADDITIONALLY drives this script when FACTLATTICE_MERMAID_NODE_MODULES points at
 * such a project directory (`tests/mermaid_check.py`).
 */
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!DOCTYPE html><body></body>', { pretendToBeVisual: true });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.Element = dom.window.Element;
globalThis.Node = dom.window.Node;
globalThis.DocumentFragment = dom.window.DocumentFragment;
globalThis.NodeFilter = dom.window.NodeFilter;
globalThis.HTMLTemplateElement = dom.window.HTMLTemplateElement;
Object.defineProperty(globalThis, 'navigator', {
  value: dom.window.navigator, configurable: true,
});

const mermaid = (await import('mermaid')).default;
mermaid.initialize({ startOnLoad: false, securityLevel: 'loose' });

let raw = '';
for await (const chunk of process.stdin) raw += chunk;
const diagrams = JSON.parse(raw || '[]');

const results = [];
for (const text of diagrams) {
  try {
    await mermaid.parse(text);
    results.push({ ok: true });
  } catch (e) {
    results.push({ ok: false, error: String((e && e.message) || e).split('\n').slice(0, 3).join(' | ') });
  }
}
process.stdout.write(JSON.stringify(results));
