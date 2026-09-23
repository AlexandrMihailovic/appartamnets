#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import db
from scrape_ad import fetch, make_session, parse

_local = threading.local()

TERMS_MARKERS = (
    "рассроч", "в рассрочку", "аванс", "первоначальный взнос", "первый взнос",
    "белый вариант", "серый вариант", "вариант алб", "черновая", "без ремонта",
    "под ремонт", "в стадии строительства", "сдача дома", "строится",
    " in rate", " în rate", "rate fara", "rate fără", "avans",
    "varianta alba", "varianta albă", "varianta gri", "varianta sura", "varianta sură",
    "la rosu", "la roșu", "la gri", "in constructie", "în construcție",
    "se construieste", "se construiește", "exploatare",
    "bloc nou in constructie", "сдача в", "сдача дома", "готовность",
)


def has_terms(text: str) -> int:
    low = (text or "").lower()
    return int(any(marker in low for marker in TERMS_MARKERS))


def session_for_thread():
    if not hasattr(_local, "session"):
        _local.session = make_session("https://999.md/ru/")
    return _local.session


def to_int(value):
    if value is None or value == "":
        return None
    m = re.search(r"-?\d+", str(value))
    return int(m.group()) if m else None


def to_float(value):
    if value is None or value == "":
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    return float(m.group().replace(",", ".")) if m else None


def rooms_number(text: str):
    if not text:
        return None
    if "туди" in text:
        return 0
    return to_int(text)


def row_from(ad: dict) -> tuple:
    owner = ad["owner"]
    return (
        ad["id"], db.now(), ad["state"], ad["posted"], ad["reseted"], ad["expire"],
        ad["views_total"], ad["views_today"],
        ad["offer_type"], ad["author_type"],
        ad["rooms"], rooms_number(ad["rooms"]),
        to_float(ad["area"]), ad["area_unit"],
        to_int(ad["floor"]), to_int(ad["floors_total"]),
        ad["housing_stock"], ad["living_room"], ad["developer"],
        to_float(ad["price_per_m2"]),
        ad["region"], ad["city"], ad["district"], ad["street"], ad["house"],
        owner["login"], owner["id"], owner["registered"],
        int(bool(owner["is_business"])), int(bool(owner["is_verified"])),
        ", ".join(ad["phones"]),
        json.dumps(ad["photos"], ensure_ascii=False), len(ad["photos"]),
        ad["description"],
        json.dumps(ad["features"], ensure_ascii=False),
        has_terms(ad["description"] + " " + ad["title"]),
    )


INSERT = """INSERT INTO ad_details (
    id, fetched_at, state, posted, reseted, expire, views_total, views_today,
    offer_type, author_type, rooms, rooms_n, area, area_unit, floor, floors_total,
    housing_stock, living_room, developer, price_per_m2,
    region, city, district, street, house,
    owner_login, owner_id, owner_registered, owner_is_business, owner_verified,
    phones, photos, photos_count, description, features_json, terms_flag
) VALUES (""" + ",".join("?" * 36) + """)
ON CONFLICT(id) DO UPDATE SET
    fetched_at=excluded.fetched_at, state=excluded.state, posted=excluded.posted,
    reseted=excluded.reseted, expire=excluded.expire, views_total=excluded.views_total,
    views_today=excluded.views_today, offer_type=excluded.offer_type,
    author_type=excluded.author_type, rooms=excluded.rooms, rooms_n=excluded.rooms_n,
    area=excluded.area, area_unit=excluded.area_unit, floor=excluded.floor,
    floors_total=excluded.floors_total, housing_stock=excluded.housing_stock,
    living_room=excluded.living_room, developer=excluded.developer,
    price_per_m2=excluded.price_per_m2, region=excluded.region, city=excluded.city,
    district=excluded.district, street=excluded.street, house=excluded.house,
    owner_login=excluded.owner_login, owner_id=excluded.owner_id,
    owner_registered=excluded.owner_registered, owner_is_business=excluded.owner_is_business,
    owner_verified=excluded.owner_verified, phones=excluded.phones, photos=excluded.photos,
    photos_count=excluded.photos_count, description=excluded.description,
    features_json=excluded.features_json, terms_flag=excluded.terms_flag"""


def pick(conn, args) -> list[str]:
    where = ["a.gone_at IS NULL"]
    params: list = []
    if args.profile:
        where.append("a.profile = ?")
        params.append(args.profile)

    if args.all:
        join = "LEFT JOIN ad_details d ON d.id = a.id"
    elif args.changed:
        join = "JOIN ad_details d ON d.id = a.id"
        where.append("a.price_changed_at IS NOT NULL AND a.price_changed_at > d.fetched_at")
    elif args.stale:
        join = "LEFT JOIN ad_details d ON d.id = a.id"
        where.append("(d.id IS NULL OR d.fetched_at < datetime('now', ?))")
        params.append(f"-{int(args.stale)} days")
    else:
        join = "LEFT JOIN ad_details d ON d.id = a.id"
        where.append("d.id IS NULL")

    sql = f"SELECT a.id FROM ads a {join} WHERE {' AND '.join(where)} ORDER BY a.first_seen DESC"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    return [row["id"] for row in conn.execute(sql, params)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Сбор карточек объявлений в базу")
    parser.add_argument("--profile", default=None, help="apartments | garages")
    parser.add_argument("--all", action="store_true", help="перечитать вообще все карточки")
    parser.add_argument("--changed", action="store_true", help="только те, где поменялась цена")
    parser.add_argument("--stale", type=int, default=None, help="обновить карточки старше N дней")
    parser.add_argument("--limit", type=int, default=None, help="не больше N штук за прогон")
    parser.add_argument("--workers", type=int, default=6, help="потоков (по умолчанию 6)")
    args = parser.parse_args()

    conn = db.connect()
    ids = pick(conn, args)
    if not ids:
        print("[✓] нечего добирать — все карточки на месте")
        return 0

    print(f"[i] к сбору: {len(ids)} карточек, потоков: {args.workers}", file=sys.stderr)
    started = time.time()
    done = failed = 0

    def work(ad_id: str):
        try:
            return ad_id, parse(fetch(ad_id, session_for_thread()), ad_id), None
        except SystemExit as exc:
            return ad_id, None, str(exc)
        except Exception as exc:
            return ad_id, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, ad_id) for ad_id in ids]
        for future in as_completed(futures):
            ad_id, ad, error = future.result()
            if ad is None:
                failed += 1
                if failed <= 10:
                    print(f"[!] {ad_id}: {error}", file=sys.stderr)
            else:
                conn.execute(INSERT, row_from(ad))
                done += 1
                if done % 100 == 0:
                    conn.commit()
                    speed = done / max(time.time() - started, 0.1)
                    left = (len(ids) - done - failed) / max(speed, 0.01)
                    print(f"    {done}/{len(ids)}  ({speed:.1f}/сек, осталось ~{left/60:.0f} мин)",
                          file=sys.stderr)
    conn.commit()

    print(f"\n[✓] собрано {done}, пропущено {failed}, за {(time.time()-started)/60:.1f} мин")
    for key, value in db.stats(conn).items():
        print(f"  {key}: {value}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
