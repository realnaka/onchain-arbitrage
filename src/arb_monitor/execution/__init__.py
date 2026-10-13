"""Execution primitives.

The package is deliberately adapter-first: strategies create declarative
intents, while adapters perform pre-trade checks and (when explicitly
enabled) external side effects.
"""

from .intent import ExecutionIntent, ExecutionLeg, LegKind, OrderType
from .state_machine import ExecutionCoordinator, ExecutionState, ExecutionResult
from .risk import ExecutionLimits, PreTradeRisk, RiskDecision
from .kill_switch import KillSwitch
from .mev import MEVPolicy, SafeBroadcaster
from .bridge import execution_intent_from_monitor

__all__ = [
    "ExecutionIntent", "ExecutionLeg", "LegKind", "OrderType",
    "ExecutionCoordinator", "ExecutionState", "ExecutionResult",
    "ExecutionLimits", "PreTradeRisk", "RiskDecision", "KillSwitch",
    "MEVPolicy", "SafeBroadcaster",
    "execution_intent_from_monitor",
]
