"""Persistence for per-SKU cost mapping (nm_id -> cost)."""
from __future__ import annotations

import json
from pathlib import Path

COSTS_PATH = Path("data/costs.json")


def load_costs() -> dict[int, float]:
    if not COSTS_PATH.exists():
        return {}
    raw = json.loads(COSTS_PATH.read_text())
    return {int(k): float(v) for k, v in raw.items()}


def save_costs(costs: dict[int, float]) -> None:
    COSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    serializable = {str(k): float(v) for k, v in costs.items()}
    COSTS_PATH.write_text(json.dumps(serializable, indent=2, ensure_ascii=False))


def update_costs(updates: dict[int, float]) -> tuple[int, int]:
    """Merge updates into stored costs. Returns (added, changed)."""
    current = load_costs()
    added = changed = 0
    for nm, cost in updates.items():
        if nm not in current:
            added += 1
        elif current[nm] != cost:
            changed += 1
        current[nm] = cost
    save_costs(current)
    return added, changed
