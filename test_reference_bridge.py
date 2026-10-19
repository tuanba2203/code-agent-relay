from copy import deepcopy
import unittest
from reference_bridge import SnapshotError, plan


def fixture():
    accounts = []
    for i, used in enumerate(([93, 20], [10, 75], [40, 45], [90, 10])):
        accounts.append({"account_key": "user-%d::workspace" % i, "alias": "account-%d" % i,
                         "auth_mode": "chatgpt", "active": i == 0,
                         "usage": {"source": "api", "updated_at": 1,
                                   "primary": {"used_percent": used[0], "resets_at": 10000},
                                   "secondary": {"used_percent": used[1], "resets_at": 20000},
                                   "refresh": {"requested": True, "method": "api", "status": "ok"}}})
    return {"received_at": 100, "result": {"schema_version": 1, "command": "list",
                                           "active_account_key": accounts[0]["account_key"],
                                           "accounts": accounts}}


class BridgeTests(unittest.TestCase):
    def test_selects_balanced_quota_not_display_row(self):
        result = plan(fixture(), now=100)
        self.assertEqual(result["target"], "user-2::workspace")
        self.assertEqual(result["action"], "prepare_handoff")
        self.assertFalse(result["executed"])

    def test_equal_api_response_may_have_old_updated_at(self):
        self.assertTrue(plan(fixture(), now=100)["accounts"][2]["eligible"])

    def test_healthy_account_kept(self):
        data = fixture()
        data["result"]["accounts"][0]["usage"]["primary"]["used_percent"] = 20
        self.assertEqual(plan(data, now=100)["action"], "keep")

    def test_expired_window_does_not_create_free_quota(self):
        data = fixture()
        for account in data["result"]["accounts"][1:]:
            account["usage"]["primary"]["resets_at"] = 99
        self.assertEqual(plan(data, now=100)["action"], "pause")

    def test_failed_refresh_rejects_cached_percentage(self):
        data = fixture()
        for account in data["result"]["accounts"][1:]:
            account["usage"]["refresh"]["status"] = "http_error"
        self.assertEqual(plan(data, now=100)["action"], "pause")

    def test_stale_and_future_envelope_rejected(self):
        for observed in (0, 101, None, True):
            data = fixture()
            data["received_at"] = observed
            with self.assertRaises(SnapshotError):
                plan(data, now=100)

    def test_unknown_schema_rejected(self):
        data = fixture()
        data["result"]["schema_version"] = 2
        with self.assertRaises(SnapshotError):
            plan(data, now=100)

    def test_duplicate_identity_rejected(self):
        data = fixture()
        data["result"]["accounts"].append(deepcopy(data["result"]["accounts"][0]))
        with self.assertRaises(SnapshotError):
            plan(data, now=100)

    def test_inconsistent_active_account_rejected(self):
        data = fixture()
        data["result"]["accounts"][1]["active"] = True
        with self.assertRaises(SnapshotError):
            plan(data, now=100)

    def test_does_not_mutate_snapshot(self):
        data = fixture()
        original = deepcopy(data)
        plan(data, now=100)
        self.assertEqual(data, original)

    def test_rejects_api_auth_for_seat_quota(self):
        data = fixture()
        for account in data["result"]["accounts"][1:]:
            account["auth_mode"] = "apikey"
        self.assertEqual(plan(data, now=100)["action"], "pause")


if __name__ == '__main__':
    unittest.main()
