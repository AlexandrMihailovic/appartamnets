#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time

import db
from profiles import BBOX, PROFILES
from scrape_999 import build_polygon, fetch_page, make_session, parse_start_url

MIN_LIMIT = 100


def fetch_profile(session, profile: dict, limit: int, delay: float) -> list[dict]:
    category_id, _, filters = parse_start_url(profile["url"])
    polygon = build_polygon(BBOX)

    ads: list[dict] = []
    seen: set[str] = set()
    skip = 0
    total = None

    while True:
        payload = {
            "polygon": polygon,
            "skip": skip,
            "limit": limit,
            "categoryId": category_id,
            "subcategoryId": profile["subcategory_id"],
            "filters": filters,
        }
        try:
            scope = fetch_page(session, payload, retries=2)
        except RuntimeError as exc:
            if "ResourceExhausted" in str(exc) and limit > MIN_LIMIT:
                limit = max(MIN_LIMIT, limit // 2)
                print(f"[!] ответ слишком большой, уменьшаю страницу до {limit}", file=sys.stderr)
                continue
            raise

        total = scope["total"]
        page = scope["ads"] or []
        for ad in page:
            ad_id = str(ad.get("id") or "")
            if ad_id and ad_id not in seen:
                seen.add(ad_id)
                ads.append(ad)

        print(f"    skip={skip:5} получено {len(page):4} / total {total}", file=sys.stderr)
        skip += limit
        if not page or skip >= total:
            break
        time.sleep(delay)

    return ads


def money(feature) -> tuple[float | None, str]:
    if not feature:
        return None, ""
    value = feature.get("value")
    if isinstance(value, dict):
        amount = value.get("value")
        unit = (value.get("unit") or value.get("measurement") or "").replace("UNIT_", "")
        return (float(amount) if isinstance(amount, (int, float)) else None), unit
    return (float(value) if isinstance(value, (int, float)) else None), ""


def store(conn, profile_key: str, profile: dict, ads: list[dict]) -> dict:
    ts = db.now()
    known = {
        row["id"]: row
        for row in conn.execute("SELECT * FROM ads WHERE profile = ?", (profile_key,))
    }

    added, price_down, price_up, unchanged = [], [], [], 0
    fetched_ids = set()

    for ad in ads:
        ad_id = str(ad["id"])
        fetched_ids.add(ad_id)

        price, currency = money(ad.get("price"))
        old_price, old_currency = money(ad.get("oldPrice"))
        point = (ad.get("mapPoint") or {}).get("value") or {}
        street = (ad.get("street") or {}).get("value") or ""
        sub = ad.get("subCategory") or {}

        row = known.get(ad_id)
        if row is None:
            conn.execute(
                """INSERT INTO ads (id, profile, subcategory_id, subcategory, title, price, currency,
                                    old_price, old_currency, street, lat, lon,
                                    first_seen, last_seen, gone_at, first_price)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                (ad_id, profile_key, sub.get("id"), (sub.get("title") or {}).get("translated") or "",
                 ad.get("title") or "", price, currency, old_price, old_currency, street,
                 point.get("lat"), point.get("lon"), ts, ts, price),
            )
            conn.execute(
                "INSERT OR IGNORE INTO price_history (ad_id, ts, price, currency) VALUES (?,?,?,?)",
                (ad_id, ts, price, currency),
            )
            added.append({"id": ad_id, "title": ad.get("title") or "", "price": price, "currency": currency})
            continue

        changed = price is not None and row["price"] is not None and (
            abs(price - row["price"]) > 0.5 or currency != (row["currency"] or "")
        )
        if changed:
            conn.execute(
                """UPDATE ads SET price = ?, currency = ?, prev_price = ?, price_changed_at = ?,
                                  old_price = ?, old_currency = ?, title = ?, street = ?,
                                  lat = ?, lon = ?, last_seen = ?, gone_at = NULL
                   WHERE id = ?""",
                (price, currency, row["price"], ts, old_price, old_currency, ad.get("title") or "",
                 street, point.get("lat"), point.get("lon"), ts, ad_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO price_history (ad_id, ts, price, currency) VALUES (?,?,?,?)",
                (ad_id, ts, price, currency),
            )
            item = {"id": ad_id, "title": ad.get("title") or "", "was": row["price"],
                    "now": price, "currency": currency}
            (price_down if price < row["price"] else price_up).append(item)
        else:
            conn.execute(
                """UPDATE ads SET last_seen = ?, gone_at = NULL, title = ?, street = ?,
                                  lat = ?, lon = ?, old_price = ?, old_currency = ?
                   WHERE id = ?""",
                (ts, ad.get("title") or "", street, point.get("lat"), point.get("lon"),
                 old_price, old_currency, ad_id),
            )
            unchanged += 1

    gone = [ad_id for ad_id, row in known.items() if ad_id not in fetched_ids and row["gone_at"] is None]
    for ad_id in gone:
        conn.execute("UPDATE ads SET gone_at = ? WHERE id = ?", (ts, ad_id))

    return {"added": added, "gone": gone, "price_down": price_down,
            "price_up": price_up, "unchanged": unchanged}


def report(profile: dict, result: dict, total: int, fetched: int) -> None:
    print(f"\n=== {profile['title']}: в выдаче {total}, обработано {fetched}")
    print(f"    новых: {len(result['added'])}   ушли: {len(result['gone'])}   "
          f"подешевели: {len(result['price_down'])}   подорожали: {len(result['price_up'])}   "
          f"без изменений: {result['unchanged']}")

    for item in result["added"][:10]:
        price = f"{item['price']:.0f} {item['currency']}" if item["price"] else "—"
        print(f"    + {item['id']}  {price:>14}  {item['title'][:60]}")
    if len(result["added"]) > 10:
        print(f"      … ещё {len(result['added']) - 10}")

    for item in sorted(result["price_down"], key=lambda x: (x["now"] - x["was"]))[:10]:
        delta = item["now"] - item["was"]
        pct = delta / item["was"] * 100 if item["was"] else 0
        print(f"    ↓ {item['id']}  {item['was']:.0f} → {item['now']:.0f} {item['currency']} "
              f"({delta:+.0f}, {pct:+.1f}%)  {item['title'][:45]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Сбор объявлений 999.md в базу")
    parser.add_argument("profiles", nargs="*", default=None,
                        help=f"какие профили собирать: {', '.join(PROFILES)} (по умолчанию все)")
    parser.add_argument("--limit", type=int, default=None, help="размер страницы")
    parser.add_argument("--delay", type=float, default=0.5, help="пауза между страницами, сек")
    args = parser.parse_args()

    keys = args.profiles or list(PROFILES)
    unknown = [k for k in keys if k not in PROFILES]
    if unknown:
        parser.error(f"неизвестные профили: {', '.join(unknown)}. Доступны: {', '.join(PROFILES)}")

    conn = db.connect()
    for key in keys:
        profile = PROFILES[key]
        started = db.now()
        print(f"\n[i] {profile['title']} — {profile['url']}", file=sys.stderr)

        session = make_session(profile["url"])
        limit = args.limit or profile.get("limit", 800)
        ads = fetch_profile(session, profile, limit, args.delay)

        result = store(conn, key, profile, ads)
        conn.execute(
            """INSERT INTO runs (profile, started, finished, total, fetched, added, gone, price_down, price_up)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (key, started, db.now(), len(ads), len(ads), len(result["added"]), len(result["gone"]),
             len(result["price_down"]), len(result["price_up"])),
        )
        conn.commit()
        report(profile, result, len(ads), len(ads))

    print()
    for key, value in db.stats(conn).items():
        print(f"  {key}: {value}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
