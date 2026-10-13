"""One verdict for "may this row be resolved by walking the route once?".

Two readers ask this question: the board (``scripts/xstocks_v2_board.py``, which
lists the probes the operator may run) and the resident live process
(``live.probe_candidates``). Until 2026-10-09 they answered it with two
different rules -- the board looked only at the plan's ``waiting_on`` and the
spread, while the gate also looked at the row's blockers -- so the same snapshot
could show "能跑" on the board and be refused by the gate (operator,
2026-10-09: 避免「板面说能跑、实盘拒」). The rule lives here once now, and both
readers call it.

What the rule says:

* a missing **route sample** is not a refusal -- it is exactly what a probe is
  for (operator, 2026-10-08: 缺样本的触发条件是用最小可行额走一次试探);
* **two stages, one implementation.** ``require_spread=True`` is the
  trade-shaped probe: the spread clears this session's bar, and nothing but a
  sample is in the way. ``require_spread=False`` is the **sample** probe
  (operator, 2026-10-09: 任何桥/提币路线，只要缺「这个币在这条边到账过」的样本，
  触发条件就是用最小可行额走一次试探，不是拒绝理由). It drops the spread
  question and tolerates ``below_threshold`` / ``below_net``, because both are
  estimates built from the numbers the crossing replaces. Measured 2026-10-09:
  2,650 rows a day carried a missing sample and 2,620 were refused by exactly
  those two estimates, so the route could never be sampled at all;
* ``below_net`` **is** a veto. The probe buys for real; a row whose own numbers
  say the trade loses money is not made better by buying less of it. This is the
  same judgement ``scan.probe_eligible`` already made, so the board now agrees
  with the scan as well as with the gate;
* a missing **input** the plan needs (today: the bridge fee, ``cost_unknown``) is
  reported as a missing input rather than as a different problem -- but it still
  stops the probe, because a probe cannot be budgeted without it. Fixing that is
  a bug fix, not a reason to keep the row waiting (A3 reads the fee off the
  chain every round).

The sample stage spends money, so it is capped: ``budget_decide`` allows at most
``max_per_day`` probes a day and refuses any single probe above
``max_usdt_per_probe`` (operator 2026-10-09 defaults: 3 probes / 2 USDT). Unset
caps refuse -- "no limit" is never the answer to a missing number.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

#: Blockers one crossing can settle. Everything else is a different problem,
#: and probing on top of it spends money to unlock nothing.
PROBE_CLEARABLE = frozenset({"unmeasured_route", "bridge_unmeasured_symbol"})
#: Blockers that do not change the observation itself.
INFORMATIONAL = frozenset({"phase_scan", "chain_out_of_phase"})
#: A number the plan needs and does not have.
MISSING_INPUT = frozenset({"cost_unknown"})
#: Verdicts that are *estimates from numbers the crossing itself replaces*: the
#: spread was measured before the route was priced, and ``below_net`` was
#: computed with an unpriced fee and an arrival nobody has seen. Telling a route
#: "you lose money" from those numbers kept 2,620 rows a day walking nowhere
#: (measured 2026-10-09). The sample stage tolerates them and reports them as
#: evidence; every other blocker still refuses.
ESTIMATE_ONLY = frozenset({"below_threshold", "below_net"})
TOLERATED = INFORMATIONAL | MISSING_INPUT

#: Daily budget file: a small counter next to the other runtime state. It exists
#: so "3 次/天、每次 ≤2 USDT" is a fact a restart cannot reset.
BUDGET_NAME = "xstocks_v2_probe_budget.json"


def _d(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def blocker_set(row: dict) -> set[str]:
    return {str(item) for item in (row.get("blockers") or [])}


def eligible(blockers, *, require_spread: bool = True) -> bool:
    """Is the *only* thing between this row and a trade a route sample?

    ``scan.probe_eligible`` and ``live.probe_candidates`` both mean this, and
    both now ask here. ``cost_unknown`` is not tolerated either way: the row is
    shaped like a probe but cannot be budgeted until that number exists.

    ``require_spread=False`` is the sample stage. It tolerates the two estimates
    a crossing would replace (``ESTIMATE_ONLY``) and nothing more.
    """
    names = {str(item) for item in (blockers or [])}
    if not names & PROBE_CLEARABLE:
        return False
    tolerated = INFORMATIONAL if require_spread else (INFORMATIONAL | ESTIMATE_ONLY)
    return not (names - PROBE_CLEARABLE - tolerated)


def sample_eligible(blockers) -> bool:
    """``eligible`` for the sample stage: 缺样本不是拒绝理由."""
    return eligible(blockers, require_spread=False)


def plannable(blockers, *, require_spread: bool = True) -> bool:
    """``eligible`` widened by exactly one blocker: an unpriced bridge fee.

    Such a row gets a plan so the board can name what it is waiting for, but it
    is not eligible to run until that number exists.

    ``require_spread=False`` also tolerates the estimates (``ESTIMATE_ONLY``),
    so a row whose only other objection is a stale price still gets the plan
    that a sample probe would use.
    """
    names = {str(item) for item in (blockers or [])}
    if not names & PROBE_CLEARABLE:
        return False
    tolerated = TOLERATED if require_spread else (TOLERATED | ESTIMATE_ONLY)
    return not (names - PROBE_CLEARABLE - tolerated)


def sample_plannable(blockers) -> bool:
    """``plannable`` for the sample stage."""
    return plannable(blockers, require_spread=False)


def verdict(row: dict, *, policy=None, snapshot: dict | None = None,
            require_spread: bool = True) -> dict:
    """``{"probe": bool, "reason": str, ...}`` for one row. Never raises.

    Every question is a refusal when it cannot be answered: a spread we cannot
    read, a threshold we do not know, an unknown minimum, a plan that is not
    there.

    ``require_spread=False`` drops the "spread clears the bar" question and
    tolerates ``below_threshold`` / ``below_net``. It does **not** drop the plan
    questions: a sample probe still has to know what to buy and how to pay.
    """
    spread = _d(row.get("edge_t0_bps"))
    required = _d(row.get("required_edge_bps"))
    if required is None and snapshot is not None:
        required = _d(snapshot.get("threshold_bps"))
    if required is None and policy is not None:
        required = policy.min_net_edge_bps
    if required is None and require_spread:
        return {"probe": False, "reason": "no_threshold"}
    base = ({"required_edge_bps": format(required, "f")} if required is not None
            else {})
    if spread is None:
        return {"probe": False, "reason": "no_spread", **base}
    base["spread_bps"] = format(spread, "f")
    if require_spread and spread < required:
        return {"probe": False, "reason": "below_threshold", **base}
    blockers = blocker_set(row)
    if not blockers & PROBE_CLEARABLE:
        return {"probe": False, "reason": "no_sample_missing", **base}
    tolerated = TOLERATED if require_spread else (TOLERATED | ESTIMATE_ONLY)
    leftover = blockers - PROBE_CLEARABLE - tolerated
    if leftover:
        return {"probe": False, "reason": "other_blockers",
                "blocked_by": sorted(leftover), **base}
    plan = row.get("probe") or {}
    if not plan:
        return {"probe": False, "reason": "no_probe_plan", **base}
    if plan.get("waiting_on"):
        return {"probe": False, "reason": "probe_inputs_unknown",
                "waiting_on": list(plan["waiting_on"]), **base}
    if require_spread:
        return {"probe": True, "reason": "spread_clears_bar", "stage": "probe",
                "plan": plan, **base}
    return {"probe": True, "reason": "sample_missing", "stage": "sample",
            "estimates": sorted(blockers & ESTIMATE_ONLY), "plan": plan, **base}


def sample_verdict(row: dict, *, policy=None, snapshot: dict | None = None) -> dict:
    """``verdict`` for the sample stage: 缺样本不是拒绝理由 (operator 2026-10-09)."""
    return verdict(row, policy=policy, snapshot=snapshot, require_spread=False)


def ready(row: dict, *, policy=None, snapshot: dict | None = None) -> bool:
    """``verdict`` as a boolean, for a reader that only needs the answer."""
    return bool(verdict(row, policy=policy, snapshot=snapshot).get("probe"))


def sample_ready(row: dict, *, policy=None, snapshot: dict | None = None) -> bool:
    """``sample_verdict`` as a boolean."""
    return bool(sample_verdict(row, policy=policy, snapshot=snapshot).get("probe"))


# ---------------------------------------------------------------------------
# Budget: 3 probes a day, 2 USDT each (operator 2026-10-09)
# ---------------------------------------------------------------------------
def budget_day(now_ms: int) -> str:
    """The UTC day a spend belongs to. UTC so a restart cannot reset the count."""
    return time.strftime("%Y-%m-%d", time.gmtime(int(now_ms) / 1000))


def budget_decide(*, state: dict | None, now_ms: int, size_usdt,
                  max_per_day: int, max_usdt_per_probe) -> tuple[bool, str, dict]:
    """``(allowed, reason, state)`` for one sample probe. Never raises.

    Unset caps refuse. So does an unknown size: a probe whose amount cannot be
    read cannot be called "small".
    """
    state = _budget_normalise(state, now_ms)
    cap = _d(max_usdt_per_probe)
    if int(max_per_day or 0) <= 0 or cap is None or cap <= 0:
        return False, "cap_not_set", state
    size = _d(size_usdt)
    if size is None or size <= 0:
        return False, "size_unknown", state
    if size > cap:
        return False, "above_cap", state
    if state["count"] >= int(max_per_day):
        return False, "daily_cap", state
    return True, "ok", state


def budget_charge(state: dict | None, *, now_ms: int, size_usdt) -> dict:
    """Record one spent probe. Called only after a probe really ran."""
    state = _budget_normalise(state, now_ms)
    size = _d(size_usdt)
    spent = _d(state.get("spent_usdt")) or Decimal(0)
    state["count"] = int(state["count"]) + 1
    if size is not None and size > 0:
        spent += size
    state["spent_usdt"] = format(spent, "f")
    state["last_ms"] = int(now_ms)
    return state


def budget_reserve(state: dict | None, *, now_ms: int, size_usdt) -> dict:
    """Charge the cap **before** the walk, not after it.

    A81 (2026-10-09): the counter was charged only once the cycle returned, so
    a process killed mid-probe spent money the day never counted -- an eth TSLAx
    sample really bought 1.12 USDC while the file still said 4 probes, not 5.
    A cap that can be escaped by killing the process is not a cap.

    Reserving early can only ever over-count: a reservation that never becomes
    a spend is given back by ``budget_release``, and a crash between the two
    leaves the day's budget *smaller* than the money spent, never larger.
    """
    return budget_charge(state, now_ms=now_ms, size_usdt=size_usdt)


def budget_release(state: dict | None, *, size_usdt) -> dict:
    """Give back a reservation that never became a spend. Never goes negative."""
    state = dict(state or {})
    size = _d(size_usdt)
    state["count"] = max(0, int(state.get("count") or 0) - 1)
    spent = _d(state.get("spent_usdt")) or Decimal(0)
    if size is not None and size > 0:
        spent -= size
    # ``0`` and not ``0.00``: the day-rollover path writes ``"0"``, and one
    # counter that prints two ways is two counters to a diff.
    state["spent_usdt"] = "0" if spent <= 0 else format(spent, "f")
    return state


def budget_load(path: Path | str) -> dict:
    """Read the budget file. A missing or broken file is an empty day."""
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def budget_save(path: Path | str, state: dict) -> None:
    """Write the budget file atomically. Failure never blocks a trade."""
    target = Path(path)
    temporary = target.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True))
        temporary.replace(target)
    except OSError:
        temporary.unlink(missing_ok=True)


def _budget_normalise(state: dict | None, now_ms: int) -> dict:
    """Roll the counter over when the UTC day changed."""
    day = budget_day(now_ms)
    state = dict(state or {})
    if str(state.get("day") or "") != day:
        state = {"day": day, "count": 0, "spent_usdt": "0"}
    state["count"] = int(state.get("count") or 0)
    state["spent_usdt"] = format(_d(state.get("spent_usdt")) or Decimal(0), "f")
    return state
