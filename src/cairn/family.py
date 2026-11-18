"""Family profile for this Cairn derivative.

The profile is opt-in through ``CAIRN_FAMILY_PROFILE``. Upstream tests and a
stock Cairn process keep the original default learner and model client.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

PROFILE_ENV = "CAIRN_FAMILY_PROFILE"
ALLOW_MODEL_ENV = "CAIRN_FAMILY_ALLOW_MODEL"


@dataclass(frozen=True)
class FamilyProfile:
    profile_id: str
    learner_name: str
    grade_level: str
    grade_label: str
    ui_language: str


def current_profile() -> FamilyProfile | None:
    """Return the Jojo profile only when the operator turns it on."""
    if os.environ.get(PROFILE_ENV, "").strip() != "jojo":
        return None
    return FamilyProfile(
        profile_id="jojo",
        learner_name="Jojo",
        grade_level="primary-4",
        grade_label="四年级",
        ui_language="zh-Hans",
    )


def new_learner_kwargs() -> dict[str, str]:
    """Fields for a brand-new learner row. Unset profile matches upstream."""
    profile = current_profile()
    if profile is None:
        return {"name": "default"}
    return {"name": profile.learner_name, "grade_level": profile.grade_level}


def claim_default_learner(learner: object) -> bool:
    """Name the untouched default learner once. Never rename a custom row."""
    profile = current_profile()
    if profile is None:
        return False
    name = getattr(learner, "name", None)
    grade = getattr(learner, "grade_level", None)
    if name != "default" or grade not in (None, "", "primary"):
        return False
    learner.name = profile.learner_name
    learner.grade_level = profile.grade_level
    return True


def model_calls_blocked() -> bool:
    """Block tutor, recall, and vision-model network calls for the family profile.

    Local OCR stays available. ``CAIRN_FAMILY_ALLOW_MODEL=1`` is an explicit
    operator override and is not set by the private launcher.
    """
    if current_profile() is None:
        return False
    return os.environ.get(ALLOW_MODEL_ENV, "").strip() != "1"


def seed_runtime(session: object) -> None:
    """Idempotent rows the web lifespan also creates, for offline import."""
    from cairn.models import Strategy
    from cairn.strategies.registry import list_strategies
    from cairn.workflow.service import (
        ensure_collection_workflow,
        ensure_default_workflow,
        ensure_runnable_default,
    )

    for entry in list_strategies():
        if session.get(Strategy, entry["id"]) is None:
            session.add(Strategy(id=entry["id"], name=entry["name"]))
    ensure_default_workflow(session)
    ensure_collection_workflow(session)
    ensure_runnable_default(session)
    session.flush()
