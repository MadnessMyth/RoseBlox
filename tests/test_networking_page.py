import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

from roseblox.backend.network_history import ConnectionRecord, RegionStat  # noqa: E402
from roseblox.pages.networking_page import NetworkingPage  # noqa: E402


class FakeStore:
    """In-memory stand-in so the page is tested without touching disk or the network."""

    def __init__(self, records=None, regions=None):
        self._records = list(records or [])
        self._regions = list(regions or [])
        self.updates = []
        self.deleted = []
        self.cleared = False

    def load(self, *, retention_days=None):
        return list(self._records)

    def best_regions(self, *, retention_days=None):
        return list(self._regions)

    def update_ping(self, address, *, region=None, ping_ms=None):
        self.updates.append((address, region, ping_ms))

    def delete(self, address):
        self.deleted.append(address)
        self._records = [r for r in self._records if r.address != address]
        return True

    def clear(self):
        self.cleared = True
        self._records = []
        self._regions = []


class NetworkingPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Adw.init()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.directory.cleanup()

    def _record(self, address, region="Frankfurt, Germany", last_ping=24, best_ping=22,
                connections=2, last_seen=1000.0, place_id=123):
        return ConnectionRecord(
            address=address, region=region, best_ping_ms=best_ping, last_ping_ms=last_ping,
            connections=connections, last_seen=last_seen, place_id=place_id, job_id="job-1",
        )

    def _page(self, records=None, regions=None, ping=lambda a: None, locate=lambda a: "Place"):
        return NetworkingPage(None, store=FakeStore(records, regions), ping=ping, locate=locate)

    def test_empty_store_shows_placeholder(self):
        page = self._page()
        self.assertTrue(page.empty_label.get_visible())
        self.assertIn("No connections recorded", page.summary_label.get_label())
        self.assertEqual(page._region_rows, [])
        self.assertEqual(page._server_rows, [])

    def test_populated_store_renders_summary_and_rows(self):
        records = [self._record("1.2.3.4"), self._record("5.6.7.8")]
        regions = [RegionStat(region="Frankfurt, Germany", best_ping_ms=22, connections=2, last_seen=1000.0)]
        page = self._page(records=records, regions=regions)
        self.assertFalse(page.empty_label.get_visible())
        self.assertIn("Frankfurt, Germany", page.summary_label.get_label())
        self.assertIn("22 ms", page.summary_label.get_label())
        self.assertEqual(len(page._region_rows), 1)
        self.assertEqual(page._region_rows[0].get_title(), "Frankfurt, Germany")
        self.assertEqual(len(page._server_rows), 2)
        self.assertEqual(page._server_rows[0].get_title(), "1.2.3.4")
        self.assertIn("place 123", page._server_rows[0].get_subtitle())

    def test_each_server_row_has_a_delete_button(self):
        records = [self._record("1.2.3.4"), self._record("5.6.7.8")]
        page = self._page(records=records, regions=[])
        self.assertEqual(len(page._server_rows), 2)
        self.assertEqual(len(page._server_delete_buttons), 2)

    def test_delete_button_removes_the_server_and_refreshes(self):
        records = [self._record("1.2.3.4"), self._record("5.6.7.8")]
        page = self._page(records=records, regions=[])
        page._on_delete_clicked(None, "1.2.3.4")
        self.assertIn("1.2.3.4", page.store.deleted)
        self.assertEqual([r.get_title() for r in page._server_rows], ["5.6.7.8"])

    def test_clear_with_empty_store_skips_the_dialog(self):
        page = self._page(records=[], regions=[])
        # No exception, no clear called: the page bails out before building a dialog.
        page._on_clear_clicked(None)
        self.assertFalse(page.store.cleared)

    def test_clear_response_clears_the_store_and_refreshes(self):
        records = [self._record("1.2.3.4"), self._record("5.6.7.8")]
        page = self._page(records=records, regions=[])
        page._on_clear_response(mock.Mock(), "clear")
        self.assertTrue(page.store.cleared)
        self.assertEqual(page._server_rows, [])

    def test_clear_response_cancel_does_not_clear(self):
        records = [self._record("1.2.3.4")]
        page = self._page(records=records, regions=[])
        page._on_clear_response(mock.Mock(), "cancel")
        self.assertFalse(page.store.cleared)
        self.assertEqual(len(page._server_rows), 1)

    def test_refresh_re_pings_every_server_and_fills_missing_regions(self):
        records = [self._record("1.2.3.4", region=None), self._record("5.6.7.8", region=None)]
        page = self._page(records=records, regions=[], ping=lambda a: 30, locate=lambda a: "Singapore")
        page._refresh_worker()  # run the worker body directly; GLib.idle_add is queued, not needed for the assertion
        self.assertEqual(len(page.store.updates), 2)
        for address, region, ping_ms in page.store.updates:
            self.assertEqual(ping_ms, 30)
            self.assertEqual(region, "Singapore")

    def test_refresh_skips_servers_that_cannot_be_pinged_or_located(self):
        records = [self._record("1.2.3.4", region=None)]
        page = self._page(records=records, regions=[], ping=lambda a: None, locate=self._raise)
        page._refresh_worker()
        self.assertEqual(page.store.updates, [])

    @staticmethod
    def _raise(address):
        from roseblox.backend.game_info import GameInfoError
        raise GameInfoError("unreachable")


if __name__ == "__main__":
    unittest.main()
