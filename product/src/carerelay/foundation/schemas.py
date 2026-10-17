"""Selected TrustedRisk document and audit models (MIT, Alessandro Flati)."""

from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field


class MerkleProofStep(BaseModel):
    """One sibling-hash step in a Merkle inclusion proof."""
    sibling_hash: str
    is_left: bool


class MerkleAuditChainResult(BaseModel):
    """Output of compute_merkle_audit_root (AUDIT-1)."""
    n_events: int = Field(ge=0)
    leaf_hashes: list[str]
    merkle_root: str
    height: int = Field(ge=0)
    method: Literal["sha256_merkle"] = "sha256_merkle"


class MerkleInclusionProof(BaseModel):
    """Proof that an event_id is included in a published Merkle root."""
    event_id: str
    leaf_hash: str
    merkle_root: str
    proof_steps: list[MerkleProofStep]
    verified: bool


class MerkleAuditVerificationReport(BaseModel):
    """Output of compute_verify_audit_chain (Phase 12.2 -- AUDIT-3).

    Posture is one of:
      `verified`       -- the recomputed leaf hash matches AND the proof
                         reconstructs to the expected root.
      `tamper_event`   -- the recomputed leaf hash diverges from the
                         claimed leaf -- a byte of the audited event was
                         changed.
      `tamper_chain`   -- the leaf hash is intact but the proof does NOT
                         reconstruct to the expected root -- a sibling
                         along the chain was tampered.
      `unknown_event`  -- the target event_id is not present in the
                         supplied ledger.
    """
    event_id: str
    expected_merkle_root: str
    recomputed_leaf_hash: str
    claimed_leaf_hash: str
    recomputed_root: str
    posture: Literal[
        "verified", "tamper_event", "tamper_chain", "unknown_event",
    ]
    rationale: str
    references: list[str] = Field(default_factory=list)


class ClinicalNoteSection(BaseModel):
    """One section of a clinical note draft.

    Cite-backs are mandatory: every clinical claim in the body must
    trace to an FHIR resource ID or a chart line span. The LLM polish
    layer paraphrases prose without inventing new facts; the cite-back
    list is the immutable spine.
    """
    section_id: str
    title: str
    body: str
    cited_evidence_ids: list[str] = Field(default_factory=list)
    is_llm_polished: bool = False
