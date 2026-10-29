# Quality Event Emission Protocol

> All FactLattice agents MUST follow this protocol. This is not optional — the event stream is how the SDLC demonstrates measurable value.

## Requirement

After completing analysis, append structured event records to `{corpus}/events/quality.jsonl`. One JSON object per line. Append-only — never modify or delete existing events.

## Event Types by Agent

| Agent | Event Type | When |
|-------|-----------|------|
| `factlattice-linter` | `factlattice.lint` | After every lint run |
| `factlattice-gap-analyzer` | `factlattice.finding` | Per gap found |
| `factlattice-densifier` | `factlattice.densification` | After every densification |
| `factlattice-signal-loss-reviewer` | `factlattice.densification` (update) | After every review verdict |
| `factlattice-corpus-analyzer` | `factlattice.corpus_health` | After every corpus analysis |
| `factlattice-diff-agent` | `factlattice.finding` | Per change with suggested_impact >= additive |
| `factlattice-delta-gen` | `core.corpus_change` | Per DELTA generated |
| `factlattice-profile-validator` | `factlattice.lint` | After every profile validation |
| spec-editing agent | (none — edits, not findings) | — |
| `factlattice-profile-extractor` | (none — migration, not findings) | — |
| `factlattice-registry` | `factlattice.corpus_health` | After registry sweep |

## Event Schemas

### factlattice.lint
```json
{"type": "factlattice.lint", "id": "LINT-{timestamp}", "date": "ISO-8601", "agent": "factlattice-linter", "document": "20-system-architecture.lts", "errors": 0, "warnings": 4, "info": 3, "density_score": 0.91, "domain": "ops.example"}
```

### factlattice.finding
```json
{"type": "factlattice.finding", "id": "GAP-DC-001", "date": "ISO-8601", "agent": "factlattice-gap-analyzer", "cycle_id": "GAP-{date}-{domain}", "spec": "34-router-pipeline.lts", "implementation": "~/project/src/router/pipeline.py", "severity": "P1", "category": "SPEC_IMPL_MISMATCH", "summary": "VALID_TYPES missing 4 new message types", "resolution_status": "open", "domain": "ops.example"}
```

### factlattice.finding_resolved
```json
{"type": "factlattice.finding_resolved", "id": "GAP-DC-001-resolved", "date": "ISO-8601", "finding_ref": "GAP-DC-001", "resolved_by": "domain-builder", "resolution": "Added 4 types to VALID_TYPES set", "cycles_to_resolve": 1, "first_pass": true}
```

### factlattice.densification
```json
{"type": "factlattice.densification", "id": "DENS-{timestamp}", "date": "ISO-8601", "agent": "factlattice-densifier", "document": "30-session-lifecycle.lts", "tokens_before": 12400, "tokens_after": 8200, "compression_ratio": 0.66, "reviewer_verdict": null, "signal_loss_findings": null}
```

### factlattice.corpus_health
```json
{"type": "factlattice.corpus_health", "id": "HEALTH-{timestamp}", "date": "ISO-8601", "agent": "factlattice-corpus-analyzer", "corpus": "ops.example", "total_files": 25, "total_nodes": 412, "missing_links": 3, "definition_drift": 1, "orphaned_definitions": 0, "convergence_candidates": 2}
```

### core.corpus_change
```json
{"type": "core.corpus_change", "id": "CR-{date}-{sha}", "date": "ISO-8601", "commit": "abc1234", "estimated_highest_impact": "breaking", "nodes_modified": ["CRED-TOKEN"], "change_record_path": "deltas/DELTA-{date}-{sha}.lts"}
```

## Implementation

At the END of every agent run, before returning results:
1. Construct the appropriate event JSON
2. Append one line to `{corpus}/events/quality.jsonl`
3. If the file doesn't exist, create it
4. If the write fails (permissions, disk), log a warning but do NOT fail the agent run — the analysis is still valid
