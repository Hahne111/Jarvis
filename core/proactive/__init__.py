"""Proactivity + learning, controlled (SPEC §14/§15, Phase 11).

Relevance engine, habit detector + suggestions, daily brief, privacy modes.
"""

from core.proactive.brief import BRIEF_MANIFEST, BriefBuilder, register_brief
from core.proactive.habits import (
    PRELOAD_FOR,
    HabitDetector,
    Suggestion,
    SuggestionStore,
)
from core.proactive.privacy import (
    MODES,
    PRIVACY_MANIFESTS,
    PrivacyService,
    PrivacyState,
    register_privacy,
)
from core.proactive.relevance import CHANNELS, Assessment, Context, RelevanceEngine

__all__ = [
    "BRIEF_MANIFEST",
    "CHANNELS",
    "MODES",
    "PRELOAD_FOR",
    "PRIVACY_MANIFESTS",
    "Assessment",
    "BriefBuilder",
    "Context",
    "HabitDetector",
    "PrivacyService",
    "PrivacyState",
    "RelevanceEngine",
    "Suggestion",
    "SuggestionStore",
    "register_brief",
    "register_privacy",
]
