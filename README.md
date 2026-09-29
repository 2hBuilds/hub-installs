# hub-installs

A daily record of how many RuneLite players have each Plugin Hub plugin installed,
and a page that charts it.

The page: https://2hbuilds.github.io/hub-installs/ (once GitHub Pages is switched on
for this repository, serving from the root of `main`).

The chart and each card's "Change" rows are the daily recording described below. The
cards' Installs number and Hub rank are read live from RuneLite by the visitor's browser,
after the page has drawn: two requests, to `static.runelite.net` and `api.runelite.net`,
at most every 30 minutes while the page is open and none while its tab is hidden. A live
number carries the time the browser read it; RuneLite's feed is cached for up to 30
minutes, so the count itself can be that much older. A "Since the daily reading" row
appears when the live number differs from the day's recording. If RuneLite cannot be
reached, the cards show the recorded numbers; if it stops answering after a good read,
that read stays, marked "last live" with its time, for up to 2 hours.

## Where the numbers come from

The same two URLs runelite.net reads to show install counts:

1. `https://static.runelite.net/bootstrap.json` gives the current client version
   (the artifact named `client-<version>.jar`).
2. `https://api.runelite.net/runelite-<version>/pluginhub` gives every Hub plugin's
   install count, keyed by the plugin's slug.

A GitHub Actions job (`.github/workflows/daily.yml`) runs `fetch_installs.py` every day
at 03:15 UTC and commits the result when it changed. It can also be started by hand
from the Actions tab (workflow_dispatch).

## Adding a plugin

Add its slug - the last part of its `runelite.net/plugin-hub/show/<slug>` address - on a
new line of `plugins.txt`. That is the only step. Its history starts on the next run;
earlier days can still be looked up in `data/all/`.

## Availability alerts

Every hour (`.github/workflows/health.yml`, at 37 minutes past) `check_health.py` reads the
Plugin Hub's own list of plugins for the current client version
(`https://repo.runelite.net/plugins/manifest/<version>_full.js`) and checks that each
plugin written by an author named in `owners.txt` still has a jar players can install.
`plugins.txt` plays no part: it only picks the plugins charted on the page.

A new plugin is picked up by itself, as long as two things name an owner in `owners.txt`:
the `author=` line of its `runelite-plugin.properties` (exactly that name, or that name
among comma-separated co-authors), and the `repository=` line of its entry in
runelite/plugin-hub (`https://github.com/<owner>/...`). The author line is free text
anyone can write, so it is not enough on its own; the check looks the entry up once, when
the plugin first appears. If a plugin you expect is not watched, the run's log in the
Actions tab carries a warning saying why, and so does an owner with no plugin at all.

Each watched plugin has one of four statuses:

- **ok** - on the Hub with a jar; players can install it.
- **pending** - on the Hub with no jar and no reason given. The Hub may be part way through
  a rebuild, so this waits: still without a jar at a check 45 minutes or more later, it
  becomes unavailable - in practice at the next hourly check.
- **unavailable** - on the Hub with no jar because its build failed or RuneLite gave a reason.
  This is what the client shows as "Plugin is incompatible, requires update by its author".
- **missing** - watched before and gone from the Hub's list. Like pending, it waits first.

When a plugin becomes unavailable or missing, the check opens a GitHub issue in this
repository, labelled `hub-health`, that says what happened and what to do and mentions the
owner - so GitHub emails them. When the plugin is available again the issue is commented on
and closed. One issue per plugin, however many runs see the problem. If the issue cannot be
opened, nothing is committed and the next hourly check tries again.

State lives in `data/health.json` (also shown as a line at the top of the page) and every
change of status or client version is appended to `data/health-log.csv`:
`time,plugin,from,to,detail`. The hourly job commits only when a status or a version
changed; the daily install run checks as well and commits `data/health.json` every day, so
the "checked" time on the page is up to a day old while all is well. A page whose last
check is more than 36 hours old says the check may have stopped.

To try it by hand: the Actions tab, "hub health", Run workflow. Locally, without touching
GitHub issues (it does update `data/health.json` and `data/health-log.csv` in this folder,
so do not commit those afterwards):

    python check_health.py --actions-out actions.json

`python check_health.py --apply actions.json` performs those actions with the `gh` CLI.

To see the alarm itself work, run a fire drill: the Actions tab, "hub health", Run workflow,
tick "Send a test alert", Run. After the usual check it opens one `hub-health` issue titled
"[drill] Test alert - nothing is wrong" that mentions the owners, comments on it and closes it,
so GitHub sends its e-mail about the mention. It touches no data file. The issue stays readable
by anyone, like every issue of a public repository; it shows the GitHub name, never an e-mail address.
If the run shows as cancelled (a scheduled run queued behind it takes its place), or warns that it
only tidied up an earlier drill left open, no e-mail came from it: run the drill once more.

## Data layout

- `data/installs.csv` - the tracked plugins only, one row per plugin per day:
  `date,plugin,installs`. An empty `installs` means the plugin was not in the feed that day.
- `data/all/YYYY-MM-DD.csv` - every Hub plugin on that day: `plugin,installs`, largest first.
- `data/latest.json` - the most recent day: `{"date", "version", "counts": {slug: installs}}`.

Dates are UTC. Running twice on the same day replaces that day's rows; nothing older is
ever removed.

## Running it by hand

Python 3, standard library only:

    python fetch_installs.py
    python -m unittest discover -s tests

To look at the page locally, serve the folder (opening `index.html` as a file will not
load the data):

    python -m http.server 8000

then open http://localhost:8000/.
