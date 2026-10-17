#!/usr/bin/env python3
"""Пошук цін туди-назад у Google Flights (через бібліотеку fast-flights).

Приклади:
  # конкретні дати
  python tools/gflights.py --from BCN,MAD --to PTY --pairs 2027-04-09:2027-04-23
  # сітка дат для вікна A (29.01.2027) або B (16.04.2027), крок 2 дні
  python tools/gflights.py --from BCN,MAD,LIS --to NRT,KIX --window A --step 2
  # довільний діапазон (OFF_WINDOW): виліт 2027-02-01..2027-03-31, 7-14 діб
  python tools/gflights.py --from BCN --to BOG --depart 2027-02-01:2027-03-31 --step 3

Виводить CSV у stdout (і JSON, якщо --json FILE). Маршрути з пересадкою або
вильотом у США / Великій Британії / Канаді позначаються banned=1 і за
замовчуванням не виводяться (див. --show-banned).

Обмеження: Google повертає 5–8 найкращих варіантів на запит; деталі видно
лише для рейсу туди, ціна — за обидва напрямки; чи входить ручна поклажа,
з результату не видно. Статус таких цін — INDICATIVE (джерело Google Flights).
"""
import argparse
import csv
import json
import random
import sys
import time
from datetime import date, timedelta

import airportsdata
from fast_flights import FlightQuery, Passengers, create_query
from fast_flights.parser import parse
from primp import Client

# Cookie згоди: без нього з європейських IP Google віддає сторінку consent.google.com.
CONSENT_COOKIES = {"SOCS": "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg", "CONSENT": "YES+cb"}
BANNED_COUNTRIES = {"US", "GB", "CA", "PR", "VI", "GU", "AS", "MP"}  # США з територіями, Британія, Канада
URL = "https://www.google.com/travel/flights"

WINDOWS = {
    # anchor, earliest departure, latest departure, earliest return, latest return
    "A": (date(2027, 1, 29), date(2027, 1, 17), date(2027, 1, 27), date(2027, 1, 31), date(2027, 2, 10)),
    "B": (date(2027, 4, 16), date(2027, 4, 4), date(2027, 4, 14), date(2027, 4, 18), date(2027, 4, 28)),
}

_AIRPORTS = airportsdata.load("IATA")


def country(code):
    a = _AIRPORTS.get(code)
    return a["country"] if a else "?"


def daterange(a, b, step):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=step)


def window_pairs(win, step, min_days, max_days):
    _, dep_a, dep_b, ret_a, ret_b = WINDOWS[win]
    for out in daterange(dep_a, dep_b, step):
        for back in daterange(ret_a, ret_b, step):
            if min_days <= (back - out).days <= max_days:
                yield out, back


def range_pairs(dep_a, dep_b, step, min_days, max_days):
    for out in daterange(dep_a, dep_b, step):
        for length in sorted({min_days, (min_days + max_days) // 2, max_days}):
            yield out, out + timedelta(days=length)


def make_client():
    c = Client(impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True)
    c.set_cookies("https://www.google.com", CONSENT_COOKIES)
    return c


def search(client, origin, dest, out, back, retries=2):
    q = create_query(
        flights=[FlightQuery(date=out.isoformat(), from_airport=origin, to_airport=dest),
                 FlightQuery(date=back.isoformat(), from_airport=dest, to_airport=origin)],
        trip="round-trip", seat="economy", passengers=Passengers(adults=1), language="en", currency="EUR",
    )
    last_err = None
    for attempt in range(retries + 1):
        try:
            res = client.get(URL, params=q.params())
            if res.status_code == 429:
                raise RuntimeError("HTTP 429 (rate limit)")
            return list(parse(res.text)), None
        except Exception as e:  # порожня сторінка, consent, rate limit, мережа
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(5 * (attempt + 1))
    return None, last_err


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="origins", required=True, help="IATA через кому, напр. BCN,MAD")
    ap.add_argument("--to", dest="dests", required=True, help="IATA через кому, напр. PTY,BOG")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--window", choices=sorted(WINDOWS), help="A = 29.01.2027, B = 16.04.2027")
    g.add_argument("--pairs", help="OUT:BACK[,OUT:BACK...] у форматі YYYY-MM-DD")
    g.add_argument("--depart", help="діапазон вильоту FROM:TO (YYYY-MM-DD) для OFF_WINDOW")
    ap.add_argument("--step", type=int, default=2, help="крок сітки дат у днях (за замовч. 2)")
    ap.add_argument("--min-days", type=int, default=7)
    ap.add_argument("--max-days", type=int, default=14)
    ap.add_argument("--max-price", type=float, default=None, help="виводити лише варіанти не дорожчі")
    ap.add_argument("--top", type=int, default=3, help="скільки найдешевших варіантів на запит виводити")
    ap.add_argument("--delay", type=float, default=2.0, help="пауза між запитами, с")
    ap.add_argument("--show-banned", action="store_true", help="виводити й маршрути через US/GB/CA")
    ap.add_argument("--json", help="зберегти всі результати в JSON-файл")
    args = ap.parse_args()

    if args.window:
        pairs = list(window_pairs(args.window, args.step, args.min_days, args.max_days))
    elif args.pairs:
        pairs = [tuple(date.fromisoformat(x) for x in p.split(":")) for p in args.pairs.split(",")]
    else:
        a, b = (date.fromisoformat(x) for x in args.depart.split(":"))
        pairs = list(range_pairs(a, b, args.step, args.min_days, args.max_days))

    origins = [x.strip().upper() for x in args.origins.split(",") if x.strip()]
    dests = [x.strip().upper() for x in args.dests.split(",") if x.strip()]
    total = len(origins) * len(dests) * len(pairs)
    print(f"# queries: {total}", file=sys.stderr)

    client = make_client()
    w = csv.writer(sys.stdout)
    w.writerow(["origin", "dest", "out", "back", "days", "price_eur", "airlines", "route_out", "stops_out",
                "banned", "found_utc"])
    rows, n, failed = [], 0, 0
    for o in origins:
        for d in dests:
            for out, back in pairs:
                n += 1
                results, err = search(client, o, d, out, back)
                stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if results is None:
                    failed += 1
                    print(f"# [{n}/{total}] {o}-{d} {out}..{back}: FAIL {err}", file=sys.stderr)
                    continue
                parsed = []
                for r in results:
                    codes = [r.flights[0].from_airport.code] + [s.to_airport.code for s in r.flights]
                    banned = any(country(c) in BANNED_COUNTRIES for c in codes)
                    parsed.append({
                        "origin": o, "dest": d, "out": out.isoformat(), "back": back.isoformat(),
                        "days": (back - out).days, "price_eur": r.price, "airlines": "+".join(r.airlines),
                        "route_out": "-".join(codes), "stops_out": len(r.flights) - 1,
                        "banned": int(banned), "found_utc": stamp,
                    })
                rows.extend(parsed)
                shown = [p for p in parsed if args.show_banned or not p["banned"]]
                if args.max_price is not None:
                    shown = [p for p in shown if p["price_eur"] <= args.max_price]
                for p in sorted(shown, key=lambda p: p["price_eur"])[: args.top]:
                    w.writerow(p.values())
                sys.stdout.flush()
                print(f"# [{n}/{total}] {o}-{d} {out}..{back}: {len(results)} results", file=sys.stderr)
                time.sleep(args.delay + random.uniform(0, 1))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"# done: {n - failed} ok, {failed} failed", file=sys.stderr)
    if failed == n and n:
        sys.exit(2)


if __name__ == "__main__":
    main()
