import contextlib
import csv
import datetime
import io
import json
import os
import re
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import check_health  # noqa: E402
import fetch_installs  # noqa: E402

SLUG = "bank-portfolio-tracker"
T0 = datetime.datetime(2026, 9, 28, 13, 37, tzinfo=datetime.timezone.utc)
FAILED_AT = 1790605311  # 2026-09-28 14:21:51 UTC


def at(hours):
    return T0 + datetime.timedelta(hours=hours)


def bootstrap(version):
    return {"artifacts": [{"name": "okio-1.17.2.jar"}, {"name": "client-%s.jar" % version}]}


def entry(slug=SLUG, author="2hBuilds", version="1.0.4", **extra):
    e = {"internalName": slug, "displayName": "2h Bank Portfolio Tracker", "author": author, "version": version}
    e.update(extra)
    return e


def manifest(display, jars, signature=512):
    body = json.dumps({"display": display, "jars": [{"internalName": j, "jarSize": 1} for j in jars]}).encode("utf-8")
    return struct.pack(">I", signature) + b"s" * signature + body


class Hub:
    """A fake Hub: the client version and the manifest bytes it serves, changed between runs."""

    def __init__(self, version="1.12.39", body=None):
        self.version = version
        self.body = body
        self.seen = []
        # slug -> the GitHub account its runelite/plugin-hub entry names (default ours), or an exception
        self.repos = {}

    def fetch(self, url):
        self.seen.append(url)
        return bootstrap(self.version)

    def fetch_manifest(self, url):
        self.seen.append(url)
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    def fetch_file(self, url):
        self.seen.append(url)
        slug = url.rsplit("/", 1)[1]
        owner = self.repos.get(slug, "2hBuilds")
        if isinstance(owner, Exception):
            raise owner
        return ("repository=https://github.com/%s/%s.git\ncommit=%s\n" % (owner, slug, "a" * 40)).encode("utf-8")


_roots = []


def tearDownModule():
    for holder in _roots:
        holder.cleanup()


def make_root(owners="# who we are\n2hBuilds\n"):
    holder = tempfile.TemporaryDirectory()
    _roots.append(holder)
    root = holder.name
    with open(os.path.join(root, "owners.txt"), "w", encoding="utf-8") as f:
        f.write(owners)
    return root


def check(root, hub, now, env=None):
    """main() as the workflow runs it; returns (exit code, actions, GITHUB_OUTPUT text, stdout)."""
    out = os.path.join(root, "actions.json")
    gh_out = os.path.join(root, "gh-output.txt")
    if os.path.exists(gh_out):
        os.remove(gh_out)
    env = {"GITHUB_OUTPUT": gh_out} if env is None else env
    with contextlib.redirect_stdout(io.StringIO()) as printed, contextlib.redirect_stderr(io.StringIO()):
        code = check_health.main(["--actions-out", out], root, env, hub.fetch, hub.fetch_manifest, now,
                                 fetch_file=hub.fetch_file)
    with open(out, encoding="utf-8") as f:
        actions = json.load(f)
    output = ""
    if os.path.exists(gh_out):
        with open(gh_out, encoding="utf-8") as f:
            output = f.read()
    return code, actions, output, printed.getvalue()


def state(root):
    with open(os.path.join(root, "data", "health.json"), encoding="utf-8") as f:
        return json.load(f)


