# World Countdowns

Live countdowns to upcoming sports events, game launches, film and TV releases, elections, public holidays and sky events.

- **Site:** `index.html`, a single static page that loads `events.json`.
- **Crawler:** `crawler/crawl.py`, Python standard library only. It merges:
  - `crawler/curated.json`: hand-checked headline events with full details (these win over duplicates)
  - [Wikidata](https://www.wikidata.org) (CC0): films, TV, video games, sports events, elections, space launches
  - [Jolpica F1 API](https://github.com/jolpica/jolpica-f1): Formula 1 race calendar
  - [Nager.Date](https://date.nager.at): public holidays
  - built-in astronomy tables: eclipses, meteor showers, solstices and equinoxes
- **Schedule:** `.github/workflows/crawl.yml` runs the crawler every day at 05:15 UTC, commits the new `events.json`, and deploys to GitHub Pages. Run it any time from the Actions tab with **Run workflow**.

If a source fails, the crawler falls back to its last good result in `crawler/cache/`. If fewer than 300 events come back in total, it keeps the previous `events.json`.

Run locally:

```bash
python3 crawler/crawl.py
python3 -m http.server 8000
```
