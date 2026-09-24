#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import time
import sqlite3
import threading
import traceback
import urllib.request
import webbrowser
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import db
from profiles import PROFILES

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "static", "index.html")
FAVICON = os.path.join(HERE, "static", "favicon.ico")
OG_IMAGE = os.path.join(HERE, "static", "og.png")
ROBOTS = "User-agent: *\nAllow: /\n\n"

PPM = "COALESCE(d.price_per_m2, CASE WHEN d.area > 0 THEN a.price / d.area END)"
DROP_ABS = "CASE WHEN a.prev_price IS NOT NULL THEN a.price - a.prev_price END"
DROP_PCT = "CASE WHEN a.prev_price > 0 THEN (a.price - a.prev_price) * 100.0 / a.prev_price END"

SORTS = {
    "posted": "d.posted",
    "first_seen": "a.first_seen",
    "price": "a.price",
    "ppm": PPM,
    "area": "d.area",
    "drop_abs": DROP_ABS,
    "drop_pct": DROP_PCT,
    "views": "d.views_total",
    "changed": "a.price_changed_at",
    "discount": f"(b.med_ppm - {PPM}) * 100.0 / b.med_ppm",
}

SELECT_COLS = f"""
SELECT a.id, a.profile, a.title, a.price, a.currency, a.old_price, a.old_currency,
       a.prev_price, a.price_changed_at, a.first_seen, a.last_seen, a.gone_at, a.first_price,
       a.lat, a.lon,
       d.posted, d.reseted, d.expire, d.views_total, d.views_today,
       d.rooms, d.rooms_n, d.area, d.floor, d.floors_total, d.housing_stock,
       d.district, d.street, d.house, d.developer, d.author_type,
       d.owner_login, d.owner_is_business, d.phones, d.photos, d.photos_count,
       COALESCE(d.terms_flag, 0) AS terms_flag,
       {PPM}      AS ppm,
       {DROP_ABS} AS drop_abs,
       {DROP_PCT} AS drop_pct,
       COALESCE(f.favorite, 0) AS favorite,
       COALESCE(f.hidden, 0)   AS hidden,
       COALESCE(f.note, '')    AS note,
       (SELECT COUNT(*) FROM price_history h WHERE h.ad_id = a.id) AS hist_points"""

MAP_COLS = f"""
SELECT a.id, a.lat, a.lon, a.price, a.currency,
       d.rooms_n, d.area, d.floor, d.floors_total, d.district, d.street, d.house,
       d.posted, d.housing_stock, d.owner_is_business,
       json_extract(d.photos, '$[0]') AS photo,
       {PPM}      AS ppm,
       {DROP_PCT} AS drop_pct,
       COALESCE(f.favorite, 0) AS favorite"""


FROM_JOINS = """
FROM ads a
LEFT JOIN ad_details d ON d.id = a.id
LEFT JOIN flags f      ON f.ad_id = a.id"""

SELECT = SELECT_COLS + FROM_JOINS

DEALS_RULES = """
    d.city = 'Кишинёв'
    AND d.floor > 1
    AND d.floors_total IS NOT NULL AND d.floor < d.floors_total
    AND d.area >= :min_area AND d.area <= 400
    AND d.rooms_n IS NOT NULL
    AND d.housing_stock <> ''
    AND d.area / MAX(d.rooms_n, 1) <= 100
    AND a.price >= 15000
    AND {ppm} BETWEEN 300 AND 12000
""".replace("{ppm}", PPM)

BENCH_CTE = f"""
WITH pool AS (
    SELECT d.district AS district, d.rooms_n AS rooms_n, d.housing_stock AS stock, {PPM} AS ppm
    FROM ads a
    JOIN ad_details d ON d.id = a.id
    WHERE a.profile = 'apartments' AND a.gone_at IS NULL
      AND d.city = 'Кишинёв' AND d.area >= ? AND d.area <= 400
      AND a.price >= 15000 AND {PPM} BETWEEN 300 AND 12000
      AND d.rooms_n IS NOT NULL AND d.housing_stock <> ''
),
ranked AS (
    SELECT district, rooms_n, stock, ppm,
           ROW_NUMBER() OVER (PARTITION BY district, rooms_n, stock ORDER BY ppm) AS rn,
           COUNT(*)     OVER (PARTITION BY district, rooms_n, stock)              AS cnt
    FROM pool
),
bench AS (
    SELECT district, rooms_n, stock, AVG(ppm) AS med_ppm, MAX(cnt) AS pool_n
    FROM ranked
    WHERE rn IN ((cnt + 1) / 2, (cnt + 2) / 2)
    GROUP BY district, rooms_n, stock
)
"""

DEALS_COLS = f"""
    , b.med_ppm AS med_ppm, b.pool_n AS pool_n,
      (b.med_ppm - {PPM}) * 100.0 / b.med_ppm AS discount"""

DEALS_JOIN = ("\nLEFT JOIN bench b ON b.district = d.district AND b.rooms_n = d.rooms_n"
              "\n                          AND b.stock = d.housing_stock")


def one(params, key, default=None):
    value = params.get(key, [None])[0]
    return default if value in (None, "") else value


def num(params, key):
    value = one(params, key)
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def many(params, key) -> list[str]:
    raw = one(params, key)
    return [x for x in raw.split(",") if x] if raw else []


def baseline(conn, profile: str) -> str:
    row = conn.execute("SELECT MIN(finished) m FROM runs WHERE profile = ?", (profile,)).fetchone()
    return (row["m"] if row else None) or "0000"


