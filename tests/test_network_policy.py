import unittest

from arb_monitor.execution.network_policy import (
    DEFAULT_ROUTES, retry_policy, route_environment,
)


class NetworkPolicyTests(unittest.TestCase):
    def test_verified_proxy_is_first_and_environment_is_explicit(self):
        self.assertEqual(DEFAULT_ROUTES[0].proxy, "http://127.0.0.1:4780")
        env = route_environment(DEFAULT_ROUTES[0])
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:4780")

    def test_ambiguous_write_is_never_replayed(self):
        self.assertEqual(
            retry_policy(write=True, may_have_reached_remote=True),
            "STOP_AND_QUERY_IDEMPOTENCY_KEY",
        )
        self.assertEqual(
            retry_policy(write=True, may_have_reached_remote=False),
            "STOP_AND_ESCALATE",
        )


if __name__ == "__main__":
    unittest.main()
