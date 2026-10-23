import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from workbench_supervisor import batch_is_stalled, next_backoff, restart_due


class WorkbenchSupervisorBackoffTests(unittest.TestCase):
    def test_health_probe_backoff_doubles_and_caps(self):
        delays = [next_backoff(0, 10, 60)]
        for _ in range(5):
            delays.append(next_backoff(delays[-1], 10, 60))

        self.assertEqual([10, 20, 40, 60, 60, 60], delays)

    def test_business_probe_backoff_has_longer_cap(self):
        delays = [next_backoff(0, 20, 120)]
        for _ in range(5):
            delays.append(next_backoff(delays[-1], 20, 120))

        self.assertEqual([20, 40, 80, 120, 120, 120], delays)


class WorkbenchSupervisorLivenessTests(unittest.TestCase):
    def test_running_batch_with_expired_active_lease_is_stalled(self):
        batch={'state':'running','active_tasks':['task_a']}
        self.assertTrue(batch_is_stalled(batch,{'leased':1},['task_a']))
        self.assertFalse(batch_is_stalled(batch,{'leased':1},[]))

    def test_idle_batch_requires_a_leased_task_to_be_stalled(self):
        batch={'state':'running','active_tasks':[]}
        self.assertTrue(batch_is_stalled(batch,{'leased':1},[]))
        self.assertFalse(batch_is_stalled(batch,{'leased':0},[]))
        self.assertFalse(batch_is_stalled({'state':'done'}, {'leased':1}, []))

    def test_restart_requires_four_failures_sixty_seconds_and_cooldown(self):
        self.assertFalse(restart_due(3,0,120,0))
        self.assertFalse(restart_due(4,61,120,0))
        self.assertFalse(restart_due(4,60,120,1))
        self.assertTrue(restart_due(4,60,120,0))


if __name__ == '__main__':
    unittest.main()