def log(root):
    path = os.path.join(root, "data", "health-log.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


def plugin(root, slug=SLUG):
    return next(p for p in state(root)["plugins"] if p["slug"] == slug)


class ManifestTest(unittest.TestCase):
    def test_a_good_manifest(self):
        display, jars = check_health.parse_manifest(manifest([entry(), entry("other", "x")], [SLUG]))
        self.assertEqual(sorted(display), [SLUG, "other"])
        self.assertEqual(jars, {SLUG})

    def test_a_short_read(self):
        with self.assertRaises(check_health.ManifestError):
            check_health.parse_manifest(b"\x00\x00")

    def test_a_length_beyond_the_file(self):
        with self.assertRaises(check_health.ManifestError):
            check_health.parse_manifest(struct.pack(">I", 5000) + b"x" * 100)

    def test_bad_json(self):
        with self.assertRaises(check_health.ManifestError):
            check_health.parse_manifest(struct.pack(">I", 2) + b"ss{not json")

    def test_display_not_a_list(self):
        body = struct.pack(">I", 1) + b"s" + json.dumps({"display": {}, "jars": []}).encode()
        with self.assertRaises(check_health.ManifestError):
            check_health.parse_manifest(body)

    def test_entries_without_an_internal_name_are_skipped(self):
        display, jars = check_health.parse_manifest(manifest([entry(), {"displayName": "x"}, "junk"], []))
        self.assertEqual(list(display), [SLUG])


class OwnerTest(unittest.TestCase):
    def test_case_and_spaces_match_and_comments_do_not(self):
        root = make_root("# 2hBuilds would match if this were not a comment\n  2HBUILDS  \n")
        hub = Hub(body=manifest([entry(author=" 2hbuilds "), entry("theirs", "someone")], [SLUG, "theirs"]))
        check(root, hub, T0)
        self.assertEqual([p["slug"] for p in state(root)["plugins"]], [SLUG])

    def test_a_comment_line_is_not_an_owner(self):
        root = make_root("# 2hBuilds\n")
        check(root, Hub(body=manifest([entry()], [SLUG])), T0)
        self.assertEqual(state(root)["plugins"], [])

    def test_plugins_txt_plays_no_part(self):
        root = make_root()
        with open(os.path.join(root, "plugins.txt"), "w", encoding="utf-8") as f:
            f.write("theirs\n")
        check(root, Hub(body=manifest([entry(), entry("theirs", "someone")], [SLUG])), T0)
        self.assertEqual([p["slug"] for p in state(root)["plugins"]], [SLUG])


class StatusTest(unittest.TestCase):
    def first(self, e, jars):
        root = make_root()
        code, actions, output, _ = check(root, Hub("1.13.0", manifest([e], jars)), T0)
        self.assertEqual(code, 0)
        return root, actions

    def test_ok_on_the_first_run_raises_nothing(self):
        root, actions = self.first(entry(), [SLUG])
        self.assertEqual(plugin(root)["status"], "ok")
        self.assertEqual(plugin(root)["since"], "2026-09-28T13:37:00Z")
        self.assertEqual(actions, [])

    def test_build_fail_at_without_a_jar_is_unavailable_at_once(self):
        root, actions = self.first(entry(buildFailAt=FAILED_AT), [])
        p = plugin(root)
        self.assertEqual(p["status"], "unavailable")
        self.assertEqual(p["reason"], "build failed against RuneLite 1.13.0 at 2026-09-28 14:21:51 UTC")
        self.assertEqual([a["action"] for a in actions], ["open"])

    def test_an_unavailable_reason_is_the_reason(self):
        root, actions = self.first(entry(unavailableReason="Very outdated", buildFailAt=FAILED_AT), [])
        self.assertEqual(plugin(root)["reason"], "Very outdated")
        self.assertEqual(len(actions), 1)

    def test_no_jar_and_no_reason_is_pending_and_raises_nothing(self):
        root, actions = self.first(entry(), [])
        self.assertEqual(plugin(root)["status"], "pending")
        self.assertEqual(actions, [])

    def test_a_warning_is_carried_and_never_alarms(self):
        root, actions = self.first(entry(warning="This plugin submits your IP address"), [SLUG])
        self.assertEqual(plugin(root)["warning"], "This plugin submits your IP address")
        self.assertEqual(plugin(root)["status"], "ok")
        self.assertEqual(actions, [])

    def test_a_jar_with_a_stale_reason_is_still_ok(self):
        root, actions = self.first(entry(buildFailAt=FAILED_AT), [SLUG])
        self.assertEqual(plugin(root)["status"], "ok")


class PendingTest(unittest.TestCase):
    def test_pending_then_ok_raises_nothing(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], []))
        check(root, hub, at(0))
        hub.body = manifest([entry()], [SLUG])
        code, actions, output, _ = check(root, hub, at(1))
        self.assertEqual(plugin(root)["status"], "ok")
        self.assertEqual(actions, [])
        self.assertIn("changed=true", output)

    def test_pending_then_still_no_jar_raises_the_alarm(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, at(0))
        hub.body = manifest([entry()], [])
        _, actions, _, _ = check(root, hub, at(1))
        self.assertEqual(plugin(root)["status"], "pending")
        self.assertEqual(actions, [])
        _, actions, _, _ = check(root, hub, at(2))
        p = plugin(root)
        self.assertEqual((p["status"], p["reason"]), ("unavailable", "no jar published, no reason given"))
        self.assertEqual([a["action"] for a in actions], ["open"])
        self.assertEqual(p["since"], "2026-09-28T15:37:00Z")

    def test_missing_takes_the_same_two_steps(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, at(0))
        hub.body = manifest([entry("someone-else", "x")], ["someone-else"])
        _, actions, _, _ = check(root, hub, at(1))
        self.assertEqual((plugin(root)["status"], actions), ("pending", []))
        _, actions, output, _ = check(root, hub, at(2))
        p = plugin(root)
        self.assertEqual(p["status"], "missing")
        self.assertEqual(p["name"], "2h Bank Portfolio Tracker")
        self.assertEqual(p["version"], "1.0.4")
        self.assertEqual([a["action"] for a in actions], ["open"])
        self.assertIn("summary=%s pending -> missing" % SLUG, output)
        hub.body = manifest([entry(version="1.0.5")], [SLUG])
        _, actions, _, _ = check(root, hub, at(3))
        self.assertEqual([a["action"] for a in actions], ["close"])

    def test_unavailable_then_missing_opens_no_second_issue(self):
        root = make_root()
        hub = Hub(body=manifest([entry(buildFailAt=FAILED_AT)], []))
        check(root, hub, at(0))
        hub.body = manifest([], [])
        _, actions, _, _ = check(root, hub, at(1))
        self.assertEqual((plugin(root)["status"], actions), ("missing", []))

    def test_a_plugin_whose_author_is_no_longer_ours_leaves_the_watch_list(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, at(0))
        hub.body = manifest([entry(author="someone")], [SLUG])
        _, actions, output, _ = check(root, hub, at(1))
        self.assertEqual(state(root)["plugins"], [])
        self.assertEqual(log(root)[-1][1:], [SLUG, "ok", "", "no longer watched"])
        self.assertIn("summary=%s left" % SLUG, output)


class ChangeTest(unittest.TestCase):
    def test_check_time_alone_is_not_a_change(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, at(0))
        rows = log(root)
        code, actions, output, printed = check(root, hub, at(1))
        self.assertEqual(output, "changed=false\nsummary=no change\n")
        self.assertIn("changed=false", printed)
        self.assertEqual(state(root)["checkedAt"], "2026-09-28T14:37:00Z")
        self.assertEqual(log(root), rows)

    def test_a_plugin_version_change_is_a_change_without_a_log_row(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, at(0))
        rows = log(root)
        hub.body = manifest([entry(version="1.0.5")], [SLUG])
        _, _, output, _ = check(root, hub, at(1))
        self.assertEqual(output, "changed=true\nsummary=%s 1.0.4 -> 1.0.5\n" % SLUG)
        self.assertEqual(log(root), rows)

    def test_a_client_version_change_is_logged(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, at(0))
        hub.version = "1.13.0"
        _, _, output, _ = check(root, hub, at(1))
        self.assertEqual(log(root)[-1], ["2026-09-28T14:37:00Z", "runelite", "1.12.39", "1.13.0", ""])
        self.assertIn("summary=runelite 1.12.39 -> 1.13.0", output)
        self.assertEqual(state(root)["versionSince"], "2026-09-28T14:37:00Z")
        check(root, hub, at(2))
        self.assertEqual(state(root)["versionSince"], "2026-09-28T14:37:00Z")

    def test_the_first_run_logs_the_baseline(self):
        root = make_root()
        _, actions, output, _ = check(root, Hub(body=manifest([entry()], [SLUG])), T0)
        self.assertEqual(log(root), [
            ["time", "plugin", "from", "to", "detail"],
            ["2026-09-28T13:37:00Z", "runelite", "", "1.12.39", ""],
            ["2026-09-28T13:37:00Z", SLUG, "", "ok", "version 1.0.4"],
        ])
        self.assertEqual(output, "changed=true\nsummary=runelite 1.12.39; %s joined (ok)\n" % SLUG)
        self.assertEqual(state(root), {
            "checkedAt": "2026-09-28T13:37:00Z", "version": "1.12.39", "versionSince": "2026-09-28T13:37:00Z",
            "plugins": [{"slug": SLUG, "name": "2h Bank Portfolio Tracker", "author": "2hBuilds", "version": "1.0.4",
                         "status": "ok", "reason": None, "since": "2026-09-28T13:37:00Z", "warning": None}],
        })

    def test_the_state_file_is_sorted_indented_and_ends_with_a_newline(self):
        root = make_root()
        check(root, Hub(body=manifest([entry("zeta"), entry("alpha"), entry()], ["zeta", "alpha", SLUG])), T0)
        with open(os.path.join(root, "data", "health.json"), encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.endswith("}\n"))
        self.assertTrue(text.startswith('{\n "checkedAt"'))
        self.assertEqual([p["slug"] for p in state(root)["plugins"]], ["alpha", SLUG, "zeta"])

    def test_without_github_output_it_only_prints(self):
        root = make_root()
        code, _, output, printed = check(root, Hub(body=manifest([entry()], [SLUG])), T0, env={})
        self.assertEqual((code, output), (0, ""))
        self.assertIn("changed=true\nsummary=", printed)


