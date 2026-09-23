#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import re
import sys
import time
from urllib.parse import parse_qsl, urlparse

import requests

GRAPHQL_URL = "https://999.md/graphql"

START_URL = (
    "https://999.md/ru/map/real-estate"
    "?o_16_1=776&o_32_7=12900&from_9441_2=50000&to_9441_2=95000&unit_9441_2=eur&spec=1404"
)

DEFAULT_BBOX = (46.93187895804624, 28.50558181762733, 47.113965779064955, 29.164418182373254)

MAX_LIMIT = 1000

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

QUERY = """query ScopeAds($input: Geo_ScopeAdsInput!) {
  scopeAds(input: $input) {
    total
    ads {
      id
      title
      price: feature(id: 2) { id value }
      oldPrice: feature(id: 1640) { id value }
      street: feature(id: 10) { id value }
      mapPoint: feature(id: 3) { id value }
      subCategory { id title { translated } }
    }
  }
}"""

CSV_FIELDS = [
    "id",
    "title",
    "price",
    "currency",
    "old_price",
    "old_currency",
    "street",
    "subcategory",
    "lat",
    "lon",
    "url",
]

RE_OPTION = re.compile(r"^o_(\d+)_(\d+)$")
RE_RANGE = re.compile(r"^(from|to|unit)_(\d+)_(\d+)$")


def parse_start_url(url: str) -> tuple[int, dict, list[dict]]:
    parsed = urlparse(url)
    params = parse_qsl(parsed.query, keep_blank_values=True)

    category_id = 270
    spec = None
    options: dict[tuple[int, int], list[int]] = {}
    ranges: dict[tuple[int, int], dict] = {}

    for key, value in params:
        if key == "spec":
            spec = int(value)
            continue
        if key in ("cat", "category", "categoryId"):
            category_id = int(value)
            continue

        m = RE_OPTION.match(key)
        if m:
            fid, feat = int(m.group(1)), int(m.group(2))
            options.setdefault((fid, feat), []).append(int(value))
            continue

        m = RE_RANGE.match(key)
        if m:
            kind, fid, feat = m.group(1), int(m.group(2)), int(m.group(3))
            slot = ranges.setdefault((fid, feat), {})
            if kind == "unit":
                slot["unit"] = "UNIT_" + value.upper()
            else:
                slot[kind] = value

    by_filter: dict[int, list[dict]] = {}
    for (fid, feat), option_ids in options.items():
        by_filter.setdefault(fid, []).append({"featureId": feat, "optionIds": option_ids})
    for (fid, feat), slot in ranges.items():
        rng = {}
        if "from" in slot:
            rng["min"] = slot["from"]
        if "to" in slot:
            rng["max"] = slot["to"]
        feature = {"featureId": feat, "range": rng}
        if "unit" in slot:
            feature["unit"] = slot["unit"]
        by_filter.setdefault(fid, []).append(feature)

    filters = [
        {"filterId": fid, "features": features}
        for fid, features in sorted(by_filter.items())
    ]
    return category_id, spec, filters


def build_polygon(bbox: tuple[float, float, float, float]) -> dict:
    lat_min, lng_min, lat_max, lng_max = bbox
    return {
        "points": [
            {"lng": lng_max, "lat": lat_max},
            {"lat": lat_max, "lng": lng_min},
            {"lng": lng_min, "lat": lat_min},
            {"lat": lat_min, "lng": lng_max},
        ]
    }


def make_session(start_url: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "accept": "*/*",
            "accept-language": "ru,en-US;q=0.9,en;q=0.8",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "content-type": "application/json",
            "lang": "ru",
            "origin": "https://999.md",
            "referer": start_url,
            "source": "desktop_redesign",
            "user-agent": USER_AGENT,
        }
    )

    resp = session.get(start_url, headers={"accept": "text/html,application/xhtml+xml"}, timeout=30)
    resp.raise_for_status()
    print(f"[i] стартовая страница: HTTP {resp.status_code}, куки: "
          f"{', '.join(session.cookies.keys()) or 'нет'}", file=sys.stderr)

    return session


def fetch_page(session: requests.Session, variables_input: dict, retries: int = 3) -> dict:
    payload = {"operationName": "ScopeAds", "variables": {"input": variables_input}, "query": QUERY}
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.post(GRAPHQL_URL, data=json.dumps(payload), timeout=60)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                raise RuntimeError(json.dumps(data["errors"], ensure_ascii=False)[:400])
            return data["data"]["scopeAds"]
        except Exception as exc:
            last_error = exc
            print(f"[!] попытка {attempt}/{retries} не удалась: {exc}", file=sys.stderr)
            time.sleep(2 * attempt)
    raise RuntimeError(f"запрос не удался: {last_error}")


