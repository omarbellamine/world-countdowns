#!/usr/bin/env python3
"""World Countdowns crawler.

Builds ../events.json from free, key-less public sources:
  - curated.json            hand-checked headline events (always kept, win on duplicates)
  - Wikidata SPARQL         films, TV, video games, sports events, elections, space launches
  - Jolpica (Ergast) F1     Formula 1 race calendar with exact start times
  - Nager.Date              public holidays for major countries
  - built-in astronomy      eclipses, meteor-shower peaks, solstices and equinoxes

Run:  python3 crawler/crawl.py            (from the Worldcountdowns folder)
Only the standard library is used. If too few events come back (a source outage),
the previous events.json is kept and the script exits non-zero.
"""
import json, re, ssl, sys, time, unicodedata, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "events.json"
CURATED = Path(__file__).resolve().parent / "curated.json"
CACHE = Path(__file__).resolve().parent / "cache"
UA = "WorldCountdownsBot/1.0 (https://github.com/omarbellamine/world-countdowns)"
NOW = datetime.now(timezone.utc)
HORIZON = NOW + timedelta(days=365 * 5)
MIN_EVENTS = 300  # below this, assume a source failed and keep the old file

try:
    import certifi  # type: ignore
    CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    CTX = ssl.create_default_context()
    for ca in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if Path(ca).exists():
            CTX = ssl.create_default_context(cafile=ca)
            break


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get_json(url, tries=3, timeout=90, on_429=None):
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            wait = 4 * (n + 1)
            if e.code == 429:
                wait = min(int(e.headers.get("Retry-After") or 65), 120) + 2
                if on_429:
                    on_429()
            log(f"  retry {n + 1}/{tries} in {wait}s ({e.code}) {url[:70]}…")
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            log(f"  retry {n + 1}/{tries} {url[:70]}… {e}")
            time.sleep(4 * (n + 1))
    raise RuntimeError(f"failed: {url[:120]}")


# Wikidata asks bots to pace themselves; when it answers 429 we drop to one query a minute.
WD_PACE = {"gap": 3.0, "last": 0.0}


def _slow_down():
    WD_PACE["gap"] = 62.0


def sparql(query):
    wait = WD_PACE["last"] + WD_PACE["gap"] - time.time()
    if wait > 0:
        time.sleep(wait)
    url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": query, "format": "json"})
    try:
        return get_json(url, tries=5, timeout=120, on_429=_slow_down)["results"]["bindings"]
    finally:
        WD_PACE["last"] = time.time()


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80] or "event"


def norm(s):
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower())


def local_day(d):  # date-only events count down to local midnight in the viewer's zone
    return d.strftime("%Y-%m-%dT00:00:00")


