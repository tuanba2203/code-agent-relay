import math
from pathlib import Path
import tempfile
import unittest
from coordinator import Coordinator, Handoff, Profile, RotationStopped, Thread, Usage


A, B, C, D = [Profile(str(i), "identity-" + str(i)) for i in range(4)]


class FakeAdapter:
    def __init__(self):
        self.current = A
        self.calls = []
        self.usage_by_name = {A.name: Usage([93, 10], 100),
                              B.name: Usage([15, 20], 100),
                              C.name: Usage([90, 20], 100),
                              D.name: Usage([99, 99], 100)}
        self.fail_summary = False
        self.fail_verify = False
        self.fail_import = False
        self.fail_activate = False

    def usage(self, profile):
        return self.usage_by_name[profile.name]

    def quiesce(self):
        self.calls.append("quiesce")
        return [Thread("desktop", "t1", "/project"), Thread("vscode", "t2", "/project")]

    def summarize(self, thread):
        self.calls.append("summarize:" + thread.frontend)
        return Handoff(thread, "" if self.fail_summary else "Verified state and next steps", True, True)

    def activate(self, profile):
        self.calls.append("activate:" + profile.name)
        self.current = profile
        if self.fail_activate and profile == B:
            raise OSError("partial activation")

    def verify(self, profile):
        return self.current == profile and not (self.fail_verify and profile == B)

    def import_handoff(self, profile, handoff):
        self.calls.append("import:" + handoff.thread.frontend)
        if self.fail_import:
            raise OSError("import disconnected")
        return "new-" + handoff.thread.id

    def release(self):
        self.calls.append("release")


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.adapter = FakeAdapter()
        self.coordinator = Coordinator(self.adapter, [A, B, C, D], A, self.temp.name,
                                       lambda: 100)

    def test_success_checkpoints_both_frontends_before_switch(self):
        self.assertEqual(self.coordinator.rotate_if_needed(), B)
        self.assertEqual(self.adapter.calls, ["quiesce", "summarize:desktop", "summarize:vscode",
                                            "activate:1", "import:desktop", "import:vscode", "release"])
        self.assertFalse(self.coordinator.journal.exists())
        self.assertTrue((Path(self.temp.name) / "last-transaction.json").exists())

    def test_healthy_does_nothing(self):
        self.adapter.usage_by_name[A.name] = Usage([10, 10], 100)
        self.assertIsNone(self.coordinator.rotate_if_needed())
        self.assertEqual(self.adapter.calls, [])

    def test_weekly_limit_also_triggers_switch(self):
        self.adapter.usage_by_name[A.name] = Usage([5, 95], 100)
        self.assertEqual(self.coordinator.rotate_if_needed(), B)

    def test_best_bottleneck_quota_selected_not_first_profile(self):
        self.adapter.usage_by_name[B.name] = Usage([60, 10], 100)
        self.adapter.usage_by_name[C.name] = Usage([20, 25], 100)
        self.assertEqual(self.coordinator.rotate_if_needed(), C)

    def test_new_account_requires_extra_margin(self):
        self.adapter.usage_by_name[B.name] = Usage([85, 20], 100)
        with self.assertRaises(RotationStopped):
            self.coordinator.rotate_if_needed()

    def test_all_exhausted_no_switch_no_loop(self):
        self.adapter.usage_by_name[B.name] = Usage([99], 100)
        with self.assertRaises(RotationStopped):
            self.coordinator.rotate_if_needed()
        self.assertEqual(self.adapter.calls, [])

    def test_summary_failure_preserves_account_and_pauses(self):
        self.adapter.fail_summary = True
        with self.assertRaises(RotationStopped):
            self.coordinator.rotate_if_needed()
        self.assertEqual(self.adapter.current, A)
        self.assertNotIn("release", self.adapter.calls)
        self.assertTrue(self.coordinator.journal.exists())

    def test_identity_mismatch_rolls_back(self):
        self.adapter.fail_verify = True
        with self.assertRaises(RotationStopped):
            self.coordinator.rotate_if_needed()
        self.assertEqual(self.adapter.current, A)
        self.assertNotIn("import:desktop", self.adapter.calls)

    def test_partial_activation_rolls_back(self):
        self.adapter.fail_activate = True
        with self.assertRaises(RotationStopped):
            self.coordinator.rotate_if_needed()
        self.assertEqual(self.adapter.current, A)

    def test_import_failure_stops_and_retains_handoffs(self):
        self.adapter.fail_import = True
        with self.assertRaises(RotationStopped):
            self.coordinator.rotate_if_needed()
        self.assertEqual(self.adapter.current, A)
        self.assertTrue((Path(self.temp.name) / "handoff-000.json").exists())
        self.assertNotIn("release", self.adapter.calls)

    def test_crash_journal_prevents_automatic_replay(self):
        self.coordinator.journal.write_text('{"phase":"activating"}')
        with self.assertRaises(RotationStopped):
            self.coordinator.rotate_if_needed()
        self.assertEqual(self.adapter.calls, [])

    def test_private_storage_permissions(self):
        self.coordinator.rotate_if_needed()
        for path in Path(self.temp.name).glob('*.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_distinct_profiles_required(self):
        with self.assertRaises(ValueError):
            Coordinator(self.adapter, [A, A], A, self.temp.name, lambda: 100)

    def test_missing_stale_invalid_quota_not_eligible(self):
        for value in ([], [math.nan], [-1], [101], [True], [92]):
            self.assertFalse(Usage(value, 100).eligible(100))
        self.assertFalse(Usage([10], 1).eligible(100))
        self.assertFalse(Usage([10], 101).eligible(100))
        self.assertFalse(Usage([10], 100, blocked=True).eligible(100))


if __name__ == '__main__':
    unittest.main()