class ActionTest(unittest.TestCase):
    def test_the_open_action(self):
        root = make_root()
        _, actions, _, _ = check(root, Hub("1.13.0", manifest([entry(buildFailAt=FAILED_AT)], [])), T0)
        (a,) = actions
        self.assertEqual(a["title"], "[%s] 2h Bank Portfolio Tracker is unavailable on the Plugin Hub" % SLUG)
        body = a["body"]
        for part in ("**unavailable**", "RuneLite client: 1.13.0", "Build failed at: 2026-09-28 14:21:51 UTC",
                     "-PruneLiteVersion=1.13.0", "`commit=` in `plugins/%s`" % SLUG, "runelite/plugin-hub",
                     "https://runelite.net/plugin-hub/show/%s" % SLUG,
                     "https://github.com/runelite/plugin-hub/blob/master/plugins/%s" % SLUG):
            self.assertIn(part, body)
        self.assertEqual(body.rstrip("\n").splitlines()[-1], "@2hBuilds")

    def test_the_mention_is_the_owners_txt_spelling_not_the_manifests(self):
        root = make_root("2hBuilds\nsomeone-else\n")
        _, actions, _, _ = check(root, Hub(body=manifest([entry(author="2HBUILDS", buildFailAt=1)], [])), T0)
        self.assertEqual(actions[0]["body"].rstrip("\n").splitlines()[-1], "@2hBuilds")

    def test_the_close_action(self):
        root = make_root()
        hub = Hub(body=manifest([entry(buildFailAt=FAILED_AT)], []))
        check(root, hub, at(0))
        hub.body = manifest([entry(version="1.0.5")], [SLUG])
        _, actions, _, _ = check(root, hub, at(1))
        (a,) = actions
        self.assertEqual((a["action"], a["slug"]), ("close", SLUG))
        self.assertIn("version 1.0.5", a["comment"])

    def test_pending_opens_nothing(self):
        root = make_root()
        _, actions, _, _ = check(root, Hub(body=manifest([entry()], [])), T0)
        self.assertEqual(actions, [])


class FakeGh:
    """gh as far as apply_actions uses it: labels are ignored, issues kept in a list."""

    def __init__(self, issues=None):
        self.issues = issues or []
        self.calls = []
        self.bodies = []

    def __call__(self, args):
        self.assertList(args)
        self.calls.append(args)
        verb = args[1:3]
        if verb == ["issue", "list"]:
            return json.dumps([{"number": i["number"], "title": i["title"]} for i in self.issues if i["open"]])
        if verb == ["issue", "create"]:
            with open(args[args.index("--body-file") + 1], encoding="utf-8") as f:
                self.bodies.append(f.read())
            number = len(self.issues) + 1
            self.issues.append({"number": number, "title": args[args.index("--title") + 1], "open": True})
            return "https://github.com/2hBuilds/hub-installs/issues/%d\n" % number
        if verb == ["issue", "comment"]:
            with open(args[args.index("--body-file") + 1], encoding="utf-8") as f:
                self.bodies.append(f.read())
        if verb == ["issue", "close"]:
            next(i for i in self.issues if str(i["number"]) == args[3])["open"] = False
        return ""

    @staticmethod
    def assertList(args):
        assert isinstance(args, list) and all(isinstance(a, str) for a in args), args


class ApplyTest(unittest.TestCase):
    OPEN = {"action": "open", "slug": SLUG, "title": "[%s] X is unavailable on the Plugin Hub" % SLUG, "body": "b\n"}
    CLOSE = {"action": "close", "slug": SLUG, "comment": "back"}

    def apply(self, actions, gh):
        with contextlib.redirect_stdout(io.StringIO()):
            check_health.apply_actions(actions, gh)

    def test_opening_twice_opens_one_issue(self):
        gh = FakeGh()
        self.apply([self.OPEN], gh)
        self.apply([self.OPEN], gh)
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual(gh.bodies, ["b\n"])
        self.assertIn(["gh", "label", "create", "hub-health", "--force"], [c[:5] for c in gh.calls])

    def test_an_issue_for_another_slug_does_not_count(self):
        gh = FakeGh([{"number": 7, "title": "[%s-two] other" % SLUG, "open": True}])
        self.apply([self.OPEN], gh)
        self.assertEqual(len(gh.issues), 2)

    def test_closing_comments_and_closes_once(self):
        gh = FakeGh()
        self.apply([self.OPEN], gh)
        self.apply([self.CLOSE], gh)
        self.apply([self.CLOSE], gh)
        self.assertFalse(gh.issues[0]["open"])
        self.assertEqual(gh.bodies, ["b\n", "back"])
        self.assertEqual(len([c for c in gh.calls if c[1:3] == ["issue", "close"]]), 1)

    def test_no_actions_runs_no_gh(self):
        gh = FakeGh()
        self.apply([], gh)
        self.assertEqual(gh.calls, [])

    def test_the_apply_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([self.OPEN], f)
            gh = FakeGh()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check_health.main(["--apply", path], runner=gh), 0)
                self.assertEqual(check_health.main(["--apply", path], runner=gh), 0)
            self.assertEqual(len(gh.issues), 1)

    def test_a_gh_failure_exits_non_zero(self):
        def broken(args):
            raise fetch_installs.FetchError("gh issue list failed: no token")

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([self.OPEN], f)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(check_health.main(["--apply", path], runner=broken), 1)
            self.assertIn("no token", err.getvalue())


