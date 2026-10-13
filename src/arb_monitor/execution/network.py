"""Fail-closed network request and order-state primitives.

These classes deliberately do not perform I/O.  Adapters can use them to
classify ambiguous responses without ever treating a timeout as a confirmed
failure (which could cause a duplicate order).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import monotonic
from typing import Callable, Generic, TypeVar
from uuid import uuid4


class NetworkState(str, Enum):
    HEALTHY = "healthy"
    DATA_UNAVAILABLE = "data_unavailable"
    REQUEST_TIMEOUT = "request_timeout"
    RESPONSE_UNKNOWN = "response_unknown"
    PRIVATE_API_UNAVAILABLE = "private_api_unavailable"
    CHAIN_BROADCAST_UNKNOWN = "chain_broadcast_unknown"
    HOST_UNAVAILABLE = "host_unavailable"


class NetworkFault(RuntimeError):
    """A transport failure whose remote side may have processed the request."""

    def __init__(self, message: str, state: NetworkState,
                 may_have_reached_remote: bool = False):
        super().__init__(message)
        self.state = state
        self.may_have_reached_remote = may_have_reached_remote


class RemoteOrderState(str, Enum):
    UNKNOWN = "unknown"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    venue: str
    endpoint: str
    sent_at: float
    timeout_s: float
    attempts: int = 0
    response_received_at: float | None = None
    may_have_reached_remote: bool = False
    state: NetworkState = NetworkState.HEALTHY
    confirmation_source: str | None = None


@dataclass
class RequestTracker:
    """Track idempotent requests and classify ambiguous outcomes."""

    records: dict[str, RequestRecord] = field(default_factory=dict)

    def start(self, venue: str, endpoint: str, timeout_s: float = 5.0,
              request_id: str | None = None) -> RequestRecord:
        rid = request_id or uuid4().hex
        record = RequestRecord(rid, venue, endpoint, monotonic(), timeout_s)
        self.records[rid] = record
        return record

    def outcome(self, request_id: str, state: NetworkState,
                *, may_have_reached_remote: bool = False,
                confirmation_source: str | None = None) -> RequestRecord:
        old = self.records[request_id]
        updated = RequestRecord(
            old.request_id, old.venue, old.endpoint, old.sent_at, old.timeout_s,
            old.attempts + 1, monotonic(), may_have_reached_remote, state,
            confirmation_source,
        )
        self.records[request_id] = updated
        return updated

    def retry_allowed(self, request_id: str, max_attempts: int = 3) -> bool:
        record = self.records[request_id]
        return record.attempts < max_attempts and not (
            record.may_have_reached_remote
            and record.state in {NetworkState.REQUEST_TIMEOUT,
                                 NetworkState.RESPONSE_UNKNOWN,
                                 NetworkState.CHAIN_BROADCAST_UNKNOWN}
        )


T = TypeVar("T")


@dataclass(frozen=True)
class Resolution(Generic[T]):
    state: RemoteOrderState
    value: T | None = None
    source: str = ""


class AmbiguousOrderResolver(Generic[T]):
    """Resolve an order using a query callback, never by blind resubmission."""

    def __init__(self, query: Callable[[str], Resolution[T]]):
        self.query = query

    def resolve(self, client_order_id: str) -> Resolution[T]:
        return self.query(client_order_id)