def utc_iso(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_wd_time(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


# ---------------------------------------------------------------- Wikidata
DATE_STMT = """
  ?i p:{P} ?st . ?st psv:{P} ?tv . ?tv wikibase:timeValue ?d ; wikibase:timePrecision ?prec .
"""
WD_KINDS = {
    # kind: (category, icon, where-clause, date property, min sitelinks)
    "film":     ("movies", "film",  "?i wdt:P31 wd:Q11424 .",   "P577", 2),
    "tv":       ("movies", "tv",    "VALUES ?cls { wd:Q5398426 wd:Q3464665 } ?i wdt:P31 ?cls .", "P580", 2),
    "game":     ("games",  "pad",   "?i wdt:P31 wd:Q7889 .",    "P577", 2),
    "sport580": ("sports", "trophy", "?i wdt:P641 ?sport .",    "P580", 2),
    "sport585": ("sports", "trophy", "?i wdt:P641 ?sport .",    "P585", 2),
    "election": ("world",  "ballot", "?i wdt:P31/wdt:P279* wd:Q40231 .", "P585", 2),
    "launch":   ("space",  "rocket", "?i wdt:P31/wdt:P279* wd:Q2133344 .", "P619", 2),
}
FACT_PROPS = {
    "P57": "Director", "P161": "Cast", "P178": "Developer", "P123": "Publisher", "P400": "Platform",
    "P641": "Sport", "P664": "Organizer", "P17": "Country", "P276": "Location", "P541": "Office",
    "P449": "Network", "P750": "Distributor", "P136": "Genre", "P137": "Operator", "P1427": "Start point",
}
FACT_ORDER = {
    "movies": ["P57", "P161", "P136", "P750", "P449", "P17"],
    "games": ["P178", "P123", "P400", "P136"],
    "sports": ["P641", "P664", "P276", "P17"],
    "world": ["P17", "P541"],
    "space": ["P137", "P1427", "P17"],
}
SPORT_ICON = [
    ("association football", "ball"), ("football", "oval"), ("rugby", "oval"), ("tennis", "racket"),
    ("golf", "golf"), ("cricket", "bat"), ("formula", "flag"), ("motor", "flag"), ("racing", "flag"),
    ("cycling", "flag"), ("olympic", "torch"), ("multi-sport", "torch"), ("ski", "snow"), ("ice", "snow"),
    ("winter", "snow"), ("basketball", "ball"), ("baseball", "ball"), ("volleyball", "ball"),
]


def wikidata_kind(kind):
    cat, ic, where, prop, minsl = WD_KINDS[kind]
    lo = (NOW - timedelta(days=400)).strftime("%Y-%m-%d")
    hi = HORIZON.strftime("%Y-%m-%d")
    q = f"""SELECT ?i ?name ?desc ?d ?prec ?end ?sl ?article WHERE {{
  {where}
  {DATE_STMT.format(P=prop)}
  FILTER(?d >= "{lo}"^^xsd:dateTime && ?d <= "{hi}"^^xsd:dateTime)
  ?i wikibase:sitelinks ?sl . FILTER(?sl >= {minsl})
  ?i rdfs:label ?name . FILTER(lang(?name) = "en")
  OPTIONAL {{ ?i schema:description ?desc . FILTER(lang(?desc) = "en") }}
  OPTIONAL {{ ?i wdt:P582 ?end . }}
  OPTIONAL {{ ?article schema:about ?i ; schema:isPartOf <https://en.wikipedia.org/> . }}
}} LIMIT 6000"""
    rows = sparql(q)
    items = {}
    for r in rows:
        qid = r["i"]["value"].rsplit("/", 1)[-1]
        d = parse_wd_time(r["d"]["value"])
        prec = int(r["prec"]["value"])
        it = items.setdefault(qid, {"qid": qid, "name": r["name"]["value"], "desc": r.get("desc", {}).get("value", ""),
                                    "sl": int(r["sl"]["value"]), "article": r.get("article", {}).get("value"),
                                    "dates": [], "end": None, "cat": cat, "ic": ic, "kind": kind})
        it["dates"].append((d, prec))
        if r.get("end"):
            try:
                it["end"] = parse_wd_time(r["end"]["value"])
            except ValueError:
                pass
    return list(items.values())


def wikidata_facts(qids, cat):
    props = FACT_ORDER.get(cat, [])
    out = {}
    if not props:
        return out
    for i in range(0, len(qids), 150):
        chunk = qids[i:i + 150]
        q = f"""SELECT ?i ?p ?vl WHERE {{
  VALUES ?i {{ {' '.join('wd:' + x for x in chunk)} }}
  VALUES ?p {{ {' '.join('wdt:' + p for p in props)} }}
  ?i ?p ?v . ?v rdfs:label ?vl . FILTER(lang(?vl) = "en")
}}"""
        try:
            rows = sparql(q)
        except RuntimeError as e:
            log("  facts chunk failed:", e)
            continue
        for r in rows:
            qid = r["i"]["value"].rsplit("/", 1)[-1]
            p = r["p"]["value"].rsplit("/", 1)[-1]
            vals = out.setdefault(qid, {}).setdefault(p, [])
            if r["vl"]["value"] not in vals and len(vals) < 4:
                vals.append(r["vl"]["value"])
        time.sleep(1)
    return out


def from_wikidata():
    events, tba = [], []
    by_cat = {}
    for kind in WD_KINDS:
        log(f"Wikidata: {kind}")
        try:
            items = wikidata_kind(kind)
        except RuntimeError as e:
            log("  skipped:", e)
            continue
        kept = 0
        for it in items:
            # earliest date at the best available precision decides the countdown
            first = min(it["dates"], key=lambda x: x[0])
            if first[0] < NOW - timedelta(days=1) and not (it["end"] and it["end"] > NOW):
                continue  # already out / already started (film released, season underway)
            day_dates = sorted(d for d, p in it["dates"] if p >= 11 and d >= NOW - timedelta(days=1))
            if day_dates and day_dates[0] - first[0] < timedelta(days=60):
                it["start"] = day_dates[0]
                by_cat.setdefault(it["cat"], []).append(it)
                kept += 1
            elif first[1] in (9, 10) and first[0] > NOW:
                label = first[0].strftime("%b %Y") if first[1] == 10 else first[0].strftime("%Y")
                tba.append({"cat": it["cat"], "ic": it["ic"], "name": it["name"], "when": label, "sl": it["sl"],
                            "link": it["article"]})
        log(f"  {len(items)} items, {kept} with exact future dates")
        time.sleep(2)

    seen = set()
    for cat, items in by_cat.items():
        uniq = []
        for it in items:
            if it["qid"] not in seen:
                seen.add(it["qid"])
                uniq.append(it)
        log(f"Wikidata facts: {cat} ({len(uniq)})")
        facts = wikidata_facts([it["qid"] for it in uniq], cat)
        for it in uniq:
            f = facts.get(it["qid"], {})
            ic = it["ic"]
            if cat == "sports":
                sport = " ".join(f.get("P641", [])).lower() + " " + it["name"].lower()
                ic = next((v for k, v in SPORT_ICON if k in sport), "trophy")
                if "american football" in sport:
                    ic = "oval"
            where = ", ".join((f.get("P276") or [])[:2] + (f.get("P17") or [])[:2])
            start = it["start"]
            ev = {
                "id": slug(it["name"]), "cat": cat, "ic": ic, "name": it["name"],
                "desc": (it["desc"][:1].upper() + it["desc"][1:]) if it["desc"] else "",
                "t": local_day(start), "where": where, "src": "wikidata", "qid": it["qid"], "rank": it["sl"],
                "link": it["article"] or f"https://www.wikidata.org/wiki/{it['qid']}",
                "facts": [[FACT_PROPS[p], ", ".join(f[p][:3])] for p in FACT_ORDER.get(cat, []) if f.get(p)][:5],
            }
            if it["end"] and it["end"] > start + timedelta(hours=20) and it["end"] < start + timedelta(days=200):
                ev["end"] = it["end"].strftime("%Y-%m-%dT23:59:00")
            events.append(ev)
    return events, tba


# ---------------------------------------------------------------- Formula 1
def from_f1():
    events = []
    for season in ("current", str(NOW.year + 1)):
        try:
            races = get_json(f"https://api.jolpi.ca/ergast/f1/{season}.json?limit=40")["MRData"]["RaceTable"]["Races"]
        except RuntimeError as e:
            log("F1:", e)
            continue
        for r in races:
            t = r.get("time", "13:00:00Z")
            d = datetime.fromisoformat(f"{r['date']}T{t}".replace("Z", "+00:00"))
            if d < NOW:
                continue
            c = r["Circuit"]
            loc = c.get("Location", {})
            events.append({
                "id": slug(f"F1 {r['raceName']} {r['season']}"), "cat": "sports", "ic": "flag",
                "name": f"F1 {r['raceName']} {r['season']}",
                "desc": f"Round {r['round']} of the {r['season']} Formula 1 World Championship.",
                "t": utc_iso(d), "end": utc_iso(d + timedelta(hours=2)),
                "where": f"{c['circuitName']}, {loc.get('locality', '')}, {loc.get('country', '')}".strip(", "),
                "link": r.get("url"), "src": "jolpica-f1", "rank": 40,
                "facts": [["Round", r["round"]], ["Circuit", c["circuitName"]], ["Lights out", t.replace("Z", " UTC")]]
                         + ([["Sprint", r["Sprint"]["date"]]] if r.get("Sprint") else []),
            })
        time.sleep(1)
    log(f"F1: {len(events)}")
    return events


# ---------------------------------------------------------------- Holidays
HOLIDAY_COUNTRIES = ["US", "GB", "CA", "AU", "NZ", "IE", "FR", "DE", "ES", "IT", "PT", "NL", "BE", "CH", "SE",
                     "NO", "DK", "PL", "BR", "MX", "AR", "CO", "JP", "KR", "CN", "PH", "ID", "ZA", "NG", "KE",
                     "EG", "MA", "TR", "JM", "TT"]
# A holiday is kept if it is shared by several countries, or is a national day in one of these.
HOLIDAY_MAJOR = {"US", "GB", "CA", "AU", "FR", "DE", "JP", "BR", "MX", "CN", "MA", "JM", "ES", "IT", "IE"}
HOLIDAY_ICON = [("christmas", "gift"), ("new year", "spark"), ("independence", "flag"), ("halloween", "moon"),
                ("eid", "moon"), ("easter", "spark"), ("labour", "flag"), ("labor", "flag")]


def from_holidays():
    merged = {}
    names = {}
    for cc in HOLIDAY_COUNTRIES:
        for year in (NOW.year, NOW.year + 1):
            try:
                rows = get_json(f"https://date.nager.at/api/v3/PublicHolidays/{year}/{cc}", tries=2, timeout=30)
            except RuntimeError:
                continue
            for h in rows:
                d = datetime.fromisoformat(h["date"]).replace(tzinfo=timezone.utc)
                if d < NOW - timedelta(days=1) or not h.get("global", True):
                    continue
                name = h["name"]
                key = (h["date"], norm(name))
                merged.setdefault(key, {"name": name, "date": h["date"], "countries": [], "local": set()})
                merged[key]["countries"].append(cc)
                if h.get("localName") and h["localName"] != name:
                    merged[key]["local"].add(h["localName"])
            time.sleep(0.2)
    events = []
    for (date, _), h in merged.items():
        n = len(h["countries"])
        if n < 3 and not (set(h["countries"]) & HOLIDAY_MAJOR):
            continue
        name = h["name"] if n > 1 or h["name"] in ("Christmas Day", "New Year's Day") else f"{h['name']} ({h['countries'][0]})"
        year = date[:4]
        disp = f"{name} {year}"
        ic = next((v for k, v in HOLIDAY_ICON if k in name.lower()), "cal")
        where = ", ".join(h["countries"][:12]) + (f" +{n - 12} more" if n > 12 else "")
        events.append({
            "id": slug(f"{disp}-{date}" if names.get(slug(disp)) else disp), "cat": "holidays", "ic": ic,
            "name": disp, "desc": f"Public holiday in {n} countr{'y' if n == 1 else 'ies'}" + (f" · {sorted(h['local'])[0]}" if h["local"] else "") + ".",
            "t": f"{date}T00:00:00", "end": f"{date}T23:59:59", "where": where, "src": "nager-date", "rank": 3 + n,
            "facts": [["Countries", where], ["Date", date]] + ([["Local name", ", ".join(sorted(h["local"])[:3])]] if h["local"] else []),
        })
        names[slug(disp)] = True
    log(f"Holidays: {len(events)}")
    return events


# ---------------------------------------------------------------- Astronomy (built-in)
ECLIPSES = [  # (UTC greatest eclipse, type, where)
    ("2027-02-06T16:00:00Z", "Annular solar eclipse", "South America, Atlantic, West Africa"),
    ("2028-01-26T15:08:00Z", "Annular solar eclipse", "Ecuador, Peru, Brazil, Spain, Portugal"),
    ("2028-07-22T02:56:00Z", "Total solar eclipse", "Australia (Sydney), New Zealand"),
    ("2028-12-31T16:52:00Z", "Total lunar eclipse", "Europe, Asia, Australia, Africa"),
    ("2029-06-26T03:22:00Z", "Total lunar eclipse", "Americas, Western Europe, Africa"),
    ("2029-12-20T22:42:00Z", "Total lunar eclipse", "Europe, Africa, Asia, Americas"),
    ("2030-06-01T06:29:00Z", "Annular solar eclipse", "North Africa, Greece, Turkey, Russia, Japan"),
    ("2030-11-25T06:51:00Z", "Total solar eclipse", "Southern Africa, Indian Ocean, Australia"),
]
SHOWERS = [  # (name, month, day of typical peak, rate per hour)
    ("Quadrantids", 1, 3, "up to 120"), ("Lyrids", 4, 22, "about 18"), ("Eta Aquariids", 5, 6, "about 50"),
    ("Perseids", 8, 12, "up to 100"), ("Orionids", 10, 21, "about 20"), ("Leonids", 11, 17, "about 15"),
    ("Geminids", 12, 14, "up to 150"),
]
SEASONS = [(3, 20, "March equinox"), (6, 21, "June solstice"), (9, 22, "September equinox"), (12, 21, "December solstice")]


def from_astronomy():
    events = []
    for t, kind, where in ECLIPSES:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if d < NOW:
            continue
        events.append({"id": slug(f"{kind} {d.year} {d.strftime('%b')}"), "cat": "space", "ic": "eclipse" if "solar" in kind else "moon",
                       "name": f"{kind}, {d.strftime('%-d %b %Y')}", "desc": f"Visible from {where}.", "t": t,
                       "end": utc_iso(d + timedelta(hours=1)), "where": where, "src": "astronomy", "rank": 30,
                       "facts": [["Type", kind], ["Greatest eclipse", d.strftime("%H:%M UTC")], ["Visible from", where]]})
    for year in range(NOW.year, NOW.year + 4):
        for name, m, day, rate in SHOWERS:
            d = datetime(year, m, day, tzinfo=timezone.utc)
            if d < NOW:
                continue
            events.append({"id": slug(f"{name} meteor shower {year}"), "cat": "space", "ic": "spark",
                           "name": f"{name} meteor shower {year}", "desc": f"Annual peak, {rate} meteors per hour under dark skies.",
                           "t": f"{d:%Y-%m-%d}T00:00:00", "end": f"{d:%Y-%m-%d}T23:59:59", "where": "Night sky, worldwide", "src": "astronomy",
                           "rank": 12 if name in ("Perseids", "Geminids") else 6, "tbc": True,
                           "facts": [["Peak (typical)", f"{d:%-d %B}"], ["Rate", f"{rate} / hour"]]})
        for m, day, name in SEASONS:
            d = datetime(year, m, day, tzinfo=timezone.utc)
            if d < NOW:
                continue
            events.append({"id": slug(f"{name} {year}"), "cat": "space", "ic": "eclipse", "name": f"{name} {year}",
                           "desc": "Start of astronomical " + {"March": "spring", "June": "summer", "September": "autumn", "December": "winter"}[name.split()[0]] + " in the Northern Hemisphere.",
                           "t": f"{d:%Y-%m-%d}T00:00:00", "end": f"{d:%Y-%m-%d}T23:59:59", "where": "Worldwide", "src": "astronomy", "rank": 8, "tbc": True,
                           "facts": [["Date (approx.)", f"{d:%-d %B %Y}"]]})
    log(f"Astronomy: {len(events)}")
    return events


# ---------------------------------------------------------------- Merge
def main():
    curated = json.loads(CURATED.read_text())
    cur = [dict(e, rank=1000) for e in curated["events"]]
    sources = [cur]
    tba = list(curated["tba"])
    CACHE.mkdir(exist_ok=True)

    def cached(name, fn, minimum):
        """Run a source; if it fails or comes back thin, fall back to its last good result."""
        path = CACHE / f"{name}.json"
        try:
            data = fn()
            size = len(data[0]) if isinstance(data, tuple) else len(data)
            if size >= minimum:
                path.write_text(json.dumps(data, default=str))
                return data
            log(f"{name}: only {size} results, using cache")
        except Exception as e:  # noqa: BLE001
            log(f"{name} failed ({e}), using cache")
        if path.exists():
            return json.loads(path.read_text())
        return data if "data" in locals() else ([], []) if name == "wikidata" else []

    wd, wd_tba = cached("wikidata", from_wikidata, 150)
    sources.append(wd)
    tba += sorted(wd_tba, key=lambda x: -x.get("sl", 0))[:60]
    sources.append(cached("f1", from_f1, 1))
    sources.append(cached("holidays", from_holidays, 50))
    sources.append(from_astronomy())

    def start(e):
        t = e["t"]
        return datetime.fromisoformat(t.replace("Z", "+00:00")) if t.endswith("Z") else datetime.fromisoformat(t).replace(tzinfo=timezone.utc)

    def end(e):
        t = e.get("end") or e["t"]
        return datetime.fromisoformat(t.replace("Z", "+00:00")) if t.endswith("Z") else datetime.fromisoformat(t).replace(tzinfo=timezone.utc)

    out, ids, keys = [], set(), []
    for src in sources:
        for e in src:
            if end(e) < NOW - timedelta(hours=12):
                continue
            k = norm(re.sub(r"\b(19|20)\d\d\b", "", e["name"]))
            s = start(e)
            # duplicate if names match closely and dates are within 3 days (curated comes first, so it wins)
            if any((k == k2 or (len(k) > 8 and (k in k2 or k2 in k))) and abs((s - s2).days) <= 3 for k2, s2 in keys):
                continue
            keys.append((k, s))
            base, n = e["id"] or "event", 2
            while e["id"] in ids:
                e["id"] = f"{base}-{n}"
                n += 1
            ids.add(e["id"])
            out.append({k: v for k, v in e.items() if v not in (None, "", [])})

    out.sort(key=lambda e: (start(e), -e.get("rank", 0)))
    seen_tba, tba_out = set(), []
    for x in tba:
        k = norm(x["name"])
        if k not in seen_tba and not any(norm(e["name"]) == k for e in out):
            seen_tba.add(k)
            tba_out.append({k2: v for k2, v in x.items() if k2 != "sl" and v})
    by_cat = {}
    for e in out:
        by_cat[e["cat"]] = by_cat.get(e["cat"], 0) + 1
    log("Totals:", len(out), by_cat, "TBA:", len(tba_out))
    if len(out) < MIN_EVENTS:
        log(f"Only {len(out)} events (< {MIN_EVENTS}); keeping the previous events.json.")
        sys.exit(2)
    payload = {"generated": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "count": len(out), "byCategory": by_cat,
               "sources": ["Wikidata (CC0)", "Jolpica F1 API", "Nager.Date", "Curated"], "events": out, "tba": tba_out}
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(OUT)
    print(json.dumps({"ok": True, "count": len(out), "byCategory": by_cat, "tba": len(tba_out), "file": str(OUT)}))


if __name__ == "__main__":
    main()
