import sys
import os
import time
import numpy as np
import unittest
from unittest.mock import patch

import ray
from ray import tune
from ray.tune.ray_trial_executor import RayTrialExecutor
from ray.tune.trial import Trial
from ray.tune import Callback
from ray.tune.utils.placement_groups import PlacementGroupFactory
from ray.util import placement_group_table
from ray.cluster_utils import Cluster
from ray.rllib import _register_all


class TrialRunnerPlacementGroupTest(unittest.TestCase):
    def setUp(self):
        os.environ["TUNE_GLOBAL_CHECKPOINT_S"] = "10000"
        self.head_cpus = 8
        self.head_gpus = 4
        self.head_custom = 16

        self.cluster = Cluster(
            initialize_head=True,
            connect=True,
            head_node_args={
                "include_dashboard": False,
                "num_cpus": self.head_cpus,
                "num_gpus": self.head_gpus,
                "resources": {
                    "custom": self.head_custom
                },
                "_system_config": {
                    "num_heartbeats_timeout": 10
                }
            })
        # Pytest doesn't play nicely with imports
        _register_all()

    def tearDown(self):
        ray.shutdown()
        self.cluster.shutdown()
        _register_all()  # re-register the evicted objects

    def _assertCleanup(self, trial_executor):
        # Assert proper cleanup
        pg_manager = trial_executor._pg_manager
        self.assertFalse(pg_manager._in_use_trials)
        self.assertFalse(pg_manager._in_use_pgs)
        self.assertFalse(pg_manager._staging_futures)
        for pgf in pg_manager._staging:
            self.assertFalse(pg_manager._staging[pgf])
        for pgf in pg_manager._ready:
            self.assertFalse(pg_manager._ready[pgf])
        self.assertTrue(pg_manager._latest_staging_start_time)

        num_non_removed_pgs = len([
            p for pid, p in placement_group_table().items()
            if p["state"] != "REMOVED"
        ])
        self.assertEqual(num_non_removed_pgs, 0)

    def testPlacementGroupRequests(self, reuse_actors=False, scheduled=10):
        """In this test we try to start 10 trials but only have resources
        for 2. Placement groups should still be created and PENDING.

        Eventually they should be scheduled sequentially (i.e. in pairs
        of two)."""

        def train(config):
            time.sleep(1)
            now = time.time()
            tune.report(end=now - config["start_time"])

        head_bundle = {"CPU": 4, "GPU": 0, "custom": 0}
        child_bundle = {"custom": 1}

        placement_group_factory = PlacementGroupFactory(
            [head_bundle, child_bundle, child_bundle])

        trial_executor = RayTrialExecutor(reuse_actors=reuse_actors)

        this = self

        class _TestCallback(Callback):
            def on_step_end(self, iteration, trials, **info):
                num_finished = len([
                    t for t in trials
                    if t.status == Trial.TERMINATED or t.status == Trial.ERROR
                ])

                num_staging = sum(
                    len(s)
                    for s in trial_executor._pg_manager._staging.values())
                num_ready = sum(
                    len(s) for s in trial_executor._pg_manager._ready.values())
                num_in_use = len(trial_executor._pg_manager._in_use_pgs)
                num_cached = len(trial_executor._pg_manager._cached_pgs)

                total_num_tracked = num_staging + num_ready + \
                    num_in_use + num_cached

                num_non_removed_pgs = len([
                    p for pid, p in placement_group_table().items()
                    if p["state"] != "REMOVED"
                ])
                num_removal_scheduled_pgs = len(
                    trial_executor._pg_manager._pgs_for_removal)

                # All trials should be scheduled
                this.assertEqual(
                    scheduled,
                    min(scheduled, len(trials)),
                    msg=f"Num trials iter {iteration}")
                # The number of PGs should decrease when trials finish
                this.assertEqual(
                    max(scheduled, len(trials)) - num_finished,
                    total_num_tracked,
                    msg=f"Num tracked iter {iteration}")
                # The number of actual placement groups should match this
                this.assertEqual(
                    max(scheduled, len(trials)) - num_finished,
                    num_non_removed_pgs - num_removal_scheduled_pgs,
                    msg=f"Num actual iter {iteration}")

        start = time.time()
        out = tune.run(
            train,
            config={"start_time": start},
            resources_per_trial=placement_group_factory,
            num_samples=10,
            trial_executor=trial_executor,
            callbacks=[_TestCallback()],
            reuse_actors=reuse_actors,
            verbose=2)

        trial_end_times = sorted(t.last_result["end"] for t in out.trials)
        print("Trial end times:", trial_end_times)
        max_diff = trial_end_times[-1] - trial_end_times[0]

        # Not all trials have been run in parallel
        self.assertGreater(max_diff, 3)

        # Some trials should have run in parallel
        # Todo: Re-enable when using buildkite
        # self.assertLess(max_diff, 10)

        self._assertCleanup(trial_executor)

    def testPlacementGroupRequestsWithActorReuse(self):
        """Assert that reuse actors doesn't leak placement groups"""
        self.testPlacementGroupRequests(reuse_actors=True)

    @patch("ray.tune.trial_runner.TUNE_MAX_PENDING_TRIALS_PG", 6)
    @patch("ray.tune.utils.placement_groups.TUNE_MAX_PENDING_TRIALS_PG", 6)
    def testPlacementGroupLimitedRequests(self):
        """Assert that maximum number of placement groups is enforced."""
        self.testPlacementGroupRequests(scheduled=6)

    @patch("ray.tune.trial_runner.TUNE_MAX_PENDING_TRIALS_PG", 6)
    @patch("ray.tune.utils.placement_groups.TUNE_MAX_PENDING_TRIALS_PG", 6)
    def testPlacementGroupLimitedRequestsWithActorReuse(self):
        self.testPlacementGroupRequests(reuse_actors=True, scheduled=6)

    def testPlacementGroupDistributedTraining(self, reuse_actors=False):
        """Run distributed training using placement groups.

        Each trial requests 4 CPUs and starts 4 remote training workers.
        """

        head_bundle = {"CPU": 1, "GPU": 0, "custom": 0}
        child_bundle = {"CPU": 1}

        placement_group_factory = PlacementGroupFactory(
            [head_bundle, child_bundle, child_bundle, child_bundle])

        @ray.remote
        class TrainingActor:
            def train(self, val):
                time.sleep(1)
                return val

        def train(config):
            base = config["base"]
            actors = [TrainingActor.remote() for _ in range(4)]
            futures = [
                actor.train.remote(base + 2 * i)
                for i, actor in enumerate(actors)
            ]
            results = ray.get(futures)

            end = time.time() - config["start_time"]
            tune.report(avg=np.mean(results), end=end)

        trial_executor = RayTrialExecutor(reuse_actors=reuse_actors)

        start = time.time()
        out = tune.run(
            train,
            config={
                "start_time": start,
                "base": tune.grid_search(list(range(0, 100, 10)))
            },
            resources_per_trial=placement_group_factory,
            num_samples=1,
            trial_executor=trial_executor,
            reuse_actors=reuse_actors,
            verbose=2)

        avgs = sorted(t.last_result["avg"] for t in out.trials)
        self.assertSequenceEqual(avgs, list(range(3, 103, 10)))

        trial_end_times = sorted(t.last_result["end"] for t in out.trials)
        print("Trial end times:", trial_end_times)
        max_diff = trial_end_times[-1] - trial_end_times[0]

        # Not all trials have been run in parallel
        self.assertGreater(max_diff, 3)

        # Some trials should have run in parallel
        # Todo: Re-enable when using buildkite
        # self.assertLess(max_diff, 10)

        self._assertCleanup(trial_executor)

    def testPlacementGroupDistributedTrainingWithActorReuse(self):
        self.testPlacementGroupDistributedTraining(reuse_actors=True)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main(["-v", __file__]))
