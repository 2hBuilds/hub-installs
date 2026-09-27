import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fetch_installs  # noqa: E402

BOOTSTRAP = {
    "version": "1.12.39",
    "artifacts": [
        {"name": "http-api-1.2.23.jar", "path": "x"},
        {"name": "okhttp-3.14.9.jar", "path": "x"},
        {"name": "client-1.12.39.jar", "path": "x"},
    ],
}


def fake_fetch(feed, bootstrap=BOOTSTRAP):
    seen = []

    def fetch(url):
        seen.append(url)
        if url == fetch_installs.BOOTSTRAP_URL:
            return bootstrap
        return feed

    fetch.seen = seen
    return fetch


def setUpModule():
    global _quiet
    _quiet = contextlib.redirect_stdout(io.StringIO())
    _quiet.__enter__()


def tearDownModule():
    _quiet.__exit__(None, None, None)


class ClientVersionTest(unittest.TestCase):
    def test_reads_the_client_artifact_when_it_is_not_first(self):
        self.assertEqual(fetch_installs.client_version(BOOTSTRAP), "1.12.39")

    def test_prefers_an_explicit_version_field(self):
        bootstrap = {"artifacts": [{"name": "okio-1.17.2.jar"}, {"name": "client-9.9.9.jar", "version": "1.2.3"}]}
        self.assertEqual(fetch_installs.client_version(bootstrap), "1.2.3")

    def test_no_client_artifact_is_an_error(self):
        with self.assertRaises(fetch_installs.FetchError):
            fetch_installs.client_version({"artifacts": [{"name": "okio-1.17.2.jar"}]})

    def test_feed_url_uses_the_picked_version(self):
        with tempfile.TemporaryDirectory() as root:
            write_plugins(root, "a")
            fetch = fake_fetch({"a": 1})
            fetch_installs.run(root, fetch, "2026-09-27")
            self.assertEqual(fetch.seen[1], "https://api.runelite.net/runelite-1.12.39/pluginhub")


def write_plugins(root, *slugs):
    with open(os.path.join(root, "plugins.txt"), "w", encoding="utf-8") as f:
        f.write("# comment\n\n" + "\n".join(slugs) + "\n")


def read_series(root):
    with open(os.path.join(root, "data", "installs.csv"), encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


class SeriesTest(unittest.TestCase):
    def test_same_day_rerun_replaces_rather_than_duplicates(self):
        with tempfile.TemporaryDirectory() as root:
            write_plugins(root, "a", "b")
            fetch_installs.run(root, fake_fetch({"a": 1, "b": 2}), "2026-09-26")
            fetch_installs.run(root, fake_fetch({"a": 5, "b": 6}), "2026-09-27")
            fetch_installs.run(root, fake_fetch({"a": 7, "b": 8}), "2026-09-27")
            self.assertEqual(read_series(root), [
                ["date", "plugin", "installs"],
                ["2026-09-26", "a", "1"],
                ["2026-09-26", "b", "2"],
                ["2026-09-27", "a", "7"],
                ["2026-09-27", "b", "8"],
            ])

    def test_a_tracked_plugin_missing_from_the_feed_gets_an_empty_field(self):
        with tempfile.TemporaryDirectory() as root:
            write_plugins(root, "a", "gone")
            missing = fetch_installs.run(root, fake_fetch({"a": 3}), "2026-09-27")
            self.assertEqual(missing, ["gone"])
            self.assertIn(["2026-09-27", "gone", ""], read_series(root))

    def test_untracked_plugins_stay_out_of_the_series_but_in_the_day_file(self):
        with tempfile.TemporaryDirectory() as root:
            write_plugins(root, "a")
            fetch_installs.run(root, fake_fetch({"a": 3, "big": 900, "c": 3}), "2026-09-27")
            self.assertEqual(len(read_series(root)), 2)
            with open(os.path.join(root, "data", "all", "2026-09-27.csv"), encoding="utf-8") as f:
                self.assertEqual(f.read(), "plugin,installs\nbig,900\na,3\nc,3\n")


class LatestTest(unittest.TestCase):
    def test_latest_json_holds_date_version_and_every_count(self):
        with tempfile.TemporaryDirectory() as root:
            write_plugins(root, "a")
            fetch_installs.run(root, fake_fetch({"a": 3, "b": 4}), "2026-09-27")
            with open(os.path.join(root, "data", "latest.json"), encoding="utf-8") as f:
                latest = json.load(f)
            self.assertEqual(latest, {"date": "2026-09-27", "version": "1.12.39", "counts": {"a": 3, "b": 4}})


class FailureTest(unittest.TestCase):
    def test_a_bad_feed_fails_and_leaves_history_alone(self):
        with tempfile.TemporaryDirectory() as root:
            write_plugins(root, "a")
            fetch_installs.run(root, fake_fetch({"a": 1}), "2026-09-26")
            before = read_series(root)
            for bad in ({}, [], {"a": "many"}, {"a": True}):
                with self.assertRaises(fetch_installs.FetchError):
                    fetch_installs.run(root, fake_fetch(bad), "2026-09-27")
            self.assertEqual(read_series(root), before)

    def test_main_exits_non_zero_on_a_fetch_failure(self):
        def broken(url):
            raise fetch_installs.FetchError("no network")

        original = fetch_installs.fetch_json
        fetch_installs.fetch_json = broken
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(fetch_installs.main(), 1)
            self.assertIn("no network", err.getvalue())
        finally:
            fetch_installs.fetch_json = original

if __name__ == "__main__":
    unittest.main()