class HostileTest(unittest.TestCase):
    NAME = "@everyone [click](https://evil.example/x) ![i](http://e.x/p.png) <img src=x> www.evil.example\nline2"

    def test_manifest_text_comes_out_harmless(self):
        root = make_root()
        e = entry(displayName=self.NAME + "x" * 5000, version="1.0.4 @admin",
                  unavailableReason="see [here](https://evil.example) @mod\r\n" + "y" * 5000,
                  warning="<script>alert(1)</script> @x")
        _, actions, _, _ = check(root, Hub(body=manifest([e], [])), T0)
        (a,) = actions
        title, body = a["title"], a["body"]
        self.assertNotIn("@", title)
        self.assertNotIn("\n", title)
        self.assertNotIn("://", title)
        # capped at 80 visible characters; the zero-width spaces that break autolinks come after
        self.assertLessEqual(len(title.replace("​", "")), len("[%s]  is unavailable on the Plugin Hub" % SLUG) + 80)
        # the only @ in the body is the owner's mention on its last line
        self.assertEqual(body.count("@"), 1)
        self.assertTrue(body.rstrip("\n").endswith("\n@2hBuilds"))
        for bad in ("://evil", "www.evil", "\r"):
            self.assertNotIn(bad, body)
        # the template itself has no < [ ] !, so any in the body came from the manifest and must be escaped
        self.assertIsNone(re.search(r"(?<!\\)[<>\[\]!]", body))
        self.assertIn("\\<script\\>", body)
        reason_line = next(line for line in body.splitlines() if line.startswith("- Reason: "))
        self.assertLessEqual(len(reason_line), len("- Reason: ") + 2 * 300)
        self.assertIn("y...", reason_line)
        self.assertEqual(check_health.harmless_title("one\ntwo\r\nthree"), "one two three")

    def test_harmless_escapes_what_markdown_would_build_on(self):
        self.assertEqual(check_health.harmless("[a](b) `c` <d> #1", 300), "\\[a\\]\\(b\\) \\`c\\` \\<d\\> \\#1")
        self.assertEqual(check_health.plain("a\x00b‮c\td", 300), "abc d")
        self.assertEqual(len(check_health.plain("z" * 5000, 300)), 300)

    def test_an_invalid_slug_is_skipped_with_a_note(self):
        root = make_root()
        _, actions, _, printed = check(root, Hub(body=manifest([entry("Bad Slug;rm", buildFailAt=1), entry()], [SLUG])), T0)
        self.assertEqual([p["slug"] for p in state(root)["plugins"]], [SLUG])
        self.assertEqual(actions, [])
        self.assertIn("note: skipping watched plugin with an invalid slug", printed)


class FailureTest(unittest.TestCase):
    def test_a_404_changes_nothing_and_exits_zero(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, at(0))
        before = (state(root), log(root))
        hub.version, hub.body = "1.13.0", check_health.ManifestMissing("404")
        code, actions, output, printed = check(root, hub, at(1))
        self.assertEqual((code, actions, output), (0, [], "changed=false\nsummary=no change\n"))
        self.assertEqual((state(root), log(root)), before)
        self.assertIn("no manifest yet", printed)
        self.assertNotIn("::warning::", printed)

    def test_another_fetch_failure_warns_and_exits_zero(self):
        root = make_root()
        hub = Hub(body=fetch_installs.FetchError("could not fetch: timed out"))
        code, actions, output, printed = check(root, hub, T0)
        self.assertEqual((code, actions), (0, []))
        self.assertIn("::warning::hub health not checked: could not fetch: timed out", printed)
        self.assertFalse(os.path.exists(os.path.join(root, "data")))

    def test_a_manifest_that_cannot_be_read_exits_one_and_changes_nothing(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, at(0))
        before = (state(root), log(root))
        for bad in (b"", b"\x00\x00\x02\x00short", struct.pack(">I", 1) + b"s[]", struct.pack(">I", 1) + b"s{oops"):
            hub.body = bad
            code, actions, output, _ = check(root, hub, at(1))
            self.assertEqual((code, actions), (1, []))
            self.assertIn("changed=false", output)
        self.assertEqual((state(root), log(root)), before)

    def test_a_404_is_told_apart_from_other_http_errors(self):
        import urllib.error
        import urllib.request

        original = urllib.request.urlopen

        def answer(code):
            def urlopen(request, timeout):
                raise urllib.error.HTTPError(request.full_url, code, "x", {}, None)
            return urlopen

        try:
            urllib.request.urlopen = answer(404)
            with self.assertRaises(check_health.ManifestMissing):
                check_health.fetch_bytes("https://example.invalid/m")
            urllib.request.urlopen = answer(503)
            with self.assertRaises(fetch_installs.FetchError) as caught:
                check_health.fetch_bytes("https://example.invalid/m")
            self.assertNotIsInstance(caught.exception, check_health.ManifestMissing)
        finally:
            urllib.request.urlopen = original


