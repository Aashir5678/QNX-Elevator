"""Pure dispatch logic. No hardware, no IPC, no I/O.

Everything here is deterministic and importable by the simulator, so the
priority+aging algorithm can be tuned before anything is wired up.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# --- Tunables -------------------------------------------------------------
#
# AGING_FACTOR is the priority gained per second of waiting. It sets the
# exchange rate between "people waiting" and "seconds waited":
#
#   AGING_FACTOR = 0.10  -> 10s of waiting is worth 1 extra person
#   AGING_FACTOR = 0.50  ->  2s of waiting is worth 1 extra person
#   AGING_FACTOR = 0.00  -> pure headcount greedy (starves lonely floors)
#
# Tune this with sim/simulate.py before touching hardware.
AGING_FACTOR = 0.10

# A floor with an active call but zero detected heads still deserves service --
# the button was physically pressed, so someone is there even if vision missed
# them (bad angle, occlusion, color threshold miss). This floor is the minimum
# headcount credited to any active call.
MIN_ASSUMED_HEADS = 1


@dataclass
class FloorCall:
    """Call state for a single floor, as published by floor_input."""

    floor: int
    active_call: bool = False
    wait_start: Optional[float] = None  # epoch seconds, set when call raised


@dataclass
class Score:
    """A floor's computed priority, with its inputs kept for explainability."""

    floor: int
    head_count: int
    credited_heads: int
    wait_seconds: float
    aging_bonus: float
    priority: float

    def reason(self) -> str:
        credited = ""
        if self.credited_heads != self.head_count:
            credited = f" (credited {self.credited_heads}, vision saw {self.head_count})"
        return (
            f"floor {self.floor}: {self.priority:6.2f} = "
            f"{self.credited_heads} heads{credited} + "
            f"{self.aging_bonus:.2f} aging ({self.wait_seconds:.1f}s waited)"
        )


@dataclass
class Decision:
    """Result of one dispatch evaluation."""

    target: Optional[int]
    scores: List[Score] = field(default_factory=list)

    def explain(self) -> str:
        if not self.scores:
            return "no active calls -- idle"
        lines = [s.reason() for s in sorted(self.scores, key=lambda s: -s.priority)]
        head = f"-> dispatch to floor {self.target}" if self.target else "-> idle"
        return head + "\n    " + "\n    ".join(lines)


def compute_scores(
    calls: Dict[int, FloorCall],
    head_counts: Dict[int, int],
    now: float,
    aging_factor: float = AGING_FACTOR,
    min_assumed_heads: int = MIN_ASSUMED_HEADS,
) -> List[Score]:
    """Score every floor that has an active call.

    head_counts is expected to be a *complete* view: the dispatcher retains the
    last known count for any floor whose ROI vision is currently suppressing,
    so absent floors here genuinely mean "never observed" and score as zero
    detections. Either way an active call is credited at least
    min_assumed_heads -- the button was physically pressed, so somebody is
    there regardless of what vision reports.
    """
    scores = []
    for floor, call in calls.items():
        if not call.active_call:
            continue
        head_count = head_counts.get(floor, 0)
        credited = max(head_count, min_assumed_heads)
        wait = max(0.0, now - call.wait_start) if call.wait_start is not None else 0.0
        bonus = aging_factor * wait
        scores.append(
            Score(
                floor=floor,
                head_count=head_count,
                credited_heads=credited,
                wait_seconds=wait,
                aging_bonus=bonus,
                priority=credited + bonus,
            )
        )
    return scores


def select_target(scores: List[Score]) -> Optional[int]:
    """Highest priority wins.

    Ties break on longest wait first, then lowest floor number -- both
    deterministic so the simulator and the live dispatcher agree.
    """
    if not scores:
        return None
    best = max(scores, key=lambda s: (s.priority, s.wait_seconds, -s.floor))
    return best.floor


def decide(
    calls: Dict[int, FloorCall],
    head_counts: Dict[int, int],
    now: float,
    aging_factor: float = AGING_FACTOR,
) -> Decision:
    scores = compute_scores(calls, head_counts, now, aging_factor)
    return Decision(target=select_target(scores), scores=scores)
