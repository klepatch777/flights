#!/usr/bin/env python3
"""Аналіз історії цін з FLIGHT_DEALS_2027/GF_RUNS/*.csv.

  # падіння цін: остання дата запуску проти мінімуму попередніх 7 днів
  python tools/price_trends.py drops --threshold 15
  # тижневий звіт (Markdown) — найкраща ціна по кожному напрямку й вікну за 7 днів
  python tools/price_trends.py weekly > FLIGHT_DEALS_2027/WEEKLY.md

Порівнюється мінімальна ціна за напрямок (країна призначення) і вікно дат (A 29.01 / B 16.04 /
OFF) серед усіх аеропортів вильоту й дат запуску. Рядки banned=1 ігноруються.
"""
import argparse
import csv
import glob
import os
import sys
from datetime import date, timedelta

import airportsdata

sys.path.insert(0, os.path.dirname(__file__))
from gflights import WINDOWS  # noqa: E402

RUNS = os.path.join(os.path.dirname(__file__), "..", "FLIGHT_DEALS_2027", "GF_RUNS")
_AIRPORTS = airportsdata.load("IATA")

# Рейси до цих країн — доїзд до хаба (positioning), а не напрямок подорожі.
EUROPE = {"ES", "PT", "FR", "IT", "DE", "NL", "BE", "CH", "AT", "IE", "PL", "HU", "CZ", "DK", "NO", "SE", "FI",
          "GR", "TR", "GB", "LU", "MT", "HR", "RO", "BG"}
NAMES = {"CV": "Кабо-Верде", "ZA": "ПАР", "CO": "Колумбія", "EC": "Еквадор", "MX": "Мексика", "GT": "Гватемала",
         "OM": "Оман", "VN": "В'єтнам", "TH": "Таїланд", "JP": "Японія", "PA": "Панама", "DO": "Домінікана",
         "JM": "Ямайка", "AR": "Аргентина", "PE": "Перу", "BO": "Болівія", "CL": "Чилі", "KR": "Південна Корея",
         "CN": "Китай", "HK": "Гонконг", "TW": "Тайвань", "MY": "Малайзія", "SG": "Сінгапур", "ID": "Індонезія",
         "PH": "Філіппіни", "KH": "Камбоджа", "LA": "Лаос", "IN": "Індія", "LK": "Шрі-Ланка", "NP": "Непал",
         "UZ": "Узбекистан", "KZ": "Казахстан", "AE": "ОАЕ", "QA": "Катар", "BR": "Бразилія", "CR": "Коста-Рика"}


def window_of(out, back):
    for name, (anchor, dep_a, dep_b, ret_a, ret_b) in WINDOWS.items():
        if dep_a <= out <= dep_b and ret_a <= back <= ret_b:
            return name
    return "OFF"


def country_of(dest):
    code = dest.split(">")[0]
    a = _AIRPORTS.get(code)
    return a["country"] if a else code


def load():
    """{run_date: [row, ...]}"""
    runs = {}
    for path in sorted(glob.glob(os.path.join(RUNS, "*.csv"))):
        try:
            run_day = date.fromisoformat(os.path.basename(path)[:10])
        except ValueError:
            continue
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("banned") == "1" or r.get("origin") == "origin":
                    continue
                try:
                    r["price"] = float(r["price_eur"])
                    r["window"] = window_of(date.fromisoformat(r["out"]), date.fromisoformat(r["back"]))
                except (KeyError, ValueError):
                    continue
                cc = country_of(r["dest"])
                if cc in EUROPE:
                    continue
                r["country"] = NAMES.get(cc, cc)
                runs.setdefault(run_day, []).append(r)
    return runs


def best_by_key(rows):
    best = {}
    for r in rows:
        k = (r["country"], r["window"])
        if k not in best or r["price"] < best[k]["price"]:
            best[k] = r
    return best


def fmt(r):
    return (f"{r['origin']}→{r['dest']} {r['out']}…{r['back']} ({r['days']} дн.), "
            f"{r['airlines']}, {r['price']:.0f} EUR")


def cmd_drops(runs, threshold):
    if len(runs) < 2:
        print("# замало історії для порівняння", file=sys.stderr)
        return
    last = max(runs)
    prev = [r for d, rows in runs.items() if last - timedelta(days=7) <= d < last for r in rows]
    if not prev:
        print("# немає запусків за попередні 7 днів", file=sys.stderr)
        return
    now, before = best_by_key(runs[last]), best_by_key(prev)
    found = False
    for k, r in sorted(now.items()):
        if k in before:
            old = before[k]["price"]
            drop = (old - r["price"]) / old * 100
            if drop >= threshold:
                found = True
                print(f"- {k[0]} (вікно {k[1]}): {old:.0f} → {r['price']:.0f} EUR (−{drop:.0f}%). {fmt(r)}")
    if not found:
        print(f"# падінь ≥{threshold}% немає", file=sys.stderr)


def cmd_weekly(runs):
    if not runs:
        print("# немає даних", file=sys.stderr)
        return
    last = max(runs)
    week = [r for d, rows in runs.items() if last - timedelta(days=6) <= d <= last for r in rows]
    prev = [r for d, rows in runs.items() if last - timedelta(days=13) <= d < last - timedelta(days=6) for r in rows]
    now, before = best_by_key(week), best_by_key(prev)
    print(f"# Тижневий звіт цін — тиждень до {last.isoformat()}\n")
    print("Найкраща ціна туди-назад (Google Flights, без доплати за ручну поклажу в лоукостів і без доїзду до "
          "аеропорту вильоту) по кожній країні й вікну дат.\n")
    for win, title in (("A", "Вікно A — 29.01.2027"), ("B", "Вікно B — 16.04.2027"), ("OFF", "Інші дати")):
        items = sorted(((k, r) for k, r in now.items() if k[1] == win), key=lambda kr: kr[1]["price"])
        if not items:
            continue
        print(f"## {title}\n")
        print("| Країна | Ціна EUR | Зміна за тиждень | Маршрут |")
        print("|---|---|---|---|")
        for k, r in items:
            ch = ""
            if k in before:
                diff = r["price"] - before[k]["price"]
                ch = f"{diff:+.0f}"
            print(f"| {k[0]} | {r['price']:.0f} | {ch} | {fmt(r)} |")
        print()
    print(f"Запусків за тиждень: {sum(1 for d in runs if last - timedelta(days=6) <= d <= last)}.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("drops")
    d.add_argument("--threshold", type=float, default=15.0, help="мінімальне падіння, %")
    sub.add_parser("weekly")
    args = ap.parse_args()
    runs = load()
    if args.cmd == "drops":
        cmd_drops(runs, args.threshold)
    else:
        cmd_weekly(runs)


if __name__ == "__main__":
    main()