class GraceTest(unittest.TestCase):
    """Pending waits PENDING_GRACE, not one run: the daily 03:15 run and manual runs come minutes apart."""

    def at_clock(self, hour, minute):
        return datetime.datetime(2026, 9, 28, hour, minute, tzinfo=datetime.timezone.utc)

    def test_a_run_soon_after_the_first_sighting_keeps_it_pending(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, self.at_clock(1, 37))
        hub.body = manifest([entry()], [])
        check(root, hub, self.at_clock(2, 37))
        for now in (self.at_clock(2, 41), self.at_clock(3, 15)):  # a manual run, then the daily run
            _, actions, output, _ = check(root, hub, now)
            self.assertEqual((plugin(root)["status"], actions), ("pending", []))
            self.assertIn("changed=false", output)
        self.assertEqual(plugin(root)["since"], "2026-09-28T02:37:00Z")
        _, actions, _, _ = check(root, hub, self.at_clock(3, 37))
        self.assertEqual(plugin(root)["status"], "unavailable")
        self.assertEqual([a["action"] for a in actions], ["open"])

    def test_missing_waits_the_same_grace(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, self.at_clock(1, 37))
        hub.body = manifest([], [])
        check(root, hub, self.at_clock(2, 37))
        _, actions, _, _ = check(root, hub, self.at_clock(3, 15))
        self.assertEqual((plugin(root)["status"], actions), ("pending", []))
        _, actions, _, _ = check(root, hub, self.at_clock(3, 37))
        self.assertEqual((plugin(root)["status"], [a["action"] for a in actions]), ("missing", ["open"]))

    def test_a_pending_since_that_cannot_be_read_does_not_hold_the_alarm_back(self):
        old = {"status": "pending", "since": "yesterday"}
        self.assertTrue(check_health.has_waited(old, T0))
        self.assertTrue(check_health.has_waited({"status": "pending", "since": "2030-01-01T00:00:00Z"}, T0))
        self.assertFalse(check_health.has_waited({"status": "pending", "since": "2026-09-28T13:00:00Z"}, T0))
        self.assertTrue(check_health.has_waited({"status": "pending", "since": "2026-09-28T12:52:00Z"}, T0))
        self.assertFalse(check_health.has_waited({"status": "ok", "since": "2020-01-01T00:00:00Z"}, T0))
        self.assertFalse(check_health.has_waited(None, T0))


