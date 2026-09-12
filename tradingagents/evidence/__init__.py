"""Evidence ledger: structured provenance for research facts (C1).

Flow: vendor fetch produces (text, artifact) from the SAME call → graph
news tools (``response_format="content_and_artifact"``) surface the
artifact on ``ToolMessage.artifact`` → the branch-isolated tool node
collects this execution's artifacts into a delta (events keyed by
``{role}:{tool_call_id}``, run identity from state) → ``evidence_reducer``
merges deltas into ``AgentState.evidence_bundle`` (immutable, idempotent,
deterministic, run-isolated) → the bundle rides the checkpoint and the
final-state JSON, and powers source summaries + reference validation.

Old string-only vendors route through the same configured chain and are
recorded as ``provenance: unknown`` — plain text is never guessed into
strong evidence.
"""

from .ledger import (
    AVAIL_PUBLICATION_ONLY,
    AVAIL_UNKNOWN,
    EXCERPT_CAP,
    PRECISION_DATE,
    PRECISION_DATETIME,
    PRECISION_UNKNOWN,
    SCHEMA_ARTIFACT,
    SCHEMA_BUNDLE,
    SCHEMA_DELTA,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SUCCESSFUL,
    bundle_event_count,
    bundle_record_count,
    collect_tool_message_delta,
    evidence_reducer,
    normalize_artifact,
    normalize_record,
    render_source_summary,
    validate_reference,
)
