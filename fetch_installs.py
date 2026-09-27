"""Fetch RuneLite Plugin Hub install counts and append them to the history in data/.

Run: python fetch_installs.py
"""
import csv
import datetime
import io
import json
import os
import re
import sys
import urllib.request

BOOTSTRAP_URL = "https://static.runelite.net/bootstrap.json"
PLUGINHUB_URL = "https://api.runelite.net/runelite-{version}/pluginhub"
USER_AGENT = "hub-installs (github.com/2hBuilds)"
ROOT = os.path.dirname(os.path.abspath(__file__))


class FetchError(Exception):
    pass


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
    except Exception as e:
        raise FetchError("could not fetch %s: %s" % (url, e))
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as e:
        raise FetchError("%s did not return JSON: %s" % (url, e))


def client_version(bootstrap):
    """The version of the artifact whose name starts with "client".

    The artifact has carried no "version" field in practice (2026-09-27), only a
    name like "client-1.12.39.jar", so the version is read from the name when
    the field is absent.
    """
    artifacts = bootstrap.get("artifacts") if isinstance(bootstrap, dict) else None
    if not isinstance(artifacts, list):
        raise FetchError("bootstrap.json has no 'artifacts' list")
    for artifact in artifacts:
        name = artifact.get("name", "") if isinstance(artifact, dict) else ""
        if not name.startswith("client"):
            continue
        if artifact.get("version"):
            return str(artifact["version"])
        match = re.fullmatch(r"client-(.+)\.jar", name)
        if match:
            return match.group(1)
        raise FetchError("client artifact %r has no version" % name)
    raise FetchError("bootstrap.json has no artifact named client*")


def parse_counts(feed):
    if not isinstance(feed, dict) or not feed:
        raise FetchError("pluginhub feed is not a non-empty JSON object")
    counts = {}
    for slug, n in feed.items():
        # bool is a subclass of int; a true/false count would be a parse failure
        if not isinstance(n, int) or isinstance(n, bool):
            raise FetchError("pluginhub feed has a non-integer count for %r: %r" % (slug, n))
        counts[slug] = n
    return counts


def read_tracked(path):
    with open(path, encoding="utf-8") as f:
        lines = (line.strip() for line in f)
        return [line for line in lines if line and not line.startswith("#")]


def write_text(path, text):
    """Write via a temp file and rename, so a crash never leaves half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def csv_text(header, rows):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def write_day(data_dir, date, counts):
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    write_text(os.path.join(data_dir, "all", date + ".csv"), csv_text(["plugin", "installs"], ranked))


def write_latest(data_dir, date, version, counts):
    body = {"date": date, "version": version, "counts": dict(sorted(counts.items()))}
    write_text(os.path.join(data_dir, "latest.json"), json.dumps(body, indent=1) + "\n")


def update_series(data_dir, date, tracked, counts):
    """Replace today's rows in installs.csv; every other day's rows are kept as they are."""
    path = os.path.join(data_dir, "installs.csv")
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header != ["date", "plugin", "installs"]:
                raise FetchError("%s has an unexpected header %r" % (path, header))
            rows = [row for row in reader if row and row[0] != date]
    for slug in tracked:
        n = counts.get(slug)
        rows.append([date, slug, "" if n is None else str(n)])
    rows.sort(key=lambda row: (row[0], row[1]))
    write_text(path, csv_text(["date", "plugin", "installs"], rows))


def run(root=ROOT, fetch=None, today=None):
    fetch = fetch or fetch_json
    date = today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    tracked = read_tracked(os.path.join(root, "plugins.txt"))
    version = client_version(fetch(BOOTSTRAP_URL))
    counts = parse_counts(fetch(PLUGINHUB_URL.format(version=version)))
    data_dir = os.path.join(root, "data")
    write_day(data_dir, date, counts)
    write_latest(data_dir, date, version, counts)
    update_series(data_dir, date, tracked, counts)
    missing = [slug for slug in tracked if slug not in counts]
    print("%s: client %s, %d plugins in the feed" % (date, version, len(counts)))
    for slug in tracked:
        print("  %s: %s" % (slug, counts.get(slug, "MISSING from the feed")))
    return missing


def main():
    try:
        run()
    except (FetchError, OSError, csv.Error) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