class TrustTest(unittest.TestCase):
    """The manifest's author field is free text; a plugin joins only when its plugin-hub entry is ours."""

    def test_a_strangers_plugin_naming_us_as_author_is_not_watched(self):
        root = make_root()
        hub = Hub("1.13.0", manifest([entry(), entry("spoof", " 2HBUILDS ", displayName="Not yours",
                                                     buildFailAt=FAILED_AT)], [SLUG]))
        hub.repos["spoof"] = "stranger"
        _, actions, output, printed = check(root, hub, T0)
        self.assertEqual([p["slug"] for p in state(root)["plugins"]], [SLUG])
        self.assertEqual(actions, [])
        self.assertNotIn("spoof", output)
        self.assertIn("::warning::spoof names an owner as its author, but its plugin-hub entry points at"
                      " github.com/stranger; not watched", printed)

    def test_an_entry_that_cannot_be_read_is_tried_again_next_run(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        hub.repos[SLUG] = fetch_installs.FetchError("could not fetch: timed out")
        _, _, output, printed = check(root, hub, at(0))
        self.assertEqual(state(root)["plugins"], [])
        self.assertIn("::warning::%s not watched yet" % SLUG, printed)
        del hub.repos[SLUG]
        _, _, output, _ = check(root, hub, at(1))
        self.assertEqual(plugin(root)["status"], "ok")
        self.assertIn("summary=%s joined (ok)" % SLUG, output)

    def test_a_watched_plugin_is_not_looked_up_again(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, at(0))
        hub.seen = []
        hub.repos[SLUG] = "stranger"
        check(root, hub, at(1))
        self.assertEqual(plugin(root)["status"], "ok")
        self.assertFalse([u for u in hub.seen if "raw.githubusercontent.com" in u])

    def test_the_repository_line_is_read_strictly(self):
        owner = check_health.repository_owner
        self.assertEqual(owner(b"repository=https://github.com/2hBuilds/bank-portfolio-tracker.git\ncommit=ab\n"),
                         "2hBuilds")
        self.assertEqual(owner(b"commit=ab\nrepository = https://github.com/Some-One/x\n"), "Some-One")
        self.assertIsNone(owner(b"repository=https://gitlab.com/2hBuilds/x.git\n"))
        self.assertIsNone(owner(b"repository=https://github.com/2hBuilds\n"))
        self.assertIsNone(owner(b"repository=https://github.com.evil/2hBuilds/x\n"))
        self.assertIsNone(owner(b"commit=ab\n"))


class OwnersTxtTest(unittest.TestCase):
    def test_a_leading_at_sign_and_a_co_author_still_match(self):
        root = make_root("@2hBuilds\n")
        hub = Hub(body=manifest([entry(author="2hBuilds, Friend", buildFailAt=FAILED_AT)], []))
        _, actions, _, _ = check(root, hub, T0)
        self.assertEqual(plugin(root)["status"], "unavailable")
        self.assertEqual(actions[0]["body"].rstrip("\n").splitlines()[-1], "@2hBuilds")

    def test_an_owner_with_no_plugin_is_warned_about(self):
        root = make_root("2hBuilds\n2h Builds\n")
        _, _, _, printed = check(root, Hub(body=manifest([entry()], [SLUG])), T0)
        self.assertIn("::warning::no plugin in the manifest has the author '2h Builds' from owners.txt", printed)
        self.assertNotIn("author '2hBuilds'", printed)

    def test_a_plugin_that_leaves_while_still_listed_is_warned_about(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, at(0))
        hub.body = manifest([entry(author="someone")], [SLUG])
        _, _, _, printed = check(root, hub, at(1))
        self.assertIn("::warning::%s is still on the Plugin Hub but its author 'someone' is not in owners.txt"
                      % SLUG, printed)


class FileTest(unittest.TestCase):
    def test_a_log_whose_last_line_lost_its_newline_gets_one_before_the_new_row(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, at(0))
        path = os.path.join(root, "data", "health-log.csv")
        with open(path, "rb") as f:
            text = f.read()
        with open(path, "wb") as f:
            f.write(text.rstrip(b"\n"))
        hub.version = "1.13.0"
        check(root, hub, at(1))
        self.assertEqual(log(root)[-2:], [["2026-09-28T13:37:00Z", SLUG, "", "ok", "version 1.0.4"],
                                          ["2026-09-28T14:37:00Z", "runelite", "1.12.39", "1.13.0", ""]])

    def test_an_empty_log_gets_its_header(self):
        root = make_root()
        os.makedirs(os.path.join(root, "data"))
        open(os.path.join(root, "data", "health-log.csv"), "w").close()
        check(root, Hub(body=manifest([entry()], [SLUG])), T0)
        self.assertEqual(log(root)[0], ["time", "plugin", "from", "to", "detail"])
        self.assertEqual(len(log(root)), 3)

    def test_a_state_file_with_a_byte_order_mark_is_read(self):
        root = make_root()
        hub = Hub(body=manifest([entry()], [SLUG]))
        check(root, hub, at(0))
        path = os.path.join(root, "data", "health.json")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(text)
        code, _, output, _ = check(root, hub, at(1))
        self.assertEqual((code, output), (0, "changed=false\nsummary=no change\n"))

    def test_a_slug_ending_in_a_newline_is_skipped(self):
        root = make_root()
        _, _, output, printed = check(root, Hub(body=manifest([entry(), entry("trail\n")], [SLUG, "trail\n"])), T0)
        self.assertEqual([p["slug"] for p in state(root)["plugins"]], [SLUG])
        self.assertEqual(output.count("\n"), 2)
        self.assertTrue(all(line.startswith(("changed=", "summary=")) for line in output.splitlines()))
        self.assertIn("note: skipping watched plugin with an invalid slug", printed)
        self.assertIsNone(check_health.SLUG.fullmatch("evil\n"))
        self.assertIsNone(check_health.VERSION.fullmatch("1.13.0\n"))
        self.assertIsNone(check_health.GITHUB_LOGIN.fullmatch("2hBuilds\n"))

    def test_a_gh_that_hangs_is_a_failure(self):
        import subprocess
        original = subprocess.run

        def hang(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

        try:
            subprocess.run = hang
            with self.assertRaises(fetch_installs.FetchError) as caught:
                check_health.gh(["gh", "issue", "list"])
            self.assertIn("gave no answer in 60 seconds", str(caught.exception))
        finally:
            subprocess.run = original


class WorkflowTest(unittest.TestCase):
    """What the review found in the workflows, pinned in their text."""

    def read(self, name):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows", name)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_both_check_out_the_branch_tip_when_they_start(self):
        # without ref, a run queued behind the other checks out the sha of its creation and its push conflicts
        for name in ("daily.yml", "health.yml"):
            text = self.read(name)
            self.assertIn("uses: actions/checkout@v7\n        with:\n          ref: ${{ github.ref }}", text, name)
            self.assertIn("group: daily-installs", text, name)

    def test_nothing_can_hold_the_shared_group_for_hours(self):
        self.assertRegex(self.read("health.yml"), r"\n    timeout-minutes: \d+\n")
        daily = self.read("daily.yml")
        step = daily[daily.index("- name: Check the Plugin Hub"):daily.index("- name: Commit if the data changed")]
        self.assertIn("timeout-minutes:", step)
        self.assertIn("continue-on-error: true", step)

    def test_the_summary_reaches_the_shell_through_the_environment(self):
        lines = [line.strip() for line in self.read("health.yml").splitlines() if "outputs.summary" in line]
        self.assertEqual(lines, ["SUMMARY: ${{ steps.health.outputs.summary }}"])


EMAIL = re.compile(r"[^\s@<>()\[\]]+@[^\s@<>()\[\]]+\.[A-Za-z]{2,}")


def snapshot(root):
    """{relative path: bytes} of every file under root."""
    files = {}
    for folder, _, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            with open(path, "rb") as f:
                files[os.path.relpath(path, root)] = f.read()
    return files


class DrillGh(FakeGh):
    """FakeGh that also keeps the body files it was handed, to see they were files and are gone."""

    def __init__(self, issues=None, fail_on=None):
        super().__init__(issues)
        self.files = []
        self.fail_on = fail_on

    def __call__(self, args):
        if "--body-file" in args:
            self.files.append(args[args.index("--body-file") + 1])
        if self.fail_on and args[1:3] == self.fail_on:
            self.calls.append(args)
            raise fetch_installs.FetchError("gh %s failed: HTTP 403" % " ".join(self.fail_on))
        return super().__call__(args)


class DrillTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        os.makedirs(os.path.join(self.root, "data"))
        with open(os.path.join(self.root, "data", "health.json"), "w", encoding="utf-8") as f:
            f.write('{"checkedAt": "2026-09-29T00:37:00Z", "plugins": []}\n')
        with open(os.path.join(self.root, "data", "health-log.csv"), "w", encoding="utf-8") as f:
            f.write("time,plugin,from,to,detail\n")
        self.fetched = []

    def no_fetch(self, url):
        self.fetched.append(url)
        raise AssertionError("a drill fetched %s" % url)

    def drill(self, gh, env=None):
        """main(["--drill"]) as the workflow runs it; returns (exit code, stdout, stderr)."""
        env = {"GITHUB_OUTPUT": os.path.join(self.root, "gh-output.txt")} if env is None else env
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = check_health.main(["--drill"], self.root, env, self.no_fetch, self.no_fetch, T0, gh,
                                     fetch_file=self.no_fetch)
        return code, out.getvalue(), err.getvalue()

    def test_one_drill_labels_lists_opens_comments_and_closes(self):
        gh = DrillGh()
        code, printed, _ = self.drill(gh)
        self.assertEqual(code, 0)
        self.assertEqual([c[1:3] for c in gh.calls], [["label", "create"], ["issue", "list"], ["issue", "create"],
                                                     ["issue", "comment"], ["issue", "close"]])
        self.assertEqual(gh.calls[0][:5], ["gh", "label", "create", "hub-health", "--force"])
        create = gh.calls[2]
        self.assertEqual(create[create.index("--title") + 1], "[drill] Test alert - nothing is wrong")
        self.assertEqual(create[create.index("--label") + 1], "hub-health")
        self.assertEqual(gh.calls[3][3], "1")
        self.assertEqual(gh.calls[4], ["gh", "issue", "close", "1"])
        self.assertEqual(gh.issues, [{"number": 1, "title": "[drill] Test alert - nothing is wrong", "open": False}])
        self.assertEqual(gh.bodies[1], "Drill complete: the alert path works. Closing.")
        self.assertIn("drill: opened https://github.com/2hBuilds/hub-installs/issues/1", printed)
        self.assertIn("drill: commented on and closed #1", printed)

    def test_the_body_says_test_and_mentions_the_owners(self):
        with open(os.path.join(self.root, "owners.txt"), "w", encoding="utf-8") as f:
            f.write("# owners\n2hBuilds\n2h Builds\n@friend\n")
        gh = DrillGh()
        self.drill(gh)
        body = gh.bodies[0]
        lines = body.rstrip("\n").splitlines()
        self.assertIn("TEST", lines[0])
        self.assertIn("no plugin is affected", lines[0])
        self.assertIn("2026-09-28 13:37:00 UTC", body)
        self.assertIn("`[<slug>] <name> is unavailable on the Plugin Hub`", body)
        self.assertIn("closes itself", body)
        # the name with a space is not a login, exactly as a real alert leaves it out
        self.assertEqual(lines[-1], "@2hBuilds @friend")
        self.assertEqual(body.count("@"), 2)

    def test_nothing_shaped_like_an_email_address_is_written_or_printed(self):
        gh = DrillGh()
        _, printed, err = self.drill(gh)
        self.assertTrue(EMAIL.search("mention a.b+c@example.co here"))  # the pattern itself works
        for text in gh.bodies + [printed, err] + [a for c in gh.calls for a in c]:
            self.assertIsNone(EMAIL.search(text), text)

    def test_the_body_goes_through_a_file_and_never_a_shell(self):
        gh = DrillGh()
        self.drill(gh)
        for call in gh.calls:
            FakeGh.assertList(call)
            self.assertNotIn("--body", call)
            self.assertFalse(any("TEST" in a or "\n" in a for a in call), call)
        self.assertEqual(len(gh.files), 2)
        self.assertFalse(any(os.path.exists(path) for path in gh.files))

    def test_a_half_finished_earlier_drill_is_closed_not_duplicated(self):
        gh = DrillGh([{"number": 5, "title": "[drill] Test alert - nothing is wrong", "open": True}])
        code, printed, _ = self.drill(gh)
        self.assertEqual(code, 0)
        self.assertEqual([c[1:3] for c in gh.calls], [["label", "create"], ["issue", "list"],
                                                     ["issue", "comment"], ["issue", "close"]])
        self.assertEqual(len(gh.issues), 1)
        self.assertFalse(gh.issues[0]["open"])
        self.assertIn("#5 from an earlier drill is still open", printed)
        # that run mentioned nobody, so it says so and asks for another drill
        self.assertIn("::warning::this drill only tidied up an earlier one", printed)
        self.assertIn("no alert e-mail comes from it; run the drill once more", printed)

    def test_a_fresh_drill_does_not_ask_to_be_run_again(self):
        _, printed, _ = self.drill(DrillGh())
        self.assertNotIn("::warning::", printed)
        self.assertNotIn("run the drill once more", printed)

    def test_owners_with_no_github_login_give_a_warning_and_a_body_with_no_mention(self):
        with open(os.path.join(self.root, "owners.txt"), "w", encoding="utf-8") as f:
            f.write("# owners\n2h Builds\nme@example.com\n")
        gh = DrillGh()
        code, printed, err = self.drill(gh)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("::warning::no name in owners.txt looks like a GitHub login; the drill mentions nobody",
                      printed)
        # the drill still runs the whole path; only the mention is missing
        self.assertEqual([c[1:3] for c in gh.calls], [["label", "create"], ["issue", "list"], ["issue", "create"],
                                                     ["issue", "comment"], ["issue", "close"]])
        body = gh.bodies[0]
        self.assertNotIn("@", body)
        self.assertIsNone(EMAIL.search(body), body)
        self.assertNotIn("example.com", body + printed)
        self.assertTrue(body.endswith("\n\n\n"), repr(body[-40:]))

    def test_a_close_that_fails_is_finished_by_the_next_drill(self):
        gh = DrillGh(fail_on=["issue", "close"])
        self.assertEqual(self.drill(gh)[0], 1)
        self.assertTrue(gh.issues[0]["open"])
        gh.fail_on = None
        self.assertEqual(self.drill(gh)[0], 0)
        self.assertEqual([(i["number"], i["open"]) for i in gh.issues], [(1, False)])

    def test_a_gh_failure_exits_one_with_one_error_line(self):
        for verb in (["label", "create"], ["issue", "list"], ["issue", "create"], ["issue", "comment"]):
            code, _, err = self.drill(DrillGh(fail_on=verb))
            self.assertEqual(code, 1, verb)
            self.assertEqual(err.count("\n"), 1, err)
            self.assertTrue(err.startswith("error: gh %s failed" % " ".join(verb)), err)

    def test_an_issue_address_gh_did_not_print_is_a_failure(self):
        code, _, err = self.drill(lambda args: "" if args[1:3] != ["issue", "list"] else "[]")
        self.assertEqual(code, 1)
        self.assertIn("printed no issue address", err)

    def test_the_data_folder_is_untouched_and_nothing_is_fetched(self):
        import urllib.request
        before = snapshot(self.root)
        original = urllib.request.urlopen

        def urlopen(*args, **kwargs):
            raise AssertionError("a drill opened a URL")

        try:
            urllib.request.urlopen = urlopen
            code, printed, _ = self.drill(DrillGh())
        finally:
            urllib.request.urlopen = original
        self.assertEqual(code, 0)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(self.fetched, [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "gh-output.txt")))
        self.assertNotIn("changed=", printed)
        self.assertNotIn("summary=", printed)

    def test_the_drill_runs_alone(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            check_health.main(["--drill", "--apply", "a.json"], self.root, {}, runner=DrillGh())


class DrillSlugTest(unittest.TestCase):
    """A plugin whose slug is "drill" is conceivable; its alert and a drill must never be taken for each other."""

    PLUGIN_TITLE = "[drill] Drill Helper is unavailable on the Plugin Hub"

    def test_the_titles_are_told_apart(self):
        self.assertTrue(check_health.is_drill(check_health.DRILL_TITLE))
        self.assertFalse(check_health.is_drill(self.PLUGIN_TITLE))
        self.assertFalse(check_health.is_drill("[%s] X is unavailable on the Plugin Hub" % SLUG))
        # the real alert for such a plugin, built the real way, is a plugin's issue
        root = make_root()
        _, actions, _, _ = check(root, Hub(body=manifest([entry("drill", displayName="Drill Helper",
                                                               buildFailAt=FAILED_AT)], [])), T0)
        self.assertEqual(actions[0]["title"], self.PLUGIN_TITLE)
        self.assertFalse(check_health.is_drill(actions[0]["title"]))
        # even a display name that reads like the drill's title
        self.assertFalse(check_health.is_drill("[drill] Test alert - nothing is wrong is unavailable on the Plugin Hub"))

    def test_a_drill_leaves_the_drill_plugins_issue_alone(self):
        gh = DrillGh([{"number": 1, "title": self.PLUGIN_TITLE, "open": True}])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check_health.main(["--drill"], make_root(), {}, runner=gh, now=T0), 0)
        self.assertEqual([(i["number"], i["open"]) for i in gh.issues], [(1, True), (2, False)])
        self.assertNotIn(["gh", "issue", "close", "1"], gh.calls)

    def test_only_the_exact_drill_title_is_a_drill(self):
        # the drill plugin's alert retitled by hand, and an issue someone opened themselves
        for title in ("[drill] Drill Helper is down", "[drill] other", "[drill] Test alert - nothing is wrong!"):
            self.assertFalse(check_health.is_drill(title), title)
            gh = DrillGh([{"number": 1, "title": title, "open": True}])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check_health.main(["--drill"], make_root(), {}, runner=gh, now=T0), 0)
            self.assertEqual([(i["number"], i["open"]) for i in gh.issues], [(1, True), (2, False)], title)
            # and the real alarm still counts it as the drill plugin's issue
            gh = FakeGh([{"number": 1, "title": title, "open": True}])
            with contextlib.redirect_stdout(io.StringIO()):
                check_health.apply_actions([{"action": "open", "slug": "drill", "title": self.PLUGIN_TITLE,
                                             "body": "b\n"}], gh)
                check_health.apply_actions([{"action": "close", "slug": "drill", "comment": "back"}], gh)
            self.assertEqual([(i["number"], i["open"]) for i in gh.issues], [(1, False)], title)

    def test_an_open_drill_issue_does_not_count_as_the_drill_plugins_alert(self):
        gh = FakeGh([{"number": 1, "title": check_health.DRILL_TITLE, "open": True}])
        opened = {"action": "open", "slug": "drill", "title": self.PLUGIN_TITLE, "body": "b\n"}
        with contextlib.redirect_stdout(io.StringIO()):
            check_health.apply_actions([opened], gh)
        self.assertEqual([i["title"] for i in gh.issues], [check_health.DRILL_TITLE, self.PLUGIN_TITLE])
        with contextlib.redirect_stdout(io.StringIO()):
            check_health.apply_actions([{"action": "close", "slug": "drill", "comment": "back"}], gh)
        self.assertEqual([(i["number"], i["open"]) for i in gh.issues], [(1, True), (2, False)])


