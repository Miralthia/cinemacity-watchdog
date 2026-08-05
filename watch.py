#!/usr/bin/env python3
"""Hlídá rozpis Cinema City a hlásí nově vypsaná představení.

Ve výchozím nastavení: film "Odyssea" v sále, jehož název obsahuje "IMAX".
Data bere z veřejného JSON API cinemacity.cz (bez klíče, bez přihlášení).

Stav (už viděná představení) drží v JSON souboru, takže při každém běhu
hlásí jen to, co přibylo od minule.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SITE_ID = "10101"  # cinemacity.cz
BASE = f"https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/{SITE_ID}"
LANG = "cs_CZ"
# Poctivá identifikace místo předstírání prohlížeče.
UA = (os.environ.get("USER_AGENT")
      or "odyssea-watch/1.0 (osobni hlidka vstupenek; nizky objem dotazu)")

FILM_PATTERN = os.environ.get("FILM_PATTERN", "odyss").lower()
AUDITORIUM_PATTERN = os.environ.get("AUDITORIUM_PATTERN", "imax").lower()
HORIZON_DAYS = int(os.environ.get("HORIZON_DAYS", "180"))
# Atribut, podle kterého API umí filtrovat kina — levná nápověda, kde hledat
# IMAX sály. Doplňuje (nenahrazuje) sondu podle názvu sálu.
HINT_ATTR = os.environ.get("HINT_ATTR", "70-mm")
DELAY = float(os.environ.get("REQUEST_DELAY", "0.25"))
# Kapacita sálu pro přepočet availabilityRatio na počet míst. IMAX VOLVO na
# Floře má odhadem 385 sedadel (1 místo ~ 1/385 ~ 0.0026). Ukládá se celé
# číslo míst, ne surový ratio — jinak by drobné kolísání ratia měnilo stav
# a workflow by si commitoval diff skoro při každém běhu.
CAPACITY = int(os.environ.get("CAPACITY", "385"))
# O kolik míst musí přibýt, aby se to hlásilo. 1 = i jediná vrácená vstupenka.
MIN_SEATS_DELTA = int(os.environ.get("MIN_SEATS_DELTA", "1"))

CZ_DAYS = ["po", "út", "st", "čt", "pá", "so", "ne"]

# API vrací eventDateTime bez zóny, v místním čase kina. Runner v GitHub
# Actions jede v UTC, takže by se čas představení porovnával s časem o dvě
# hodiny pozadu — projekce, která právě doběhla, by vypadala jako budoucí
# a při zmizení z rozpisu by se falešně nahlásila jako zrušená.
CINEMA_TZ = ZoneInfo("Europe/Prague")


def now():
    """Aktuální čas v zóně kina, bez tzinfo — porovnatelný s daty z API."""
    return datetime.now(CINEMA_TZ).replace(tzinfo=None)


def api(path):
    """GET na data-api-service; vrací obsah klíče "body"."""
    url = f"{BASE}{path}"
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))["body"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise SystemExit(f"API selhalo po 4 pokusech: {url}\n{last}")


def horizon():
    return (date.today() + timedelta(days=HORIZON_DAYS)).isoformat()


def fetch_cinemas():
    body = api(f"/cinemas/with-event/until/{horizon()}?attr=&lang={LANG}")
    return {c["id"]: c for c in body["cinemas"]}


def fetch_dates(cinema_id):
    time.sleep(DELAY)
    return api(f"/dates/in-cinema/{cinema_id}/until/{horizon()}?attr=&lang={LANG}")["dates"]


def fetch_day(cinema_id, day):
    time.sleep(DELAY)
    body = api(f"/film-events/in-cinema/{cinema_id}/at-date/{day}?attr=&lang={LANG}")
    films = {f["id"]: f for f in body.get("films", [])}
    return films, body.get("events", [])


def hint_cinema_ids():
    """Kina, která podle API mají představení s atributem HINT_ATTR."""
    if not HINT_ATTR:
        return set()
    body = api(f"/cinemas/with-event/until/{horizon()}?attr={HINT_ATTR}&lang={LANG}")
    return {c["id"] for c in body["cinemas"]}


def is_target_hall(event):
    return AUDITORIUM_PATTERN in (event.get("auditorium") or "").lower()


def collect():
    """Projde relevantní kina a vrátí {event_id: záznam} pro hlídaná představení.

    Aby se netahal celý rozpis všech kin, běží to dvoufázově: nejdřív se
    zjistí, která kina vůbec mají hlídaný sál (jedna sonda na kino + nápověda
    z API), a teprve ta se projdou do hloubky.
    """
    cinemas = fetch_cinemas()
    dates_by_cinema = {cid: fetch_dates(cid) for cid in cinemas}

    candidates = hint_cinema_ids() & set(cinemas)
    day_cache = {}
    for cid, days in dates_by_cinema.items():
        if not days:
            continue
        probe = days[0]
        day_cache[(cid, probe)] = fetch_day(cid, probe)
        if any(is_target_hall(e) for e in day_cache[(cid, probe)][1]):
            candidates.add(cid)

    found = {}
    for cid in sorted(candidates):
        for day in dates_by_cinema.get(cid, []):
            films, events = day_cache.get((cid, day)) or fetch_day(cid, day)
            for e in events:
                film = films.get(e["filmId"], {})
                if FILM_PATTERN not in film.get("name", "").lower():
                    continue
                if not is_target_hall(e):
                    continue
                found[e["id"]] = {
                    "id": e["id"],
                    "film": film.get("name", e["filmId"]),
                    "filmLink": film.get("link"),
                    "cinema": cinemas[cid]["displayName"],
                    "cinemaId": cid,
                    "datetime": e["eventDateTime"],
                    "auditorium": e.get("auditorium"),
                    "attrs": e.get("attributeIds", []),
                    # Žádné z polí, která API nabízí, není použitelné jako
                    # odkaz: bookingLink vrací na GET 404, obsoleteBookingUrl
                    # je i podle názvu mrtvý a bookingRouterLaunchLink vede na
                    # stránku se samoodesílacím POST formulářem, jehož cíl
                    # (tickets.rel.…) na přímý GET odpoví 403. Ten POST ale
                    # skončí na prosté adrese /order/{id}, která funguje i na
                    # GET a otevře rovnou výběr sedadel. Pozor, parametr lang
                    # tady dělá 404 — musí se vynechat.
                    "booking": f"https://tickets.cinemacity.cz/order/{e.get('presentationCode') or e['id']}",
                    "soldOut": bool(e.get("soldOut")),
                    # availabilityRatio = podíl volných míst, rozlišení ~1 sedadlo.
                    "seats": round(float(e.get("availabilityRatio") or 0.0) * CAPACITY),
                }
    return found


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"updated": None, "events": {}}


def save_state(path, events):
    """Zapíše stav, ale jen když se změnila množina představení.

    Kdyby se soubor přepisoval při každém běhu, měnilo by se v něm razítko
    "updated" a workflow by si po sobě commitoval prázdnou změnu 48× denně.
    Rozhoduje proto seznam ID a počet volných míst — přesně to, na čem stojí
    hlášení. Místa se ukládají jako celé číslo, takže kolísání ratia pod
    jedno sedadlo stav nemění a zbytečný commit nevznikne.
    """
    if signature(events) == signature(load_state(path).get("events", {})):
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "updated": now().replace(microsecond=0).isoformat(),
        "events": dict(sorted(events.items(), key=lambda kv: kv[1]["datetime"])),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=False)
        fh.write("\n")
    return True


def signature(events):
    """Co dělá stav "změněným": množina ID + počet volných míst u každého."""
    return {k: (v.get("seats"), bool(v.get("soldOut"))) for k, v in events.items()}


def freed_seats(known, current):
    """Představení, kde přibyla místa (vrácené vstupenky) nebo se odvyprodalo.

    Záznamy bez klíče "seats" pocházejí ze staršího formátu stavu — u nich se
    první běh po upgradu jen tiše zapíše jako výchozí hladina.
    """
    out = []
    for eid, ev in current.items():
        prev = known.get(eid)
        if not prev or prev.get("seats") is None:
            continue
        gain = (ev.get("seats") or 0) - (prev.get("seats") or 0)
        back = prev.get("soldOut") and not ev.get("soldOut")
        if gain >= MIN_SEATS_DELTA or back:
            out.append(dict(ev, gain=gain, was_sold_out=bool(prev.get("soldOut"))))
    return sorted(out, key=lambda e: e["datetime"])


def prune_past(events):
    """Zahodí ze stavu představení, která už proběhla — ať soubor neroste."""
    cutoff = (now() - timedelta(days=1)).isoformat()
    return {k: v for k, v in events.items() if v["datetime"] >= cutoff}


def fmt_dt(iso):
    dt = datetime.fromisoformat(iso)
    return f"{CZ_DAYS[dt.weekday()]} {dt.day}. {dt.month}. {dt.year} v {dt:%H:%M}"


def fmt_short(iso):
    dt = datetime.fromisoformat(iso)
    return f"{dt.day}. {dt.month}."


def render(new_events, gone_events, freed=()):
    """Markdown tělo hlášení."""
    lines = []
    if freed:
        lines.append(f"### Uvolněná místa ({len(freed)})\n")
        for cinema, group in group_by_cinema(freed):
            lines.append(f"**{cinema}**\n")
            for e in group:
                if e["was_sold_out"]:
                    what = "znovu v prodeji"
                elif e["gain"] == 1:
                    what = "+1 místo"
                else:
                    what = f"+{e['gain']} míst"
                free = e.get("seats")
                stav = f", volno ~{free}" if free is not None else ""
                link = f" — [koupit]({e['booking']})" if e["booking"] else ""
                lines.append(
                    f"- **{what}**{stav} — {fmt_dt(e['datetime'])} · "
                    f"{e['auditorium']}{link}"
                )
            lines.append("")
    if new_events:
        lines.append(f"### Nově vypsáno ({len(new_events)})\n")
        for cinema, group in group_by_cinema(new_events):
            lines.append(f"**{cinema}**\n")
            for e in group:
                flags = []
                if "70-mm" in e["attrs"]:
                    flags.append("70mm")
                if "subbed" in e["attrs"]:
                    flags.append("titulky")
                if "dubbed" in e["attrs"]:
                    flags.append("dabing")
                if e["soldOut"]:
                    flags.append("**vyprodáno**")
                suffix = f" — {', '.join(flags)}" if flags else ""
                link = f" — [koupit]({e['booking']})" if e["booking"] else ""
                lines.append(f"- {fmt_dt(e['datetime'])} · {e['auditorium']}{suffix}{link}")
            lines.append("")
    if gone_events:
        lines.append(f"### Zmizelo z rozpisu ({len(gone_events)})\n")
        for cinema, group in group_by_cinema(gone_events):
            lines.append(f"**{cinema}**\n")
            for e in group:
                lines.append(f"- {fmt_dt(e['datetime'])} · {e['auditorium']}")
            lines.append("")
    film_link = next(
        (e["filmLink"] for e in list(new_events) + list(gone_events) if e.get("filmLink")),
        None,
    )
    if film_link:
        lines.append(f"[Stránka filmu na Cinema City]({film_link})")
    lines.append("")
    lines.append(
        f"<sub>Zkontrolováno {now():%d. %m. %Y %H:%M} · "
        f"film ~ `{FILM_PATTERN}` · sál ~ `{AUDITORIUM_PATTERN}`</sub>"
    )
    return "\n".join(lines)


def group_by_cinema(events):
    order = {}
    for e in sorted(events, key=lambda x: (x["cinema"], x["datetime"])):
        order.setdefault(e["cinema"], []).append(e)
    return order.items()


def title_for(new_events):
    film = new_events[0]["film"]
    days = sorted({e["datetime"][:10] for e in new_events})
    span = fmt_short(days[0])
    if len(days) > 1:
        span += f"–{fmt_short(days[-1])}"
    n = len(new_events)
    word = "nový termín" if n == 1 else ("nové termíny" if n < 5 else "nových termínů")
    return f"🎬 {film} v IMAXu: {n} {word} ({span})"


def title_for_freed(freed):
    n = len(freed)
    film = freed[0]["film"]
    if n == 1:
        e = freed[0]
        what = "znovu v prodeji" if e["was_sold_out"] else (
            "+1 místo" if e["gain"] == 1 else f"+{e['gain']} míst")
        return f"🎟️ {film}: {what} — {fmt_dt(e['datetime'])}"
    # "u" váže genitiv, takže vždy "projekcí"
    return f"🎟️ {film}: uvolněná místa u {n} projekcí"


def gh_output(**kwargs):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in kwargs.items():
            fh.write(f"{key}={value}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", default="state/seen.json", help="soubor se stavem")
    ap.add_argument("--seed", action="store_true", help="jen ulož stav, nic nehlas")
    ap.add_argument("--force-report", action="store_true", help="nahlas vše, i známé")
    ap.add_argument("--report", default="report.md", help="kam zapsat markdown hlášení")
    ap.add_argument("--title", default="title.txt", help="kam zapsat titulek issue")
    args = ap.parse_args()

    current = collect()
    state = load_state(args.state)
    known = state.get("events", {})

    print(f"Nalezeno {len(current)} hlídaných představení, ve stavu {len(known)}.")

    if args.seed:
        save_state(args.state, prune_past(current))
        print(f"Stav zapsán do {args.state} (seed, nic se nehlásí).")
        gh_output(has_news="false")
        return

    if args.force_report:
        new_events = sorted(current.values(), key=lambda e: e["datetime"])
        gone = []
        freed = []
    else:
        new_events = sorted(
            (v for k, v in current.items() if k not in known),
            key=lambda e: e["datetime"],
        )
        future = now().isoformat()
        gone = sorted(
            (v for k, v in known.items() if k not in current and v["datetime"] > future),
            key=lambda e: e["datetime"],
        )
        freed = [e for e in freed_seats(known, current) if e["datetime"] > future]

    save_state(args.state, prune_past(current))

    if not new_events and not gone and not freed:
        print("Nic nového.")
        gh_output(has_news="false")
        return

    body = render(new_events, gone, freed)
    if freed:
        title = title_for_freed(freed)
    elif new_events:
        title = title_for(new_events)
    else:
        title = "🎬 Odyssea v IMAXu: zrušené termíny"
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    with open(args.title, "w", encoding="utf-8") as fh:
        fh.write(title + "\n")

    print(f"\n{title}\n")
    print(body)
    gh_output(has_news="true")


if __name__ == "__main__":
    sys.exit(main())
