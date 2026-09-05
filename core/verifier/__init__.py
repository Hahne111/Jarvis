"""Verifier interface (SPEC §5.1, Phase 2 step 14, Commit 008)."""

from core.verifier.mocks import register_mock_verifiers
from core.verifier.model import EVENT_FOR_OUTCOME, Outcome, Verification
from core.verifier.service import (
    RetryPolicy,
    VerificationService,
    VerifiedExecutor,
    VerifiedResult,
    VerifierNotFound,
    VerifierRegistry,
)

__all__ = [
    "EVENT_FOR_OUTCOME",
    "Outcome",
    "RetryPolicy",
    "Verification",
    "VerificationService",
    "VerifiedExecutor",
    "VerifiedResult",
    "VerifierNotFound",
    "VerifierRegistry",
    "register_mock_verifiers",
]
