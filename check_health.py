"""Check that our Plugin Hub plugins are still available, and raise an alert when one is not.

Run: python check_health.py --actions-out actions.json
     python check_health.py --apply actions.json      (opens / closes GitHub issues with gh)

The watched plugins are every Hub plugin whose author is named in owners.txt, plus any
plugin a previous run watched that has since left the manifest. The author field is free
text anyone can write, so a plugin joins only once its runelite/plugin-hub entry is seen
to point at a github.com/<owner>/ repository of an owner in owners.txt. State is kept in
data/health.json and every status change is appended to data/health-log.csv.
"""
import argparse
import csv
import datetime
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request

from fetch_installs import (
    BOOTSTRAP_URL, ROOT, USER_AGENT, FetchError, client_version, fetch_json, read_tracked, write_text,
)

MANIFEST_URL = "https://repo.runelite.net/plugins/manifest/{version}_full.js"
# the two-line file a Hub submission adds: repository=https://github.com/<owner>/<repo>.git, commit=<sha>
PLUGIN_FILE_URL = "https://raw.githubusercontent.com/runelite/plugin-hub/master/plugins/{slug}"
LABEL = "hub-health"
# used with fullmatch: re's $ would also accept a trailing newline
SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
# what a version may look like before it goes into a URL, a commit message or GITHUB_OUTPUT
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,39}")
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
REPOSITORY = re.compile(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/[^/\s]+?/?")
LOG_HEADER = ["time", "plugin", "from", "to", "detail"]
# no jar (or no entry) on a second run at least this long after the first raises the alarm;
# the Hub may be part way through a rebuild, and the daily run or a manual one can come minutes later
PENDING_GRACE = datetime.timedelta(minutes=45)
ALARMED = ("unavailable", "missing")


class ManifestMissing(FetchError):
    """The URL answered 404: for the manifest, normal for a while after a client release."""


class ManifestError(Exception):
    """A manifest arrived but could not be read."""


class StateError(Exception):
    pass


def fetch_bytes(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ManifestMissing("%s is not there yet (404)" % url)
        raise FetchError("could not fetch %s: %s" % (url, e))
    except Exception as e:
        raise FetchError("could not fetch %s: %s" % (url, e))


def parse_manifest(body):
    """{internalName: display entry}, {internalName of every jar} from the binary manifest.

    The file is a 4-byte big-endian length N, N bytes of signature, then UTF-8 JSON
    {"display": [...], "jars": [...]}. Entries without a string internalName are skipped.
    """
    if not isinstance(body, bytes) or len(body) < 4:
        raise ManifestError("manifest is %d bytes, too short for its length field" % len(body or b""))
    (n,) = struct.unpack(">I", body[:4])
    if 4 + n >= len(body):
        raise ManifestError("manifest signature length %d runs past the end of its %d bytes" % (n, len(body)))
    try:
        doc = json.loads(body[4 + n:].decode("utf-8"))
    except ValueError as e:
        raise ManifestError("manifest is not JSON after its signature: %s" % e)
    if not isinstance(doc, dict) or not isinstance(doc.get("display"), list) or not isinstance(doc.get("jars"), list):
        raise ManifestError("manifest has no 'display' and 'jars' lists")
    display = {}
    for entry in doc["display"]:
        if isinstance(entry, dict) and isinstance(entry.get("internalName"), str):
            display[entry["internalName"]] = entry
    jars = set()
    for entry in doc["jars"]:
        if isinstance(entry, dict) and isinstance(entry.get("internalName"), str):
            jars.add(entry["internalName"])
    return display, jars


def repository_owner(body):
    """The GitHub account of the repository= line in a plugin-hub plugins/<slug> file, or None."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "repository":
            match = REPOSITORY.fullmatch(value.strip())
            return match.group(1) if match else None
    return None


# --- text from the manifest is written by strangers --------------------------------------

def plain(value, limit):
    """One line of text: no control or format characters, whitespace runs as one space, capped."""
    if value is None:
        return ""
    text = "".join(" " if c.isspace() else c for c in str(value) if c.isspace() or unicodedata.category(c)[0] != "C")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _break_links(text):
    # a zero-width space stops GitHub's autolinks (www., scheme://, GH-123)
    text = re.sub(r"(?i)www\.", lambda m: m.group(0)[:3] + "​.", text)
    text = text.replace("://", ":​//")
    return re.sub(r"(?i)\bgh-(?=\d)", lambda m: m.group(0)[:2] + "​-", text)


def harmless(value, limit):
    """For an issue body or comment: plain, then no mention, no link, no image, no HTML.

    @ becomes a full-width at sign, and every character markdown could build a link,
    image, tag, reference or code span from is backslash-escaped.
    """
    text = plain(value, limit).replace("@", "＠")
    text = re.sub(r"([\\`*_{}\[\]()<>#!|~&])", r"\\\1", text)
    return _break_links(text)


def harmless_title(value, limit=80):
    """For an issue title, which GitHub shows as text (only `code` renders), so no backslashes."""
    return _break_links(plain(value, limit).replace("@", "＠").replace("`", "'"))


def safe_version(value):
    text = plain(value, 40)
    return text if VERSION.fullmatch(text) else "?"


# --- state ------------------------------------------------------------------------------

def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_time(seconds):
    """'2026-09-28 14:21:51 UTC' from seconds since the epoch, or None when it is not a number."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    try:
        moment = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def is_set(value):
    return value is not None and value is not False and value != ""


def parse_iso(text):
    """A datetime from iso()'s own format, or None."""
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def load_state(path):
    if not os.path.exists(path):
        return None
    try:
        # utf-8-sig, as read_tracked: a hand edit in Windows Notepad may add a byte-order mark
        with open(path, encoding="utf-8-sig") as f:
            state = json.load(f)
    except ValueError as e:
        raise StateError("%s is not JSON: %s" % (path, e))
    plugins = state.get("plugins") if isinstance(state, dict) else None
    if not isinstance(plugins, list) or not all(isinstance(p, dict) and isinstance(p.get("slug"), str) for p in plugins):
        raise StateError("%s has no 'plugins' list of {slug, ...}" % path)
    return state


def owner_name(line):
    """An owners.txt line as a name: trimmed, a leading @ dropped."""
    return line.strip().lstrip("@").strip()


def owner_of(author, owners):
    """The owners.txt name that matches author, or one of its comma-separated co-authors
    (trimmed, any case), or None."""
    names = {part.strip().lower() for part in author.split(",")} if isinstance(author, str) else set()
    names.discard("")
    for owner in owners:
        if owner_name(owner).lower() in names:
            return owner_name(owner)
    return None


class Outcome:
    def __init__(self, state, rows, actions, changed, summary, warnings=()):
        self.state = state
        self.rows = rows
        self.actions = actions
        self.changed = changed
        self.summary = summary
        self.warnings = list(warnings)


def has_waited(old, now):
    """True when a plugin's previous record already alarmed, or has been pending for PENDING_GRACE.

    A pending since that cannot be read, or lies in the future, counts as waited: never hold an alarm back on it.
    """
    was = old.get("status") if old else None
    if was in ALARMED:
        return True
    if was != "pending":
        return False
    since = parse_iso(old.get("since"))
    return since is None or since > now or now - since >= PENDING_GRACE


def status_of(entry, has_jar, waited, version):
    """(status, reason) for one watched plugin; waited is has_waited() of its previous record."""
    if entry is None:
        if waited:
            return "missing", "not in the Plugin Hub manifest for RuneLite %s" % version
        return "pending", "not in the Plugin Hub manifest for RuneLite %s - checking again next run" % version
    if has_jar:
        return "ok", None
    if is_set(entry.get("unavailableReason")):
        return "unavailable", plain(entry["unavailableReason"], 300)
    if is_set(entry.get("buildFailAt")):
        when = utc_time(entry["buildFailAt"])
        reason = "build failed against RuneLite %s" % version
        return "unavailable", reason + (" at " + when if when else "")
    if waited:
        return "unavailable", "no jar published, no reason given"
    return "pending", "no jar published yet - checking again next run"


def candidates(display, owners):
    """Slugs of the display entries whose author names an owner."""
    return [slug for slug, entry in display.items() if owner_of(entry.get("author"), owners)]


def evaluate(prev, display, jars, version, owners, now, confirmed):
    """Compare this manifest with the previous state; nothing here touches the disk.

    confirmed holds the slugs not watched before whose plugin-hub entry was seen this run to
    point at an owner's repository; a plugin already watched needs no second look.
    """
    stamp = iso(now)
    before = {p["slug"]: p for p in (prev or {}).get("plugins", [])}
    warnings = []
    watched = {}
    for slug in candidates(display, owners):
        if slug in before or slug in confirmed:
            watched[slug] = display[slug]
    for owner in owners:
        if owner_name(owner) and not any(owner_of(e.get("author"), [owner]) for e in display.values()):
            warnings.append("no plugin in the manifest has the author %r from owners.txt" % plain(owner, 40))
    # a plugin we watched that has left the manifest stays watched, so its going is noticed,
    # as long as its recorded author is still one of ours
    for slug, old in before.items():
        if slug not in display and owner_of(old.get("author"), owners):
            watched[slug] = None
    plugins, rows, actions, parts = [], [], [], []
    prev_version = (prev or {}).get("version")
    if prev_version != version:
        rows.append([stamp, "runelite", prev_version or "", version, ""])
        parts.append("runelite %s" % (safe_version(version) if not prev_version
                                        else "%s -> %s" % (safe_version(prev_version), safe_version(version))))
    for slug in sorted(watched):
        if not SLUG.fullmatch(slug):
            print("note: skipping watched plugin with an invalid slug %r" % plain(slug, 80))
            continue
        entry, old = watched[slug], before.get(slug)
        was = old.get("status") if old else None
        status, reason = status_of(entry, slug in jars, has_waited(old, now), version)
        if entry is not None:
            name = plain(entry.get("displayName"), 80) or slug
            author = plain(entry.get("author"), 80)
            plugin_version = plain(entry.get("version"), 40)
            warning = plain(entry.get("warning"), 300) or None
        else:
            name, author = old.get("name") or slug, old.get("author") or ""
            plugin_version, warning = old.get("version") or "", old.get("warning")
        record = {
            "slug": slug, "name": name, "author": author, "version": plugin_version,
            "status": status, "reason": reason, "since": old["since"] if old and was == status and old.get("since") else stamp,
            "warning": warning,
        }
        plugins.append(record)
        if was != status:
            rows.append([stamp, slug, was or "", status, reason or "version %s" % plugin_version])
            parts.append("%s %s" % (slug, "joined (%s)" % status if old is None else "%s -> %s" % (was, status)))
        elif old and old.get("version") != plugin_version:
            parts.append("%s %s -> %s" % (slug, safe_version(old.get("version")), safe_version(plugin_version)))
        if status in ALARMED and was not in ALARMED:
            actions.append(open_action(record, entry, version, owners))
        elif status == "ok" and was in ALARMED:
            actions.append(close_action(record, version, stamp))
    for slug, old in sorted(before.items()):
        if slug not in watched and SLUG.fullmatch(slug):
            rows.append([stamp, slug, old.get("status") or "", "", "no longer watched"])
            parts.append("%s left" % slug)
            if slug in display:
                warnings.append("%s is still on the Plugin Hub but its author %r is not in owners.txt;"
                                " it is no longer watched" % (slug, plain(display[slug].get("author"), 80)))
    since = (prev or {}).get("versionSince") if prev_version == version else None
    state = {"checkedAt": stamp, "version": version, "versionSince": since or stamp, "plugins": plugins}
    summary = "; ".join(parts)
    if len(summary) > 200:
        summary = summary[:197] + "..."
    return Outcome(state, rows, actions, bool(parts), summary or "no change", warnings)


# --- alert actions ------------------------------------------------------------------------

def open_action(record, entry, version, owners):
    slug = record["slug"]
    lines = [
        "**%s** (`%s`) is **%s** on the RuneLite Plugin Hub: players cannot install or run it."
        % (harmless(record["name"], 80), slug, record["status"]),
        "",
        "- Status: %s" % record["status"],
        "- Reason: %s" % harmless(record["reason"], 300),
        "- RuneLite client: %s" % harmless(version, 40),
    ]
    failed = utc_time(entry.get("buildFailAt")) if entry else None
    if failed:
        lines.append("- Build failed at: %s" % failed)
    if record["version"]:
        lines.append("- Plugin version on the Hub: %s" % harmless(record["version"], 40))
    if record["warning"]:
        lines.append("- Hub warning (for information): %s" % harmless(record["warning"], 300))
    lines += [
        "",
        "### What to do",
        "",
        "1. Build and test against the new client: `./gradlew run -PruneLiteVersion=%s`." % safe_version(version),
        "2. Fix what no longer compiles.",
        "3. Send an update pull request to runelite/plugin-hub that changes `commit=` in `plugins/%s`." % slug,
        "",
        "- https://runelite.net/plugin-hub/show/%s" % slug,
        "- https://github.com/runelite/plugin-hub/blob/master/plugins/%s" % slug,
        "",
    ]
    owner = owner_of(record["author"], owners)
    mentions = [owner] if owner else list(owners)
    lines.append(" ".join("@" + owner_name(o) for o in mentions if GITHUB_LOGIN.fullmatch(owner_name(o))))
    return {
        "action": "open", "slug": slug,
        "title": "[%s] %s is unavailable on the Plugin Hub" % (slug, harmless_title(record["name"])),
        "body": "\n".join(lines) + "\n",
    }


def close_action(record, version, stamp):
    return {
        "action": "close", "slug": record["slug"],
        "comment": "**%s** is available on the Plugin Hub again: version %s with a jar, RuneLite %s (checked %s). Closing."
        % (harmless(record["name"], 80), harmless(record["version"], 40), harmless(version, 40), stamp),
    }


def gh(args, timeout=60):
    try:
        done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FetchError("%s gave no answer in %d seconds" % (" ".join(args[:3]), timeout))
    if done.returncode != 0:
        raise FetchError("%s failed: %s" % (" ".join(args[:3]), (done.stderr or "").strip()))
    return done.stdout


def _with_file(text, use):
    handle, path = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return use(path)
    finally:
        os.remove(path)


def apply_actions(actions, run=gh):
    """Open or close hub-health issues with gh; running the same actions twice changes nothing more.

    run takes an argument list (never a shell string) and returns gh's stdout.
    """
    if not actions:
        print("no issue to open or close")
        return
    run(["gh", "label", "create", LABEL, "--force", "--color", "d73a4a",
         "--description", "Plugin Hub availability alerts from check_health.py"])
    listed = json.loads(run(["gh", "issue", "list", "--label", LABEL, "--state", "open",
                             "--json", "number,title", "--limit", "200"]) or "[]")
    issues = [i for i in listed if isinstance(i, dict) and isinstance(i.get("title"), str)]
    for action in actions:
        slug = action.get("slug") if isinstance(action, dict) else None
        if not isinstance(slug, str) or not SLUG.fullmatch(slug):
            print("note: skipping an action with an invalid slug")
            continue
        mine = [i for i in issues if i["title"].startswith("[%s]" % slug)]
        if action.get("action") == "open":
            if mine:
                print("%s: issue #%s is already open" % (slug, mine[0].get("number")))
                continue
            url = _with_file(action["body"], lambda path: run(
                ["gh", "issue", "create", "--title", action["title"], "--body-file", path, "--label", LABEL]))
            issues.append({"number": None, "title": action["title"]})
            print("%s: opened %s" % (slug, (url or "").strip()))
        elif action.get("action") == "close":
            for issue in mine:
                number = str(issue["number"])
                _with_file(action["comment"], lambda path: run(["gh", "issue", "comment", number, "--body-file", path]))
                run(["gh", "issue", "close", number])
                print("%s: closed #%s" % (slug, number))
            if not mine:
                print("%s: no open issue to close" % slug)


# --- the run --------------------------------------------------------------------------------

def append_log(path, rows):
    """Append rows; the header goes into a missing or empty file, and a last line that lost its
    newline (a hand edit) gets one back first, so a new row is never glued onto it."""
    if not rows:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    size = os.path.getsize(path) if os.path.exists(path) else 0
    ends_in_newline = True
    if size:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            ends_in_newline = f.read(1) == b"\n"
    with open(path, "a", encoding="utf-8", newline="") as f:
        if not ends_in_newline:
            f.write("\n")
        writer = csv.writer(f, lineterminator="\n")
        if not size:
            writer.writerow(LOG_HEADER)
        writer.writerows(rows)


def confirm(display, owners, before, fetch_file):
    """The slugs among the author matches not watched before whose plugin-hub entry names an
    owner's github.com repository. A manifest's author field is free text anyone can write;
    the repository a plugin is built from is not."""
    confirmed = set()
    for slug in candidates(display, owners):
        if slug in before:
            continue
        if not SLUG.fullmatch(slug):
            print("note: skipping watched plugin with an invalid slug %r" % plain(slug, 80))
            continue
        try:
            repo_owner = repository_owner(fetch_file(PLUGIN_FILE_URL.format(slug=slug)))
        except FetchError as e:
            print("::warning::%s not watched yet, its plugin-hub entry could not be read: %s" % (slug, plain(e, 200)))
            continue
        if repo_owner and owner_of(repo_owner, owners):
            confirmed.add(slug)
        else:
            print("::warning::%s names an owner as its author, but its plugin-hub entry points at %s; not watched"
                  % (slug, "github.com/%s" % repo_owner if repo_owner else "no github.com repository"))
    return confirmed


def run(root=ROOT, fetch=None, fetch_manifest=None, now=None, fetch_file=None):
    """One check. Returns an Outcome, or None when the Hub could not be read (nothing written).

    Raises ManifestError for a manifest that arrived unreadable, StateError / OSError for our own files.
    """
    fetch = fetch or fetch_json
    fetch_manifest = fetch_manifest or fetch_bytes
    fetch_file = fetch_file or fetch_bytes
    now = now or datetime.datetime.now(datetime.timezone.utc)
    owners = read_tracked(os.path.join(root, "owners.txt"))
    data_dir = os.path.join(root, "data")
    prev = load_state(os.path.join(data_dir, "health.json"))
    try:
        version = client_version(fetch(BOOTSTRAP_URL))
        if not VERSION.fullmatch(version):
            raise FetchError("client version %r does not look like a version" % plain(version, 40))
        body = fetch_manifest(MANIFEST_URL.format(version=version))
    except ManifestMissing as e:
        print("no manifest yet: %s; nothing changed" % e)
        return None
    except FetchError as e:
        print("::warning::hub health not checked: %s" % e)
        return None
    display, jars = parse_manifest(body)
    before = {p["slug"] for p in (prev or {}).get("plugins", [])}
    outcome = evaluate(prev, display, jars, version, owners, now, confirm(display, owners, before, fetch_file))
    for warning in outcome.warnings:
        print("::warning::%s" % warning)
    append_log(os.path.join(data_dir, "health-log.csv"), outcome.rows)
    write_text(os.path.join(data_dir, "health.json"), json.dumps(outcome.state, indent=1) + "\n")
    print("%s: client %s, %d plugins in the manifest, %d watched" % (
        outcome.state["checkedAt"], version, len(display), len(outcome.state["plugins"])))
    for p in outcome.state["plugins"]:
        print("  %s %s: %s%s" % (p["slug"], p["version"], p["status"], " (%s)" % p["reason"] if p["reason"] else ""))
    return outcome


def report(changed, summary, actions, actions_out, env):
    lines = ["changed=%s" % ("true" if changed else "false"), "summary=%s" % summary]
    for line in lines:
        print(line)
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    if actions_out:
        with open(actions_out, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps(actions, indent=1) + "\n")


def main(argv=None, root=ROOT, env=None, fetch=None, fetch_manifest=None, now=None, runner=gh, fetch_file=None):
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description="Check our Plugin Hub plugins are available.")
    parser.add_argument("--actions-out", help="write the issue actions of this run to this JSON file")
    parser.add_argument("--apply", metavar="ACTIONS", help="perform the actions in this JSON file with gh, then stop")
    args = parser.parse_args(argv)
    if args.apply:
        try:
            with open(args.apply, encoding="utf-8") as f:
                actions = json.load(f)
            if not isinstance(actions, list):
                raise ValueError("%s is not a JSON list" % args.apply)
            apply_actions(actions, runner)
        except (FetchError, OSError, ValueError, KeyError) as e:
            print("error: %s" % e, file=sys.stderr)
            return 1
        return 0
    try:
        outcome = run(root, fetch, fetch_manifest, now, fetch_file)
    except (ManifestError, StateError, FetchError, OSError, csv.Error) as e:
        print("error: %s" % e, file=sys.stderr)
        report(False, "error", [], args.actions_out, env)
        return 1
    except Exception as e:
        print("error: unexpected %s: %s" % (type(e).__name__, e), file=sys.stderr)
        report(False, "error", [], args.actions_out, env)
        return 1
    if outcome is None:
        report(False, "no change", [], args.actions_out, env)
        return 0
    report(outcome.changed, outcome.summary, outcome.actions, args.actions_out, env)
    return 0


if __name__ == "__main__":
    # manifest text can hold any character; a Windows console would otherwise raise on printing it
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    sys.exit(main())