class DrillWorkflowTest(unittest.TestCase):
    def read(self, name):
        return WorkflowTest.read(self, name)

    def test_the_input(self):
        self.assertIn("  workflow_dispatch:\n    inputs:\n      drill:\n"
                      '        description: "Send a test alert: opens and closes one test issue"\n'
                      "        type: boolean\n        default: false\n", self.read("health.yml"))

    def test_the_fire_drill_step_is_last_and_runs_only_by_hand_with_the_box_ticked(self):
        text = self.read("health.yml")
        step = text[text.index("      - name: Fire drill"):]
        self.assertGreater(text.index("      - name: Fire drill"), text.index("- name: Commit if the health changed"))
        self.assertNotRegex(step[1:], r"\n      - ", "the drill is the last step")
        self.assertEqual(step.splitlines()[1].strip(),
                         "if: success() && github.event_name == 'workflow_dispatch' && inputs.drill == true")
        self.assertIn("        env:\n          GH_TOKEN: ${{ github.token }}\n"
                      "          GH_REPO: ${{ github.repository }}\n"
                      "        run: python3 check_health.py --drill\n", step)
        self.assertNotIn("continue-on-error", step)
        # the input reaches only the if:, never a shell
        self.assertEqual([line.strip() for line in text.splitlines()
                          if "inputs." in line and not line.strip().startswith("#")],
                         ["if: success() && github.event_name == 'workflow_dispatch' && inputs.drill == true"])
        self.assertNotIn("${{ inputs", text)
        self.assertNotIn("github.event.inputs", text)

    def test_daily_has_no_drill(self):
        self.assertNotIn("drill", self.read("daily.yml"))


