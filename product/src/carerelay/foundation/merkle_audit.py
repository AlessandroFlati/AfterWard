"""TrustedRisk-derived Merkle proofs for CareRelay.

These prove payload inclusion relative to a trusted root. They do not
provide storage, identity, append-only enforcement or clinical validity.
Odd nodes are duplicated; this is not the RFC 6962 tree algorithm.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .schemas import (
    MerkleAuditChainResult,
    MerkleAuditVerificationReport,
    MerkleInclusionProof,
    MerkleProofStep,
)


def _hash_leaf(payload: dict[str, Any]) -> str:
    """SHA-256 of canonical-JSON of the event payload."""
    import json as _json
    blob = _json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    # Distinguish leaf from internal node (RFC 6962 style)
    return hashlib.sha256(b"\x00" + blob).hexdigest()


def _hash_internal(left: str, right: str) -> str:
    return hashlib.sha256(
        b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)
    ).hexdigest()


def _build_tree(leaves: list[str]) -> tuple[str, list[list[str]]]:
    """Return (root, levels). levels[0] is the leaves; levels[-1] = [root]."""
    if not leaves:
        # Empty-tree convention: root = SHA-256(empty)
        empty_root = hashlib.sha256(b"").hexdigest()
        return empty_root, [[empty_root]]

    levels: list[list[str]] = [list(leaves)]
    while len(levels[-1]) > 1:
        cur = levels[-1]
        nxt: list[str] = []
        for i in range(0, len(cur), 2):
            left = cur[i]
            right = cur[i + 1] if i + 1 < len(cur) else cur[i]
            nxt.append(_hash_internal(left, right))
        levels.append(nxt)
    return levels[-1][0], levels


def _proof_for_index(idx: int,
                       levels: list[list[str]]) -> list[MerkleProofStep]:
    proof: list[MerkleProofStep] = []
    cur = idx
    for level in levels[:-1]:
        sibling_idx = cur ^ 1
        if sibling_idx >= len(level):
            sibling = level[cur]   # duplicated leaf path
            is_left = False
        else:
            sibling = level[sibling_idx]
            is_left = sibling_idx < cur
        proof.append(MerkleProofStep(sibling_hash=sibling, is_left=is_left))
        cur //= 2
    return proof


# ─────────────────────── Public API ───────────────────────

def compute_merkle_audit_root(
    events: list[dict[str, Any]],
) -> MerkleAuditChainResult:
    """Build a Merkle tree over a list of audit-event payloads."""
    if not isinstance(events, list):
        raise ValueError("events must be a list of dicts.")
    leaves = [_hash_leaf(e) for e in events if isinstance(e, dict)]
    root, levels = _build_tree(leaves)
    return MerkleAuditChainResult(
        n_events=len(leaves),
        leaf_hashes=leaves,
        merkle_root=root,
        height=max(0, len(levels) - 1),
    )


def build_inclusion_proof(
    events: list[dict[str, Any]],
    target_event_id: str,
    *,
    event_id_field: str = "event_id",
) -> MerkleInclusionProof:
    """Construct an inclusion proof for one event in the supplied list."""
    if not isinstance(events, list) or not events:
        raise ValueError("events must be a non-empty list.")
    target_idx: int | None = None
    for i, e in enumerate(events):
        if isinstance(e, dict) and e.get(event_id_field) == target_event_id:
            target_idx = i
            break
    if target_idx is None:
        raise ValueError(
            f"event_id {target_event_id!r} not found in events.")

    leaves = [_hash_leaf(e) for e in events if isinstance(e, dict)]
    root, levels = _build_tree(leaves)
    leaf_hash = leaves[target_idx]
    proof = _proof_for_index(target_idx, levels)
    verified = verify_inclusion(leaf_hash, proof, root)
    return MerkleInclusionProof(
        event_id=target_event_id,
        leaf_hash=leaf_hash,
        merkle_root=root,
        proof_steps=proof,
        verified=verified,
    )


def verify_inclusion(
    leaf_hash: str,
    proof_steps: list[MerkleProofStep],
    expected_root: str,
) -> bool:
    """Recompute the Merkle root from leaf + proof; compare to expected."""
    cur = leaf_hash
    for step in proof_steps:
        if step.is_left:
            cur = _hash_internal(step.sibling_hash, cur)
        else:
            cur = _hash_internal(cur, step.sibling_hash)
    return cur == expected_root


# ─────────────────────── Phase 12.2 -- full chain verifier ───────────────────────


def verify_audit_chain(
    events: list[dict[str, Any]],
    *,
    target_event_id: str,
    claimed_leaf_hash: str,
    proof_steps: list[MerkleProofStep] | list[dict[str, Any]],
    expected_merkle_root: str,
    event_id_field: str = "event_id",
) -> MerkleAuditVerificationReport:
    """End-to-end audit-chain verifier.

    Re-derives the leaf hash for `target_event_id` from the supplied
    `events` ledger and walks the proof to reconstruct the Merkle root.
    Distinguishes three failure modes -- event tamper, chain tamper,
    unknown event -- so an auditor can act on each differently.

    Args:
        events: full ordered ledger as a list of dicts.
        target_event_id: the event we want to verify.
        claimed_leaf_hash: the leaf hash recorded in the published
            inclusion proof. We recompute and compare.
        proof_steps: published `MerkleProofStep` chain. Accepted as
            either Pydantic instances or raw dicts.
        expected_merkle_root: the published Merkle root.
        event_id_field: which field on the event carries the id.

    Returns:
        MerkleAuditVerificationReport.
    """
    # Coerce raw dicts to MerkleProofStep instances
    typed_proof: list[MerkleProofStep] = []
    for step in proof_steps:
        if isinstance(step, MerkleProofStep):
            typed_proof.append(step)
        else:
            typed_proof.append(MerkleProofStep.model_validate(step))

    target = next(
        (e for e in events
         if isinstance(e, dict) and e.get(event_id_field) == target_event_id),
        None,
    )
    if target is None:
        return MerkleAuditVerificationReport(
            event_id=target_event_id,
            expected_merkle_root=expected_merkle_root,
            recomputed_leaf_hash="",
            claimed_leaf_hash=claimed_leaf_hash,
            recomputed_root="",
            posture="unknown_event",
            rationale=(
                f"event_id {target_event_id!r} not present in the "
                f"supplied ledger of {len(events)} event(s)."
            ),
            references=_REFS,
        )

    recomputed_leaf = _hash_leaf(target)
    if recomputed_leaf != claimed_leaf_hash:
        return MerkleAuditVerificationReport(
            event_id=target_event_id,
            expected_merkle_root=expected_merkle_root,
            recomputed_leaf_hash=recomputed_leaf,
            claimed_leaf_hash=claimed_leaf_hash,
            recomputed_root="",
            posture="tamper_event",
            rationale=(
                "The recomputed leaf hash diverges from the published "
                "leaf hash -- a byte of the audited event has changed."
            ),
            references=_REFS,
        )

    # Walk the proof
    cur = recomputed_leaf
    for step in typed_proof:
        if step.is_left:
            cur = _hash_internal(step.sibling_hash, cur)
        else:
            cur = _hash_internal(cur, step.sibling_hash)
    if cur != expected_merkle_root:
        return MerkleAuditVerificationReport(
            event_id=target_event_id,
            expected_merkle_root=expected_merkle_root,
            recomputed_leaf_hash=recomputed_leaf,
            claimed_leaf_hash=claimed_leaf_hash,
            recomputed_root=cur,
            posture="tamper_chain",
            rationale=(
                "The leaf hash is intact, but the proof does not "
                "reconstruct to the expected Merkle root -- a sibling "
                "along the inclusion path has been tampered."
            ),
            references=_REFS,
        )

    return MerkleAuditVerificationReport(
        event_id=target_event_id,
        expected_merkle_root=expected_merkle_root,
        recomputed_leaf_hash=recomputed_leaf,
        claimed_leaf_hash=claimed_leaf_hash,
        recomputed_root=cur,
        posture="verified",
        rationale=(
            "Leaf hash matches AND the proof reconstructs to the "
            "expected root -- the audited event is byte-identical to "
            "its published version."
        ),
        references=_REFS,
    )


_REFS: list[str] = [
    "SHA-256 Merkle tree with domain separation and duplicated odd nodes.",
    "TrustedRisk Merkle audit chain -- Phase 12.2.",
]