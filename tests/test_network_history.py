import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from roseblox.backend.network_history import ConnectionRecord, NetworkHistoryStore, RegionStat


class NetworkHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = NetworkHistoryStore(self.root / "network_history.json", clock=lambda: 1000.0)

    def tearDown(self):
        self.directory.cleanup()

    def test_record_and_load(self):
        self.store.record("1.2.3.4", place_id=123, job_id="job-1", region="Frankfurt, Germany", ping_ms=24)
        records = self.store.load()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.address, "1.2.3.4")
        self.assertEqual(record.region, "Frankfurt, Germany")
        self.assertEqual(record.best_ping_ms, 24)
        self.assertEqual(record.last_ping_ms, 24)
        self.assertEqual(record.connections, 1)
        self.assertEqual(record.place_id, 123)
        self.assertEqual(record.job_id, "job-1")

    def test_private_loopback_and_non_ip_addresses_are_ignored(self):
        self.store.record("10.0.0.1")
        self.store.record("127.0.0.1")
        self.store.record("192.168.1.1")
        self.store.record("not-an-ip")
        self.assertEqual(self.store.load(), [])

    def test_repeated_connections_bump_count_and_keep_best_ping(self):
        self.store.record("1.2.3.4", region="A", ping_ms=40, timestamp=1000.0)
        self.store.record("1.2.3.4", region="A", ping_ms=22, timestamp=2000.0)
        self.store.record("1.2.3.4", region="A", ping_ms=60, timestamp=3000.0)
        records = self.store.load()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.connections, 3)
        self.assertEqual(record.best_ping_ms, 22)
        self.assertEqual(record.last_ping_ms, 60)
        self.assertEqual(record.last_seen, 3000.0)

    def test_load_returns_newest_first(self):
        self.store.record("1.2.3.4", timestamp=1000.0)
        self.store.record("5.6.7.8", timestamp=3000.0)
        self.store.record("9.10.11.12", timestamp=2000.0)
        self.assertEqual([r.address for r in self.store.load()], ["5.6.7.8", "9.10.11.12", "1.2.3.4"])

    def test_best_regions_ranked_by_ping(self):
        self.store.record("1.1.1.1", region="Frankfurt, Germany", ping_ms=24, timestamp=1000.0)
        self.store.record("2.2.2.2", region="Singapore", ping_ms=180, timestamp=2000.0)
        self.store.record("3.3.3.3", region="Frankfurt, Germany", ping_ms=20, timestamp=3000.0)
        regions = self.store.best_regions()
        self.assertEqual([r.region for r in regions], ["Frankfurt, Germany", "Singapore"])
        frankfurt = regions[0]
        self.assertEqual(frankfurt.best_ping_ms, 20)
        self.assertEqual(frankfurt.connections, 2)
        self.assertEqual(frankfurt.last_seen, 3000.0)

    def test_best_regions_without_ping_sort_after_measured(self):
        self.store.record("1.1.1.1", region="Singapore", ping_ms=180, timestamp=1000.0)
        self.store.record("2.2.2.2", region="Unknown Place", timestamp=2000.0)
        regions = self.store.best_regions()
        self.assertEqual(regions[0].region, "Singapore")
        self.assertIsNone(regions[1].best_ping_ms)

    def test_servers_without_a_region_are_not_in_best_regions(self):
        self.store.record("1.1.1.1", ping_ms=24, timestamp=1000.0)
        self.assertEqual(self.store.best_regions(), [])

    def test_update_ping_does_not_bump_count(self):
        self.store.record("1.2.3.4", region="A", ping_ms=40, timestamp=1000.0)
        self.store.update_ping("1.2.3.4", ping_ms=18)
        record = self.store.load()[0]
        self.assertEqual(record.connections, 1)
        self.assertEqual(record.best_ping_ms, 18)
        self.assertEqual(record.last_ping_ms, 18)

    def test_update_ping_fills_missing_region(self):
        self.store.record("1.2.3.4", timestamp=1000.0)
        self.assertIsNone(self.store.load()[0].region)
        self.store.update_ping("1.2.3.4", region="Frankfurt, Germany", ping_ms=24)
        record = self.store.load()[0]
        self.assertEqual(record.region, "Frankfurt, Germany")
        self.assertEqual(record.best_ping_ms, 24)

    def test_update_ping_ignores_unknown_address(self):
        self.store.update_ping("9.9.9.9", ping_ms=10)
        self.assertEqual(self.store.load(), [])

    def test_persistence_across_store_instances(self):
        self.store.record("1.2.3.4", region="A", ping_ms=24, timestamp=1000.0)
        reopened = NetworkHistoryStore(self.root / "network_history.json")
        records = reopened.load()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].address, "1.2.3.4")
        self.assertEqual(records[0].region, "A")

    def test_corrupted_file_is_rebuilt_from_scratch(self):
        self.store.record("1.2.3.4", ping_ms=24, timestamp=1000.0)
        self.store.path.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(self.store.load(), [])
        self.store.record("5.6.7.8", ping_ms=30, timestamp=2000.0)
        self.assertEqual(len(self.store.load()), 1)
        self.assertEqual(self.store.load()[0].address, "5.6.7.8")

    def test_record_uses_clock_when_timestamp_omitted(self):
        store = NetworkHistoryStore(self.root / "nh.json", clock=lambda: 4242.0)
        store.record("1.2.3.4", ping_ms=24)
        self.assertEqual(store.load()[0].last_seen, 4242.0)


if __name__ == "__main__":
    unittest.main()