class IncidentTest(unittest.TestCase):
    """The 2026-09-28 incident: the Hub moves to 1.13.0, the build fails, the jar comes back."""

    def test_replay(self):
        root = make_root()
        hub = Hub("1.12.39", manifest([entry(version="1.0.4")], [SLUG]))

        code, actions, output, _ = check(root, hub, datetime.datetime(2026, 9, 28, 13, 37, tzinfo=datetime.timezone.utc))
        self.assertEqual((code, actions, plugin(root)["status"]), (0, [], "ok"))
        rows = len(log(root))

        hub.version = "1.13.0"
        hub.body = manifest([entry(version="1.0.4", buildFailAt=FAILED_AT)], [])
        code, actions, output, _ = check(root, hub, datetime.datetime(2026, 9, 28, 14, 37, tzinfo=datetime.timezone.utc))
        new = log(root)[rows:]
        self.assertEqual(new, [
            ["2026-09-28T14:37:00Z", "runelite", "1.12.39", "1.13.0", ""],
            ["2026-09-28T14:37:00Z", SLUG, "ok", "unavailable",
             "build failed against RuneLite 1.13.0 at 2026-09-28 14:21:51 UTC"],
        ])
        self.assertIn("changed=true", output)
        self.assertEqual([a["action"] for a in actions], ["open"])
        rows = len(log(root))

        code, actions, output, _ = check(root, hub, datetime.datetime(2026, 9, 28, 15, 37, tzinfo=datetime.timezone.utc))
        self.assertEqual((actions, len(log(root))), ([], rows))
        self.assertIn("changed=false", output)
        self.assertEqual(plugin(root)["since"], "2026-09-28T14:37:00Z")

        hub.body = manifest([entry(version="1.0.5")], [SLUG])
        code, actions, output, _ = check(root, hub, datetime.datetime(2026, 9, 29, 1, 37, tzinfo=datetime.timezone.utc))
        self.assertEqual(log(root)[rows:], [["2026-09-29T01:37:00Z", SLUG, "unavailable", "ok", "version 1.0.5"]])
        self.assertIn("changed=true", output)
        self.assertEqual([a["action"] for a in actions], ["close"])

        gh = FakeGh()
        with contextlib.redirect_stdout(io.StringIO()):
            check_health.apply_actions([{"action": "open", "slug": SLUG, "title": "[%s] t" % SLUG, "body": "b"}], gh)
            check_health.apply_actions(actions, gh)
        self.assertFalse(gh.issues[0]["open"])


if __name__ == "__main__":
    unittest.main()
