# hub-installs

A daily record of how many RuneLite players have each Plugin Hub plugin installed,
and a page that charts it.

The page: https://2hbuilds.github.io/hub-installs/ (once GitHub Pages is switched on
for this repository, serving from the root of `main`).

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