def build_where(conn, params) -> tuple[str, list, str]:
    profile = one(params, "profile", "apartments")
    where = ["a.profile = ?"]
    args: list = [profile]

    gone = one(params, "gone")
    if gone == "1":
        where.append("a.gone_at IS NOT NULL")
    elif gone != "both":
        where.append("a.gone_at IS NULL")

    tab = one(params, "tab", "all")
    base = baseline(conn, profile)
    if tab == "new":
        if one(params, "new_by") == "base":
            where.append("a.first_seen > ?")
            args.append(base)
        else:
            days = num(params, "new_days") or 7
            where.append("d.posted IS NOT NULL AND d.posted > datetime('now', ?)")
            args.append(f"-{int(days)} days")
    elif tab == "down":
        where.append(f"({DROP_ABS} < 0 OR (a.old_price IS NOT NULL AND a.old_price > a.price))")
    elif tab == "fav":
        where.append("COALESCE(f.favorite, 0) = 1")
    elif tab == "deals":
        min_area = num(params, "min_area")
        min_area = 30 if min_area is None else min_area
        where.append(DEALS_RULES.replace(":min_area", str(float(min_area))))
        where.append("b.pool_n >= ?")
        args.append(int(num(params, "pool_min") or 8))
        where.append(f"(b.med_ppm - {PPM}) * 100.0 / b.med_ppm >= ?")
        args.append(num(params, "discount_min") if num(params, "discount_min") is not None else 15)
        where.append(f"(b.med_ppm - {PPM}) * 100.0 / b.med_ppm <= ?")
        args.append(num(params, "discount_max") if num(params, "discount_max") is not None else 60)
        if one(params, "show_terms") != "1":
            where.append("COALESCE(d.terms_flag, 0) = 0")

    if one(params, "show_hidden") != "1" and tab != "hidden":
        where.append("COALESCE(f.hidden, 0) = 0")
    if tab == "hidden":
        where.append("COALESCE(f.hidden, 0) = 1")

    ranges = [
        ("price_min", "a.price >= ?"), ("price_max", "a.price <= ?"),
        ("ppm_min", f"{PPM} >= ?"), ("ppm_max", f"{PPM} <= ?"),
        ("area_min", "d.area >= ?"), ("area_max", "d.area <= ?"),
        ("floor_min", "d.floor >= ?"), ("floor_max", "d.floor <= ?"),
        ("views_min", "d.views_total >= ?"),
        ("photos_min", "d.photos_count >= ?"),
    ]
    for key, clause in ranges:
        value = num(params, key)
        if value is not None:
            where.append(clause)
            args.append(value)

    rooms = many(params, "rooms")
    if rooms:
        if "5" in rooms:
            others = [r for r in rooms if r != "5"]
            parts = ["d.rooms_n >= 5"]
            if others:
                parts.append(f"d.rooms_n IN ({','.join('?' * len(others))})")
                args.extend(int(r) for r in others)
            where.append("(" + " OR ".join(parts) + ")")
        else:
            where.append(f"d.rooms_n IN ({','.join('?' * len(rooms))})")
            args.extend(int(r) for r in rooms)

    for key, column in (("districts", "d.district"), ("housing", "d.housing_stock"),
                        ("developers", "d.developer")):
        values = many(params, key)
        if values:
            where.append(f"{column} IN ({','.join('?' * len(values))})")
            args.extend(values)

    author = one(params, "author")
    if author == "private":
        where.append("COALESCE(d.owner_is_business, 0) = 0 AND COALESCE(d.author_type,'') NOT LIKE '%генст%'")
    elif author == "business":
        where.append("(COALESCE(d.owner_is_business, 0) = 1 OR COALESCE(d.author_type,'') LIKE '%генст%')")

    if one(params, "not_first") == "1":
        where.append("d.floor > 1")
    if one(params, "not_last") == "1":
        where.append("(d.floors_total IS NULL OR d.floor < d.floors_total)")
    if one(params, "has_photo") == "1":
        where.append("COALESCE(d.photos_count, 0) > 0")

    age = num(params, "age_max")
    if age is not None:
        where.append("d.posted IS NOT NULL AND d.posted > datetime('now', ?)")
        args.append(f"-{int(age)} days")

    query = one(params, "q")
    if query:
        where.append("(a.title LIKE ? OR d.description LIKE ? OR d.street LIKE ? OR a.id LIKE ?)")
        args.extend([f"%{query}%"] * 4)

    return " AND ".join(where), args, base


