#!/usr/bin/env python3
"""Пошук цін туди-назад у Google Flights (через бібліотеку fast-flights).

Приклади:
  # конкретні дати
  python tools/gflights.py --from BCN,MAD --to PTY --pairs 2027-04-09:2027-04-23
  # сітка дат для вікна A (29.01.2027) або B (16.04.2027), крок 2 дні
  python tools/gflights.py --from BCN,MAD,LIS --to NRT,KIX --window A --step 2
  # довільний діапазон (OFF_WINDOW): виліт 2027-02-01..2027-03-31, 7-14 діб
  python tools/gflights.py --from BCN --to BOG --depart 2027-02-01:2027-03-31 --step 3
  # зупинка в Стамбулі на 1–3 дні по дорозі туди (три квитки в один бік)
  python tools/gflights.py --from BCN --to MCT --window A --step 3 --via IST --stop-days 1,2,3
  # open-jaw: прилітаємо в Боготу, повертаємося з Картахени або Медельїна
  python tools/gflights.py --from BCN,MAD --to BOG --return-from CTG,MDE --window B --step 3
  # два квитки в один бік (видно маршрут повернення)
  python tools/gflights.py --from BCN --to SID --oneway --pairs 2027-01-21:2027-02-04

Виводить CSV у stdout (і JSON, якщо --json FILE). Маршрути з пересадкою або
вильотом у США / Великій Британії / Канаді позначаються banned=1 і за
замовчуванням не виводяться (див. --show-banned).

Не запускайте більше 2 копій скрипта одночасно: Google відповідає 429.

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
from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query
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


def search(client, origin, dest, out, back=None, retries=2):
    """Туди-назад, або в один бік, якщо back=None."""
    legs = [FlightQuery(date=out.isoformat(), from_airport=origin, to_airport=dest)]
    if back is not None:
        legs.append(FlightQuery(date=back.isoformat(), from_airport=dest, to_airport=origin))
    q = create_query(
        flights=legs, trip="round-trip" if back is not None else "one-way",
        seat="economy", passengers=Passengers(adults=1), language="en", currency="EUR",
    )
    last_err = None
    for attempt in range(retries + 1):
        try:
            res = client.get(URL, params=q.params())
            if res.status_code == 429:
                raise RuntimeError("HTTP 429 (rate limit)")
            return list(parse(res.text)), None
        except FlightsNotFound:  # на ці дати рейсів немає — це не помилка
            return [], None
        except (TypeError, IndexError) as e:  # парсер fast-flights не розібрав сторінку; повтор не допомагає
            return None, f"PARSE {type(e).__name__}: {e}"
        except Exception as e:  # порожня сторінка, consent, rate limit, мережа
            last_err = f"{type(e).__name__}: {e}"
            time.sleep((20 if "429" in last_err else 5) * (attempt + 1))
    return None, last_err


def route_codes(r):
    return [r.flights[0].from_airport.code] + [s.to_airport.code for s in r.flights]


def cheapest_allowed(results):
    """Найдешевший варіант без пересадок у US/GB/CA або None."""
    ok = [r for r in results or [] if not any(country(c) in BANNED_COUNTRIES for c in route_codes(r))]
    return min(ok, key=lambda r: r.price) if ok else None


class OneWayCache:
    """Кешує запити в один бік: у режимі --via однакові відрізки повторюються."""

    def __init__(self, client, delay):
        self.client, self.delay, self.memo = client, delay, {}
        self.ok = self.failed = 0

    def get(self, o, d, day):
        key = (o, d, day)
        if key not in self.memo:
            results, err = search(self.client, o, d, day)
            if results is None:
                self.failed += 1
                print(f"# {o}-{d} {day}: FAIL {err}", file=sys.stderr)
            else:
                self.ok += 1
            self.memo[key] = cheapest_allowed(results)
            time.sleep(self.delay + random.uniform(0, 1))
        return self.memo[key]


def run_legs(args, origins, dests, pairs, w):
    """Маршрут з окремих квитків в один бік; ціна — їх сума.
    Туди: ORIGIN→DEST, або з --via: ORIGIN→HUB, зупинка 1–3 дні, HUB→DEST.
    Назад: з DEST або з кожного міста --return-from (open-jaw) → ORIGIN, будь-яким маршрутом."""
    hub = args.via.upper() if args.via else None
    stop_days = [int(x) for x in args.stop_days.split(",")] if hub else [0]
    cache = OneWayCache(make_client(), args.delay)
    rows = []
    for o in origins:
        for d in dests:
            back_from = [x.strip().upper() for x in args.return_from.split(",")] if args.return_from else [d]
            for out, back in pairs:
                if hub:
                    leg1 = cache.get(o, hub, out)
                    if leg1 is None:
                        continue
                    best = None
                    for s in stop_days:
                        cand = cache.get(hub, d, out + timedelta(days=s))
                        if cand is not None and (best is None or cand.price < best[1].price):
                            best = (s, cand)
                    if best is None:
                        continue
                    s, leg2 = best
                    outbound = [leg1, leg2]
                    out_desc = (f"{'-'.join(route_codes(leg1))} [{leg1.price}] / stop {s}d in {hub} / "
                                f"{'-'.join(route_codes(leg2))} [{leg2.price}]")
                else:
                    leg = cache.get(o, d, out)
                    if leg is None:
                        continue
                    outbound = [leg]
                    out_desc = f"{'-'.join(route_codes(leg))} [{leg.price}]"
                for r in back_from:
                    ret = cache.get(r, o, back)
                    if ret is None:
                        continue
                    legs = outbound + [ret]
                    total = sum(x.price for x in legs)
                    row = {
                        "origin": o, "dest": d if r == d else f"{d}>{r}", "out": out.isoformat(),
                        "back": back.isoformat(), "days": (back - out).days, "price_eur": total,
                        "airlines": " | ".join("+".join(x.airlines) for x in legs),
                        "route_out": f"{out_desc} / back {'-'.join(route_codes(ret))} [{ret.price}]",
                        "stops_out": f"via {hub}" if hub else ("open-jaw" if r != d else "2x one-way"),
                        "banned": 0, "found_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    rows.append(row)
                    if args.max_price is None or total <= args.max_price:
                        w.writerow(row.values())
                        sys.stdout.flush()
                    print(f"# {o}-{d}>{r} {out}..{back}: {total} EUR", file=sys.stderr)
    print(f"# done: {cache.ok} ok, {cache.failed} failed (one-way queries)", file=sys.stderr)
    return rows, cache.ok, cache.failed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="origins", required=True, help="IATA через кому, напр. BCN,MAD")
    ap.add_argument("--to", dest="dests", required=True, help="IATA через кому, напр. PTY,BOG")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--window", choices=sorted(WINDOWS), help="A = 29.01.2027, B = 16.04.2027")
    g.add_argument("--pairs", help="OUT:BACK[,OUT:BACK...] у форматі YYYY-MM-DD")
    g.add_argument("--depart", help="діапазон вильоту FROM:TO (YYYY-MM-DD) для OFF_WINDOW")
    ap.add_argument("--step", type=int, default=2, help="крок сітки дат у днях (за замовч. 2)")
    ap.add_argument("--min-days", type=int, default=10)
    ap.add_argument("--max-days", type=int, default=14)
    ap.add_argument("--via", help="хаб для зупинки (напр. IST): три квитки в один бік, ціна — сума")
    ap.add_argument("--stop-days", default="1,2,3", help="скільки днів зупинки в хабі перебирати (для --via)")
    ap.add_argument("--return-from", help="open-jaw: повернення з цих міст (IATA через кому) замість --to")
    ap.add_argument("--oneway", action="store_true", help="два квитки в один бік замість туди-назад")
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
    w = csv.writer(sys.stdout)
    w.writerow(["origin", "dest", "out", "back", "days", "price_eur", "airlines", "route_out", "stops_out",
                "banned", "found_utc"])

    if args.via or args.return_from or args.oneway:
        print(f"# itineraries: {total} (one-way queries, cached)", file=sys.stderr)
        rows, ok, failed = run_legs(args, origins, dests, pairs, w)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=1)
        sys.exit(2 if ok == 0 and failed else 0)

    print(f"# queries: {total}", file=sys.stderr)
    client = make_client()
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
                    codes = route_codes(r)
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