def money(feature) -> tuple[str, str]:
    if not feature:
        return "", ""
    value = feature.get("value")
    if isinstance(value, dict):
        amount = value.get("value", "")
        unit = value.get("unit") or value.get("measurement") or ""
        return ("" if amount is None else str(amount), str(unit or "").replace("UNIT_", ""))
    return ("" if value is None else str(value), "")


def flatten(ad: dict) -> dict:
    price, currency = money(ad.get("price"))
    old_price, old_currency = money(ad.get("oldPrice"))

    street = ad.get("street") or {}
    street_value = street.get("value") or ""

    sub = ad.get("subCategory") or {}
    sub_title = (sub.get("title") or {}).get("translated") or ""

    point = (ad.get("mapPoint") or {}).get("value") or {}
    lat = point.get("lat", "") if isinstance(point, dict) else ""
    lon = point.get("lon", "") if isinstance(point, dict) else ""

    return {
        "id": ad.get("id", ""),
        "title": ad.get("title", ""),
        "price": price,
        "currency": currency,
        "old_price": old_price,
        "old_currency": old_currency,
        "street": street_value,
        "subcategory": sub_title,
        "lat": lat,
        "lon": lon,
        "url": f"https://999.md/ru/{ad.get('id', '')}",
    }


def main() -> int:
    today = dt.date.today().isoformat()
    parser = argparse.ArgumentParser(description="Сбор объявлений 999.md в CSV")
    parser.add_argument("--url", default=START_URL, help="ссылка-точка отправки (с фильтрами)")
    parser.add_argument("--out", default=f"appart-{today}.csv", help="файл результата")
    parser.add_argument("--limit", type=int, default=MAX_LIMIT, help=f"размер страницы (<= {MAX_LIMIT})")
    parser.add_argument("--spec", type=int, default=None,
                        help="оставить только эту подкатегорию, напр. 1404 — квартиры "
                             "(по умолчанию сохраняются все)")
    parser.add_argument("--bbox", default=None,
                        help="границы карты 'lat_min,lng_min,lat_max,lng_max'")
    parser.add_argument("--delay", type=float, default=1.0, help="пауза между страницами, сек")
    args = parser.parse_args()

    limit = max(1, min(args.limit, MAX_LIMIT))
    bbox = tuple(float(x) for x in args.bbox.split(",")) if args.bbox else DEFAULT_BBOX
    if len(bbox) != 4:
        parser.error("--bbox ожидает 4 числа: lat_min,lng_min,lat_max,lng_max")

    category_id, url_spec, filters = parse_start_url(args.url)
    spec = args.spec or None

    print(f"[i] categoryId={category_id} фильтров={len(filters)}", file=sys.stderr)
    if spec:
        print(f"[i] отбор по подкатегории spec={spec}", file=sys.stderr)
    elif url_spec:
        print(f"[i] в ссылке spec={url_spec}, но сохраняются все подкатегории "
              f"(включить отбор: --spec {url_spec})", file=sys.stderr)
    print(f"[i] фильтры: {json.dumps(filters, ensure_ascii=False)}", file=sys.stderr)

    session = make_session(args.url)

    rows: list[dict] = []
    seen: set[str] = set()
    skipped_by_spec: collections.Counter = collections.Counter()
    skip = 0
    total = None

    while True:
        payload_input = {
            "polygon": build_polygon(bbox),
            "skip": skip,
            "limit": limit,
            "categoryId": category_id,
            "filters": filters,
        }
        scope = fetch_page(session, payload_input)
        total = scope["total"]
        ads = scope["ads"] or []
        print(f"[i] skip={skip}: получено {len(ads)} из total={total}", file=sys.stderr)

        for ad in ads:
            ad_id = str(ad.get("id", ""))
            if not ad_id or ad_id in seen:
                continue
            seen.add(ad_id)
            if spec:
                sub = ad.get("subCategory") or {}
                if sub.get("id") != spec:
                    label = (sub.get("title") or {}).get("translated") or "без подкатегории"
                    skipped_by_spec[f"{label} (id={sub.get('id')})"] += 1
                    continue
            rows.append(flatten(ad))

        skip += limit
        if not ads or skip >= total:
            break
        time.sleep(args.delay)

    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[✓] всего объявлений по фильтру: {total}; уникальных обработано: {len(seen)}", file=sys.stderr)
    if spec:
        dropped = sum(skipped_by_spec.values())
        print(f"[i] отброшено по spec={spec}: {dropped}", file=sys.stderr)
        for label, count in skipped_by_spec.most_common():
            print(f"      - {label}: {count}", file=sys.stderr)
        print(f"[i] сходимость: {len(rows)} записано + {dropped} отброшено = "
              f"{len(rows) + dropped} (уникальных {len(seen)}, total {total})", file=sys.stderr)
    print(f"[✓] записано {len(rows)} строк в {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