def api_ads(conn, params) -> dict:
    deals = one(params, "tab") == "deals"
    where, args, base = build_where(conn, params)

    default_sort = "discount" if deals else "posted"
    sort_key = one(params, "sort", default_sort)
    if sort_key == "discount" and not deals:
        sort_key = "posted"
    sort = SORTS.get(sort_key, "d.posted")
    direction = "ASC" if one(params, "dir") == "asc" else "DESC"
    limit = max(1, min(int(num(params, "limit") or 100), 500))
    offset = max(0, int(num(params, "offset") or 0))

    if deals:
        min_area = num(params, "min_area")
        head = BENCH_CTE
        pre = [30 if min_area is None else min_area]
        cols, joins = SELECT_COLS + DEALS_COLS, FROM_JOINS + DEALS_JOIN
    else:
        head, pre, cols, joins = "", [], SELECT_COLS, FROM_JOINS

    total = conn.execute(
        f"{head} SELECT COUNT(*) c FROM (SELECT a.id {joins} WHERE {where})", pre + args
    ).fetchone()["c"]

    rows = conn.execute(
        f"{head} {cols} {joins} WHERE {where} "
        f"ORDER BY {sort} IS NULL, {sort} {direction}, a.id DESC LIMIT ? OFFSET ?",
        pre + args + [limit, offset]
    ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        item["photos"] = json.loads(item["photos"]) if item["photos"] else []
        item["is_new"] = bool(item["first_seen"] and item["first_seen"] > base)
        if item["hist_points"] and item["hist_points"] > 1:
            item["hist"] = [
                [h["ts"], h["price"]]
                for h in conn.execute(
                    "SELECT ts, price FROM price_history WHERE ad_id = ? ORDER BY ts", (item["id"],))
            ]
        items.append(item)

    return {"total": total, "items": items, "baseline": base}


def api_map(conn, params) -> dict:
    deals = one(params, "tab") == "deals"
    where, args, base = build_where(conn, params)
    where += " AND a.lat IS NOT NULL AND a.lon IS NOT NULL"
    limit = max(1, min(int(num(params, "limit") or 12000), 20000))

    if deals:
        min_area = num(params, "min_area")
        head = BENCH_CTE
        pre = [30 if min_area is None else min_area]
        cols, joins = MAP_COLS + DEALS_COLS, FROM_JOINS + DEALS_JOIN
    else:
        head, pre, cols, joins = "", [], MAP_COLS, FROM_JOINS

    total = conn.execute(
        f"{head} SELECT COUNT(*) c FROM (SELECT a.id {joins} WHERE {where})", pre + args
    ).fetchone()["c"]
    rows = conn.execute(
        f"{head} {cols} {joins} WHERE {where} ORDER BY a.id LIMIT ?", pre + args + [limit]
    ).fetchall()

    points = [dict(r) for r in rows]
    return {"total": total, "shown": len(points), "capped": total > len(points), "points": points}


def api_ad(conn, ad_id: str) -> dict:
    row = conn.execute(f"{SELECT} WHERE a.id = ?", (ad_id,)).fetchone()
    if not row:
        return {"error": "не найдено"}
    item = dict(row)
    item["photos"] = json.loads(item["photos"]) if item["photos"] else []
    detail = conn.execute(
        "SELECT description, features_json, owner_login, owner_registered, owner_verified, "
        "state, region, city FROM ad_details WHERE id = ?", (ad_id,)).fetchone()
    if detail:
        item.update(dict(detail))
        item["features"] = json.loads(detail["features_json"]) if detail["features_json"] else {}
    item.pop("features_json", None)
    item["history"] = [
        dict(h) for h in conn.execute(
            "SELECT ts, price, currency FROM price_history WHERE ad_id = ? ORDER BY ts", (ad_id,))
    ]
    return item


def api_meta(conn) -> dict:
    out = {"profiles": [], "runs": [], "counts": {}}
    for key, profile in PROFILES.items():
        base = baseline(conn, key)
        row = conn.execute(
            "SELECT COUNT(*) c, SUM(CASE WHEN first_seen > ? THEN 1 ELSE 0 END) fresh "
            "FROM ads WHERE profile = ? AND gone_at IS NULL", (base, key)).fetchone()
        down = conn.execute(
            f"SELECT COUNT(*) c FROM ads a WHERE a.profile = ? AND a.gone_at IS NULL "
            f"AND (({DROP_ABS}) < 0 OR (a.old_price IS NOT NULL AND a.old_price > a.price))",
            (key,)).fetchone()["c"]
        recent = conn.execute(
            "SELECT COUNT(*) c FROM ads a JOIN ad_details d ON d.id = a.id "
            "WHERE a.profile = ? AND a.gone_at IS NULL AND d.posted > datetime('now', '-7 days')",
            (key,)).fetchone()["c"]
        deals = 0
        if key == "apartments":
            deals = conn.execute(
                f"""{BENCH_CTE} SELECT COUNT(*) c FROM (
                        SELECT a.id {FROM_JOINS}{DEALS_JOIN}
                        WHERE a.profile = ? AND a.gone_at IS NULL
                          AND {DEALS_RULES.replace(":min_area", "30")}
                          AND COALESCE(d.terms_flag, 0) = 0
                          AND b.pool_n >= 8
                          AND (b.med_ppm - {PPM}) * 100.0 / b.med_ppm BETWEEN 15 AND 60)""",
                (30, key)).fetchone()["c"]
        out["profiles"].append({
            "key": key, "title": profile["title"], "count": row["c"],
            "fresh": row["fresh"] or 0, "down": down, "recent7": recent, "baseline": base,
            "deals": deals,
        })
        out["counts"][key] = {
            "districts": [r["v"] for r in conn.execute(
                "SELECT DISTINCT d.district v FROM ad_details d JOIN ads a ON a.id = d.id "
                "WHERE a.profile = ? AND d.district <> '' ORDER BY 1", (key,))],
            "housing": [r["v"] for r in conn.execute(
                "SELECT DISTINCT d.housing_stock v FROM ad_details d JOIN ads a ON a.id = d.id "
                "WHERE a.profile = ? AND d.housing_stock <> '' ORDER BY 1", (key,))],
            "developers": [r["v"] for r in conn.execute(
                "SELECT d.developer v, COUNT(*) c FROM ad_details d JOIN ads a ON a.id = d.id "
                "WHERE a.profile = ? AND d.developer <> '' GROUP BY 1 ORDER BY c DESC LIMIT 60", (key,))],
        }
    out["runs"] = [dict(r) for r in conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 12")]
    out["details_done"] = conn.execute("SELECT COUNT(*) c FROM ad_details").fetchone()["c"]
    return out


def api_stats(conn, params) -> dict:
    profile = one(params, "profile", "apartments")

    by_district = [dict(r) for r in conn.execute(
        f"""SELECT d.district AS district, COUNT(*) AS n,
                   ROUND(AVG({PPM})) AS avg_ppm,
                   ROUND(MIN(a.price)) AS min_price, ROUND(AVG(a.price)) AS avg_price,
                   ROUND(MAX(a.price)) AS max_price
            FROM ads a JOIN ad_details d ON d.id = a.id
            WHERE a.profile = ? AND a.gone_at IS NULL AND d.district <> ''
            GROUP BY d.district HAVING n >= 3 ORDER BY avg_ppm DESC""", (profile,))]

    by_rooms = [dict(r) for r in conn.execute(
        f"""SELECT d.rooms_n AS rooms, COUNT(*) AS n, ROUND(AVG(a.price)) AS avg_price,
                   ROUND(AVG({PPM})) AS avg_ppm, ROUND(AVG(d.area), 1) AS avg_area
            FROM ads a JOIN ad_details d ON d.id = a.id
            WHERE a.profile = ? AND a.gone_at IS NULL AND d.rooms_n IS NOT NULL
            GROUP BY d.rooms_n ORDER BY d.rooms_n""", (profile,))]

    posted = [dict(r) for r in conn.execute(
        """SELECT substr(d.posted, 1, 7) AS month, COUNT(*) AS n
           FROM ads a JOIN ad_details d ON d.id = a.id
           WHERE a.profile = ? AND a.gone_at IS NULL AND d.posted <> ''
           GROUP BY month ORDER BY month DESC LIMIT 24""", (profile,))][::-1]

    changes = [dict(r) for r in conn.execute(
        """SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n
           FROM price_history h JOIN ads a ON a.id = h.ad_id
           WHERE a.profile = ? GROUP BY day ORDER BY day DESC LIMIT 60""", (profile,))][::-1]

    price_hist = [dict(r) for r in conn.execute(
        """SELECT CAST(a.price / 10000 AS INT) * 10 AS bucket_k, COUNT(*) AS n
           FROM ads a WHERE a.profile = ? AND a.gone_at IS NULL AND a.price > 0
           GROUP BY bucket_k ORDER BY bucket_k""", (profile,))]

    return {"by_district": by_district, "by_rooms": by_rooms, "posted": posted,
            "changes": changes, "price_hist": price_hist}


TREND_GROUPS = {
    "none":   "'все лоты'",
    "rooms":  ("CASE WHEN d.rooms_n IS NULL THEN NULL WHEN d.rooms_n = 0 THEN 'студия' "
               "WHEN d.rooms_n >= 5 THEN '5+ комн.' ELSE d.rooms_n || '-комн.' END"),
    "district": "NULLIF(d.district, '')",
    "housing":  "NULLIF(d.housing_stock, '')",
    "author":   ("CASE WHEN COALESCE(d.author_type,'') <> '' THEN d.author_type "
                 "WHEN COALESCE(d.owner_is_business,0) = 1 THEN 'Агентство' ELSE 'Частное лицо' END"),
    "area":   ("CASE WHEN d.area IS NULL OR d.area <= 0 THEN NULL WHEN d.area < 40 THEN 'до 40 m²' "
               "WHEN d.area < 60 THEN '40–60 m²' WHEN d.area < 80 THEN '60–80 m²' "
               "WHEN d.area < 100 THEN '80–100 m²' ELSE 'от 100 m²' END"),
    "floor":  ("CASE WHEN d.floor IS NULL THEN NULL WHEN d.floor = 1 THEN 'первый этаж' "
               "WHEN d.floors_total IS NOT NULL AND d.floor >= d.floors_total THEN 'последний этаж' "
               "ELSE 'средние этажи' END"),
    "developer": "NULLIF(d.developer, '')",
}

TREND_ORDER = {
    "rooms": ["студия", "1-комн.", "2-комн.", "3-комн.", "4-комн.", "5+ комн."],
    "area":  ["до 40 m²", "40–60 m²", "60–80 m²", "80–100 m²", "от 100 m²"],
    "floor": ["первый этаж", "средние этажи", "последний этаж"],
}

MAX_SERIES = 8
PPM_SANE = (100.0, 25000.0)


def _quantile(values: list[float], q: float):
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = q * (len(values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _month_range(first: str, last: str) -> list[str]:
    out = []
    y, m = int(first[:4]), int(first[5:7])
    ylast, mlast = int(last[:4]), int(last[5:7])
    while (y, m) <= (ylast, mlast):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def api_trends(conn, params) -> dict:
    group = one(params, "group", "none")
    expr = TREND_GROUPS.get(group, TREND_GROUPS["none"])
    months_n = max(3, min(int(num(params, "months") or 24), 120))
    min_n = max(1, int(num(params, "min_n") or 5))

    where, args, _ = build_where(conn, params)
    sql = f"""
        SELECT substr(d.posted, 1, 7) AS m, {expr} AS g,
               a.price AS price, d.area AS area, {PPM} AS ppm
        {FROM_JOINS}
        WHERE {where}
          AND d.posted IS NOT NULL AND d.posted <> ''
          AND a.price > 0
          AND substr(d.posted, 1, 7) >= strftime('%Y-%m', date('now', 'start of month', ?))
          AND substr(d.posted, 1, 7) <= strftime('%Y-%m', 'now')"""
    rows = conn.execute(sql, args + [f"-{months_n - 1} months"]).fetchall()

    cells: dict = {}
    for r in rows:
        key = (r["m"], r["g"] if r["g"] is not None else "не указано")
        cell = cells.get(key)
        if cell is None:
            cell = cells[key] = {"prices": [], "ppms": [], "areas": []}
        cell["prices"].append(r["price"])
        if r["ppm"] is not None and PPM_SANE[0] <= r["ppm"] <= PPM_SANE[1]:
            cell["ppms"].append(r["ppm"])
        if r["area"]:
            cell["areas"].append(r["area"])

    if not cells:
        return {"months": [], "series": [], "group": group, "min_n": min_n, "total": 0}

    months = _month_range(min(m for m, _ in cells), max(m for m, _ in cells))

    totals: dict = {}
    for (_, g), cell in cells.items():
        totals[g] = totals.get(g, 0) + len(cell["prices"])

    order = TREND_ORDER.get(group)
    if order:
        names = [g for g in order if g in totals] + sorted(
            (g for g in totals if g not in order), key=lambda g: -totals[g])
    else:
        names = sorted(totals, key=lambda g: (-totals[g], g))
    hidden = set(names[MAX_SERIES:])
    names = names[:MAX_SERIES]

    series = []
    for name in names:
        points = []
        for month in months:
            cell = cells.get((month, name))
            if not cell:
                points.append({"m": month, "n": 0})
                continue
            prices = sorted(cell["prices"])
            ppms = sorted(cell["ppms"])
            areas = sorted(cell["areas"])
            points.append({
                "m": month, "n": len(prices),
                "med_ppm": _quantile(ppms, .5), "avg_ppm": (sum(ppms) / len(ppms)) if ppms else None,
                "p25_ppm": _quantile(ppms, .25), "p75_ppm": _quantile(ppms, .75), "n_ppm": len(ppms),
                "med_price": _quantile(prices, .5), "avg_price": sum(prices) / len(prices),
                "med_area": _quantile(areas, .5),
            })
        series.append({"key": name, "n": totals[name], "points": points})

    return {"months": months, "series": series, "group": group, "min_n": min_n,
            "total": sum(totals.values()), "hidden": len(hidden),
            "hidden_n": sum(totals[g] for g in hidden)}


PRIVATE_FIELDS = ("phones", "owner_login", "owner_id", "owner_registered", "owner_verified")

PRIVATE_FEATURES = ("Контакты", "Агентство")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?373[\s\-.()]*)?0?[\s\-.()]*[67]\d(?:[\s\-.()]*\d){6}(?!\d)")
PHONE_MASK = "[номер скрыт]"

def strip_private(payload):
    items = payload.get("items") if isinstance(payload, dict) else None
    for item in (items if items is not None else [payload]):
        if not isinstance(item, dict):
            continue
        for field in PRIVATE_FIELDS:
            if field in item:
                item[field] = None
        if isinstance(item.get("description"), str):
            item["description"] = PHONE_RE.sub(PHONE_MASK, item["description"])
        features = item.get("features")
        if isinstance(features, dict):
            for field in PRIVATE_FEATURES:
                features.pop(field, None)
            text = features.get("Текст объявления")
            if isinstance(text, str):
                features["Текст объявления"] = PHONE_RE.sub(PHONE_MASK, text)
    return payload


NOTIFY_CONFIG = os.path.join(HERE, "notify.json")
CONTACT_LIMIT_IP = 5
CONTACT_LIMIT_ALL = 40
TOKEN_MIN_AGE = 3
TOKEN_MAX_AGE = 7200
MAX_BODY = 64 * 1024
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[a-z0-9.-]{1,180}\.[a-z]{2,24}$", re.I)


def form_secret(conn) -> bytes:
    value = db.get_meta(conn, "form_secret")
    if not value:
        value = os.urandom(32).hex()
        db.set_meta(conn, "form_secret", value)
        conn.commit()
    return value.encode()


def sign_stamp(conn, stamp: str) -> str:
    return hmac.new(form_secret(conn), stamp.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(conn) -> str:
    stamp = str(int(time.time()))
    return f"{stamp}.{sign_stamp(conn, stamp)}"


def check_token(conn, token: str):
    stamp, _, sign = (token or "").partition(".")
    stale = "форма устарела, обновите страницу"
    if not stamp.isdigit() or len(sign) != 32:
        return stale
    if not hmac.compare_digest(sign, sign_stamp(conn, stamp)):
        return stale
    age = time.time() - int(stamp)
    if age < TOKEN_MIN_AGE:
        return "слишком быстро — похоже на робота"
    if age > TOKEN_MAX_AGE:
        return stale
    return None


def notify_config():
    try:
        with open(NOTIFY_CONFIG, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def send_telegram(cfg: dict, row: dict) -> None:
    text = (f"Сообщение с сайта\n\n"
            f"Имя: {row['name']}\n"
            f"Почта: {row['email']}\n"
            f"Адрес: {row['ip']}\n"
            f"Время: {row['ts']}\n\n"
            f"{row['body']}")
    payload = json.dumps({
        "chat_id": cfg["chat_id"],
        "text": text[:4000],
        "disable_web_page_preview": True,
    }).encode()
    url = "https://api.telegram.org/bot%s/sendMessage" % quote(str(cfg["token"]), safe=":")
    request = urllib.request.Request(url, data=payload, headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        answer = json.loads(response.read())
    if not answer.get("ok"):
        raise RuntimeError(answer.get("description") or "телеграм отказал")


def deliver(row: dict) -> None:
    cfg = notify_config()
    if not cfg:
        return
    conn = db.connect()
    try:
        send_telegram(cfg, row)
        conn.execute("UPDATE messages SET sent = 1, error = NULL WHERE id = ?", (row["id"],))
    except Exception as exc:
        conn.execute("UPDATE messages SET sent = 0, error = ? WHERE id = ?",
                     (f"{type(exc).__name__}: {exc}"[:500], row["id"]))
    finally:
        conn.commit()
        conn.close()


def api_contact(conn, payload: dict, ip: str) -> dict:
    if (payload.get("website") or "").strip():
        return {"ok": True}

    problem = check_token(conn, payload.get("token", ""))
    if problem:
        return {"error": problem}

    name = (payload.get("name") or "").strip()[:100]
    email = (payload.get("email") or "").strip()[:200]
    body = (payload.get("body") or "").strip()[:5000]
    if len(name) < 2:
        return {"error": "напишите, как к вам обращаться"}
    if not EMAIL_RE.match(email):
        return {"error": "проверьте адрес почты"}
    if len(body) < 10:
        return {"error": "слишком короткое сообщение"}

    recent_ip = conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE ip = ? AND ts > datetime('now', '-1 hour')",
        (ip,)).fetchone()["c"]
    recent_all = conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE ts > datetime('now', '-1 hour')").fetchone()["c"]
    if recent_ip >= CONTACT_LIMIT_IP or recent_all >= CONTACT_LIMIT_ALL:
        return {"error": "слишком много сообщений, попробуйте позже"}

    stamp = db.now()
    cursor = conn.execute(
        "INSERT INTO messages (ts, ip, name, email, body) VALUES (?,?,?,?,?)",
        (stamp, ip, name, email, body))
    conn.commit()
    row = {"id": cursor.lastrowid, "ts": stamp, "ip": ip, "name": name, "email": email, "body": body}
    threading.Thread(target=deliver, args=(row,), daemon=True).start()
    return {"ok": True}


MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря")
MONTHS_NOM = ("январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
SUMMARY_TTL = 600
_page_cache: dict = {}

SECTORS = {"Ботаника": "botanica", "Центр": "centru", "Чокана": "ciocana", "Буюканы": "buiucani",
           "Рышкановка": "riscani", "Телецентр": "telecentru", "Старая Почта": "posta-veche",
           "Скулянка": "sculeanca", "Аэропорт": "aeroport", "Окраина": "okraina"}
SECTOR_BY_SLUG = {slug: name for name, slug in SECTORS.items()}
SECTIONS = (("/", "Квартиры"), ("/garages", "Гаражи и парковки"),
            ("/stats", "Статистика"), ("/trends", "Динамика цен"))


def money(value) -> str:
    return f"{round(value):,}".replace(",", " ") if value else "—"


def rooms_label(rooms: int) -> str:
    return "студии" if rooms == 0 else f"{rooms}-комнатные"


def sector_link(name: str) -> str:
    slug = SECTORS.get(name)
    return f'<a href="/sector/{slug}">{escape(name)}</a>' if slug else escape(name)


def date_text() -> str:
    today = datetime.now()
    return f"{today.day} {MONTHS_RU[today.month - 1]} {today.year}"


def links_html(conn, path: str) -> str:
    def link(href, text):
        return f"<b>{escape(text)}</b>" if href == path else f'<a href="{href}">{escape(text)}</a>'
    names = [r["district"] for r in conn.execute(
        """SELECT d.district, COUNT(*) n FROM ads a JOIN ad_details d ON d.id = a.id
           WHERE a.profile = 'apartments' AND a.gone_at IS NULL AND d.district <> ''
           GROUP BY d.district ORDER BY n DESC""") if r["district"] in SECTORS]
    return f"""<nav class="links">
  <h3>Разделы</h3>
  <p>{' · '.join(link(href, text) for href, text in SECTIONS)}</p>
  <h3>Квартиры по секторам Кишинёва</h3>
  <p>{' · '.join(link('/sector/' + SECTORS[n], n) for n in names)}</p>
</nav>"""


def rooms_table(rows) -> str:
    body = "".join(
        f"<tr><td>{rooms_label(r['rooms'])}</td><td>{money(r['avg_ppm'])} €</td>"
        f"<td>{money(r['avg_area'])} m²</td><td>{money(r['avg_price'])} €</td><td>{r['n']}</td></tr>"
        for r in rows)
    return ("<table class=\"data\"><tr><th>Квартиры</th><th>Цена за m²</th><th>Средняя площадь</th>"
            f"<th>Средняя цена</th><th>Объявлений</th></tr>\n  {body}</table>")


def trend_table(conn, params) -> str:
    data = api_trends(conn, {"profile": ["apartments"], "gone": ["both"], "months": ["12"],
                             "min_n": ["5"], **params})
    points = [p for p in (data["series"][0]["points"] if data["series"] else [])
              if p["n"] >= 5 and p.get("med_ppm")]
    if not points:
        return ""
    body = "".join(
        f"<tr><td>{MONTHS_NOM[int(p['m'][5:]) - 1]} {p['m'][:4]}</td><td>{money(p['med_ppm'])} €</td>"
        f"<td>{money(p['med_price'])} €</td><td>{p['n']}</td></tr>" for p in points)
    return ("<table class=\"data\"><tr><th>Месяц</th><th>Медиана за m²</th><th>Медиана цены</th>"
            f"<th>Объявлений</th></tr>\n  {body}</table>")


def district_rows(stats) -> str:
    return "".join(
        f"<tr><td>{sector_link(r['district'])}</td><td>{money(r['avg_ppm'])} €</td>"
        f"<td>{money(r['avg_price'])} €</td><td>{r['n']}</td></tr>"
        for r in stats["by_district"] if r["avg_ppm"])


def active_count(conn, profile: str) -> int:
    return conn.execute("SELECT COUNT(*) c FROM ads WHERE profile = ? AND gone_at IS NULL",
                        (profile,)).fetchone()["c"]


def home_page(conn) -> dict:
    stats = api_stats(conn, {"profile": ["apartments"]})
    rooms = [r for r in stats["by_rooms"] if r["rooms"] is not None and r["n"] >= 5]
    body = f"""<h2>Цены на недвижимость в Кишинёве на {date_text()}</h2>
  <p>В базе {money(active_count(conn, 'apartments'))} действующих объявлений о продаже квартир
  и {money(active_count(conn, 'garages'))} объявлений о продаже гаражей и парковочных мест
  в Кишинёве. Данные обновляются каждые полчаса, по каждому объявлению сохраняется история
  изменения цены, поэтому видно, кто из продавцов снижает цену и насколько.</p>
  <h3>Средняя цена квадратного метра по секторам Кишинёва</h3>
  <table class="data"><tr><th>Сектор</th><th>Цена за m²</th><th>Средняя цена лота</th><th>Объявлений</th></tr>
  {district_rows(stats)}</table>
  <h3>Цены по количеству комнат</h3>
  {rooms_table(rooms)}
  <p>Цифры посчитаны по действующим объявлениям с карты 999.md: вторичное жильё и новостройки
  вместе, без учёта снятых с продажи лотов. Помесячная динамика цены за квадратный метр,
  фильтры по площади, этажу и жилому фонду, карта и отбор недооценённых предложений —
  во вкладках выше.</p>"""
    return {"title": "Цены на квартиры в Кишинёве — анализ рынка недвижимости",
            "description": "Цены на квартиры, гаражи и парковки в Кишинёве: история снижения цен, "
                           "статистика по секторам и динамика цены за m² по месяцам.",
            "h1": "Недвижимость Кишинёва: цены, статистика и динамика рынка",
            "init": {"profile": "apartments", "tab": "new"}, "body": body}


def garages_page(conn) -> dict:
    stats = api_stats(conn, {"profile": ["garages"]})
    total = active_count(conn, "garages")
    rows = "".join(f"<tr><td>{escape(r['district'])}</td><td>{money(r['avg_price'])} €</td><td>{r['n']}</td></tr>"
                   for r in sorted(stats["by_district"], key=lambda r: -(r["avg_price"] or 0)))
    body = f"""<h2>Цены на гаражи и парковки в Кишинёве на {date_text()}</h2>
  <p>В базе {money(total)} действующих объявлений о продаже гаражей и парковочных мест
  в Кишинёве, от кирпичных боксов до мест в подземных паркингах новостроек. По каждому
  объявлению хранится история цены: видно, какие гаражи подешевели и на сколько.</p>
  <h3>Средняя цена гаража по секторам</h3>
  <table class="data"><tr><th>Сектор</th><th>Средняя цена</th><th>Объявлений</th></tr>
  {rows}</table>"""
    return {"title": "Гаражи и парковки в Кишинёве — цены и объявления",
            "description": f"{money(total)} объявлений о продаже гаражей и парковочных мест в Кишинёве: "
                           "цены по секторам, новые объявления и снижения цен.",
            "h1": "Гаражи и парковочные места в Кишинёве: цены и объявления",
            "init": {"profile": "garages", "tab": "new"}, "body": body}


def stats_page(conn) -> dict:
    stats = api_stats(conn, {"profile": ["apartments"]})
    rooms = [r for r in stats["by_rooms"] if r["rooms"] is not None and r["n"] >= 5]
    body = f"""<h2>Статистика цен на квартиры в Кишинёве на {date_text()}</h2>
  <p>Сводка по {money(active_count(conn, 'apartments'))} действующим объявлениям о продаже квартир:
  в каких секторах квадратный метр дороже и сколько в среднем стоят квартиры по числу комнат.</p>
  <h3>Средняя цена квадратного метра по секторам</h3>
  <table class="data"><tr><th>Сектор</th><th>Цена за m²</th><th>Средняя цена</th><th>Объявлений</th></tr>
  {district_rows(stats)}</table>
  <h3>Цены по количеству комнат</h3>
  {rooms_table(rooms)}"""
    return {"title": "Статистика цен на квартиры в Кишинёве по секторам",
            "description": "Средняя цена квадратного метра и квартиры по секторам Кишинёва "
                           "и по числу комнат, распределение цен и активность продавцов.",
            "h1": "Статистика цен на квартиры в Кишинёве",
            "init": {"profile": "apartments", "tab": "stats"}, "body": body}


def trends_page(conn) -> dict:
    body = f"""<h2>Как менялась цена квадратного метра в Кишинёве</h2>
  <p>Медиана цены за m² по месяцу публикации объявления, за последние двенадцать месяцев.
  В расчёт идут и действующие, и уже снятые с продажи квартиры, поэтому видно, по каким ценам
  рынок выставлял жильё в каждом месяце. На графике выше тренд можно разложить по секторам,
  числу комнат и жилому фонду.</p>
  {trend_table(conn, {})}"""
    return {"title": "Динамика цен на квартиры в Кишинёве по месяцам",
            "description": "Как меняется цена квадратного метра в Кишинёве: медиана и средняя "
                           "по месяцам, в разрезе секторов, комнат и жилого фонда.",
            "h1": "Динамика цен на квартиры в Кишинёве",
            "init": {"profile": "apartments", "tab": "trends"}, "body": body}


def sector_page(conn, name: str) -> dict:
    where = "a.profile = 'apartments' AND a.gone_at IS NULL AND d.district = ?"
    head = conn.execute(
        f"""SELECT COUNT(*) n, ROUND(AVG({PPM})) avg_ppm, ROUND(AVG(a.price)) avg_price,
                   SUM(CASE WHEN ({DROP_ABS}) < 0 THEN 1 ELSE 0 END) down
            FROM ads a JOIN ad_details d ON d.id = a.id WHERE {where}""", (name,)).fetchone()
    rooms = [dict(r) for r in conn.execute(
        f"""SELECT d.rooms_n AS rooms, COUNT(*) AS n, ROUND(AVG(a.price)) AS avg_price,
                   ROUND(AVG({PPM})) AS avg_ppm, ROUND(AVG(d.area), 1) AS avg_area
            FROM ads a JOIN ad_details d ON d.id = a.id
            WHERE {where} AND d.rooms_n IS NOT NULL
            GROUP BY d.rooms_n HAVING n >= 3 ORDER BY d.rooms_n""", (name,))]
    trend = trend_table(conn, {"districts": [name]})
    title = escape(name)
    body = f"""<h2>Цены на квартиры в секторе {title} на {date_text()}</h2>
  <p>В секторе {title} сейчас {money(head['n'])} действующих объявлений о продаже квартир.
  Средняя цена квадратного метра — {money(head['avg_ppm'])} €, средняя цена квартиры —
  {money(head['avg_price'])} €. Снизили цену с момента публикации: {money(head['down']) if head['down'] else 'ни одного'}.</p>
  <h3>Цены по количеству комнат в секторе {title}</h3>
  {rooms_table(rooms)}
  {f'<h3>Медиана цены за m² в секторе {title} по месяцам</h3>' + chr(10) + '  ' + trend if trend else ''}"""
    return {"title": f"Квартиры в секторе {name}, Кишинёв — цены за m²",
            "description": f"Квартиры в секторе {name} (Кишинёв): {money(head['n'])} объявлений, "
                           f"средняя цена {money(head['avg_ppm'])} € за m², цены по числу комнат "
                           "и динамика по месяцам.",
            "h1": f"Квартиры в секторе {name}, Кишинёв: цены и объявления",
            "init": {"profile": "apartments", "tab": "all", "district": name}, "body": body}


def page_for(conn, route: str) -> dict | None:
    path = route.rstrip("/") or "/"
    if path == "/index.html":
        path = "/"
    if path.startswith("/sector/"):
        name = SECTOR_BY_SLUG.get(path[len("/sector/"):])
        if not name:
            return None
        build = lambda: sector_page(conn, name)
    else:
        build = {"/": lambda: home_page(conn), "/garages": lambda: garages_page(conn),
                 "/stats": lambda: stats_page(conn), "/trends": lambda: trends_page(conn)}.get(path)
        if not build:
            return None

    now = time.time()
    cached = _page_cache.get(path)
    if cached and now - cached["at"] < SUMMARY_TTL:
        return cached
    page = build()
    page.update(at=now, path=path,
                summary=f'<section class="seo">\n  {page.pop("body")}\n{links_html(conn, path)}\n</section>')
    _page_cache[path] = page
    return page


def sitemap_xml(origin: str) -> str:
    today = db.now()[:10]
    paths = [p for p, _ in SECTIONS] + [f"/sector/{slug}" for slug in SECTORS.values()]
    urls = "".join(
        f"  <url><loc>{origin}{p}</loc><lastmod>{today}</lastmod><changefreq>hourly</changefreq>"
        f"<priority>{'1.0' if p == '/' else '0.8'}</priority></url>\n" for p in paths)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}</urlset>\n')


def api_flag(conn, payload: dict) -> dict:
    ad_id = str(payload.get("id") or "")
    if not ad_id:
        return {"error": "нет id"}
    row = conn.execute("SELECT * FROM flags WHERE ad_id = ?", (ad_id,)).fetchone()
    favorite = payload.get("favorite", row["favorite"] if row else 0)
    hidden = payload.get("hidden", row["hidden"] if row else 0)
    note = payload.get("note", row["note"] if row else "")
    conn.execute(
        """INSERT INTO flags (ad_id, favorite, hidden, note, updated) VALUES (?,?,?,?,?)
           ON CONFLICT(ad_id) DO UPDATE SET favorite=excluded.favorite, hidden=excluded.hidden,
                                            note=excluded.note, updated=excluded.updated""",
        (ad_id, int(bool(favorite)), int(bool(hidden)), note, db.now()))
    conn.commit()
    return {"ok": True, "id": ad_id, "favorite": int(bool(favorite)),
            "hidden": int(bool(hidden)), "note": note}


class Handler(BaseHTTPRequestHandler):
    conn_local = threading.local()

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self.conn_local, "conn"):
            self.conn_local.conn = db.connect()
        return self.conn_local.conn

    def log_message(self, *args):
        pass

    def origin(self) -> str:
        scheme = self.headers.get("x-forwarded-proto") or "http"
        host = self.headers.get("host") or "127.0.0.1"
        return f"{scheme}://{host}"

    def send_page(self, page: dict):
        init = json.dumps(page["init"], ensure_ascii=False).replace("</", "<\\/")
        with open(INDEX, encoding="utf-8") as fh:
            html = fh.read()
        for key, value in (("{{TITLE}}", escape(page["title"])),
                           ("{{DESCRIPTION}}", escape(page["description"])),
                           ("{{H1}}", escape(page["h1"])),
                           ("{{PATH}}", page["path"]),
                           ("{{INIT}}", init),
                           ("{{SUMMARY}}", page["summary"]),
                           ("{{ORIGIN}}", self.origin())):
            html = html.replace(key, value)
        self.send_bytes(html.encode(), "text/html; charset=utf-8")

    def send_bytes(self, body: bytes, ctype: str, status=200):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: str, ctype: str):
        with open(path, "rb") as fh:
            self.send_bytes(fh.read(), ctype)

    def send_json(self, payload, status=200):
        self.send_bytes(json.dumps(payload, ensure_ascii=False).encode(),
                        "application/json; charset=utf-8", status)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)
        try:
            page = None if route.startswith("/api/") else page_for(self.conn, route)
            if page:
                self.send_page(page)
            elif route == "/favicon.ico":
                self.send_file(FAVICON, "image/x-icon")
            elif route == "/og.png":
                self.send_file(OG_IMAGE, "image/png")
            elif route == "/robots.txt":
                self.send_bytes(f"{ROBOTS}Sitemap: {self.origin()}/sitemap.xml\n".encode(),
                                "text/plain; charset=utf-8")
            elif route == "/sitemap.xml":
                self.send_bytes(sitemap_xml(self.origin()).encode(), "application/xml")
            elif route == "/api/contact/token":
                self.send_json({"token": make_token(self.conn)})
            elif route == "/api/meta":
                self.send_json(api_meta(self.conn))
            elif route == "/api/ads":
                self.send_json(strip_private(api_ads(self.conn, params)))
            elif route == "/api/map":
                self.send_json(api_map(self.conn, params))
            elif route == "/api/stats":
                self.send_json(api_stats(self.conn, params))
            elif route == "/api/trends":
                self.send_json(api_trends(self.conn, params))
            elif route.startswith("/api/ad/"):
                self.send_json(strip_private(api_ad(self.conn, route.rsplit("/", 1)[-1])))
            else:
                self.send_json({"error": "нет такого адреса"}, 404)
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "внутренняя ошибка"}, 500)

    @property
    def client_ip(self) -> str:
        forwarded = self.headers.get("x-forwarded-for") or ""
        return (forwarded.split(",")[0].strip()
                or self.headers.get("x-real-ip")
                or self.client_address[0])

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("allow", "GET, HEAD, POST")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_BODY:
            return self.send_json({"error": "слишком большой запрос"}, 413)
        route = urlparse(self.path).path
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "битый JSON"}, 400)
        try:
            if route == "/api/flag":
                self.send_json(api_flag(self.conn, payload))
            elif route == "/api/contact":
                self.send_json(api_contact(self.conn, payload, self.client_ip))
            else:
                self.send_json({"error": "нет такого адреса"}, 404)
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "внутренняя ошибка"}, 500)


def main() -> int:
    parser = argparse.ArgumentParser(description="Дашборд 999.md")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[✓] дашборд: {url}   (Ctrl+C — остановить)")
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
