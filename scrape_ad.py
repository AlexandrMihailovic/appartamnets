#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys

import requests

from db import SECTOR_RENAMES
from scrape_999 import USER_AGENT, make_session

GRAPHQL_URL = "https://999.md/graphql"
IMAGE_BASE = "https://i.simpalsmedia.com/999.md/BoardImages"
AVATAR_BASE = "https://i.simpalsmedia.com/forum.md/avatars"

DATE_FMT = {"format": "2006-01-02 15:04:05", "timezone": "Europe/Chisinau"}

QUERY = """query AdvertFull($input: AdvertInput!, $df: DatetimeFormat, $id: String!) {
  advert(input: $input) {
    id
    state
    title
    isExpired
    posted(input: $df)
    reseted(input: $df)
    expire(input: $df)
    subCategory {
      id
      title { translated }
      parent { id title { translated } parent { id title { translated } } }
    }
    owner {
      id
      login
      avatar
      createdDate(input: $df)
      business { id plan marketplace hasDelivery }
      verification { isVerified date }
      isDeleted
    }
    groups(placement: VIEW_ONE_DESKTOP) {
      title
      controls {
        type
        title
        option_title
        with_link
        feature { id type value }
      }
    }
  }
  views: adViews(input: {adId: $id}) { total today sinceRepublish }
}"""

FEATURE_IDS = {
    1: "offer_type",
    2: "price",
    3: "map",
    7: "region",
    8: "city",
    9: "district",
    10: "street",
    11: "house",
    13: "body",
    14: "photos",
    15: "video",
    16: "contacts",
    241: "rooms",
    244: "area",
    248: "floor",
    249: "floors_total",
    795: "author_type",
    852: "housing_stock",
    1385: "price_per_m2",
    1640: "old_price",
    1658: "developer",
    2199: "living_room",
    2562: "uploaded_video",
}

UNITS = {
    "UNIT_METER_SQUARE": "m²",
    "UNIT_ARE": "сот.",
    "UNIT_HECTARE": "га",
    "UNIT_EUR": "EUR",
    "UNIT_USD": "USD",
    "UNIT_MDL": "MDL",
    "UNIT_LEI": "MDL",
}

CSV_FIELDS = [
    "id", "url", "title", "state", "posted", "reseted", "expire", "is_expired",
    "views_total", "views_today", "views_since_republish",
    "category", "subcategory", "subcategory_id",
    "offer_type", "author_type", "price", "currency", "bargain", "price_per_m2",
    "old_price", "old_currency",
    "rooms", "area", "floor", "floors_total", "housing_stock", "living_room", "developer",
    "region", "city", "district", "street", "house", "lat", "lon",
    "owner_login", "owner_id", "owner_registered", "owner_is_business", "owner_business_plan",
    "owner_verified", "phones", "photos_count", "photos", "description",
]


def ad_id_from(value: str) -> str:
    m = re.search(r"(\d{6,})", value)
    if not m:
        raise SystemExit(f"не удалось определить id объявления из {value!r}")
    return m.group(1)


def unit(name) -> str:
    return UNITS.get(name, (name or "").replace("UNIT_", ""))


def feature_text(value) -> str:
    if value is None or value == {} or value == []:
        return ""
    if isinstance(value, dict):
        if "translated" in value:
            return str(value["translated"])
        if "phone_numbers" in value:
            return ", ".join(value["phone_numbers"])
        if "lat" in value and "lon" in value:
            return f"{value['lat']}, {value['lon']}"
        if "value" in value:
            amount = value["value"]
            measure = unit(value.get("unit") or value.get("measurement"))
            text = f"{amount} {measure}".strip()
            if value.get("bargain"):
                text += " (торг)"
            return text
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return ", ".join(str(x) for x in value)
    return str(value)


def fetch(ad_id: str, session: requests.Session) -> dict:
    payload = {
        "operationName": "AdvertFull",
        "variables": {"input": {"id": ad_id}, "df": DATE_FMT, "id": ad_id},
        "query": QUERY,
    }
    resp = session.post(GRAPHQL_URL, data=json.dumps(payload), timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise SystemExit("GraphQL: " + json.dumps(data["errors"], ensure_ascii=False)[:500])
    if not (data.get("data") or {}).get("advert"):
        raise SystemExit(f"объявление {ad_id} не найдено или снято с публикации")
    return data["data"]


def parse(data: dict, ad_id: str) -> dict:
    ad = data["advert"]
    views = data.get("views") or {}

    by_id: dict[int, dict] = {}
    groups: list[dict] = []
    for group in ad.get("groups") or []:
        items = []
        for control in group.get("controls") or []:
            feature = control.get("feature") or {}
            fid = feature.get("id")
            item = {
                "id": fid,
                "title": control.get("title") or "",
                "type": feature.get("type"),
                "value": feature.get("value"),
                "text": feature_text(feature.get("value")),
            }
            items.append(item)
            if fid is not None:
                by_id[fid] = item
        if items:
            groups.append({"group": group.get("title") or "", "items": items})

    def raw(fid):
        return (by_id.get(fid) or {}).get("value")

    def text(fid):
        return (by_id.get(fid) or {}).get("text", "")

    price = raw(2) or {}
    old_price = raw(1640) or {}
    point = raw(3) or {}
    body = raw(13) or {}
    photos = raw(14) or []
    contacts = raw(16) or {}
    area = raw(244) or {}

    sub = ad.get("subCategory") or {}
    parent = sub.get("parent") or {}
    breadcrumbs = [
        ((parent.get("parent") or {}).get("title") or {}).get("translated"),
        (parent.get("title") or {}).get("translated"),
        (sub.get("title") or {}).get("translated"),
    ]
    breadcrumbs = [b for b in breadcrumbs if b]

    owner = ad.get("owner") or {}
    business = owner.get("business") or {}
    verification = owner.get("verification") or {}
    avatar = owner.get("avatar") or ""

    result = {
        "id": ad.get("id") or ad_id,
        "url": f"https://999.md/ru/{ad.get('id') or ad_id}",
        "title": ad.get("title") or "",
        "state": ad.get("state") or "",
        "is_expired": ad.get("isExpired"),
        "posted": ad.get("posted") or "",
        "reseted": ad.get("reseted") or "",
        "expire": ad.get("expire") or "",
        "views_total": views.get("total"),
        "views_today": views.get("today"),
        "views_since_republish": views.get("sinceRepublish"),
        "category_path": breadcrumbs,
        "subcategory": (sub.get("title") or {}).get("translated") or "",
        "subcategory_id": sub.get("id"),
        "offer_type": text(1),
        "author_type": text(795),
        "price": price.get("value") if isinstance(price, dict) else price,
        "currency": unit(price.get("unit") or price.get("measurement")) if isinstance(price, dict) else "",
        "bargain": price.get("bargain") if isinstance(price, dict) else None,
        "price_mode": price.get("mode") if isinstance(price, dict) else None,
        "down_payment": price.get("down_payment") if isinstance(price, dict) else None,
        "price_per_m2": raw(1385),
        "old_price": old_price.get("value") if isinstance(old_price, dict) else old_price,
        "old_currency": unit(old_price.get("unit") or old_price.get("measurement")) if isinstance(old_price, dict) else "",
        "rooms": text(241),
        "area": area.get("value") if isinstance(area, dict) else area,
        "area_unit": unit(area.get("unit")) if isinstance(area, dict) else "",
        "floor": text(248),
        "floors_total": text(249),
        "housing_stock": text(852),
        "living_room": text(2199),
        "developer": text(1658),
        "region": text(7),
        "city": text(8),
        "district": SECTOR_RENAMES.get(text(9), text(9)),
        "street": text(10),
        "house": text(11),
        "lat": point.get("lat") if isinstance(point, dict) else None,
        "lon": point.get("lon") if isinstance(point, dict) else None,
        "map_url": (
            f"https://www.google.com/maps?q={point['lat']},{point['lon']}"
            if isinstance(point, dict) and point.get("lat") else ""
        ),
        "description": (body.get("translated") if isinstance(body, dict) else body) or "",
        "description_ru": body.get("ru", "") if isinstance(body, dict) else "",
        "description_ro": body.get("ro", "") if isinstance(body, dict) else "",
        "phones": contacts.get("phone_numbers", []) if isinstance(contacts, dict) else [],
        "photos": [f"{IMAGE_BASE}/900x900/{name}" for name in photos] if isinstance(photos, list) else [],
        "photos_thumbs": [f"{IMAGE_BASE}/320x240/{name}" for name in photos] if isinstance(photos, list) else [],
        "owner": {
            "id": owner.get("id") or "",
            "login": owner.get("login") or "",
            "registered": owner.get("createdDate") or "",
            "avatar_url": f"{AVATAR_BASE}/200x200/{avatar}" if avatar else "",
            "is_business": bool(business.get("id")),
            "business_plan": business.get("plan") or "",
            "marketplace": business.get("marketplace"),
            "has_delivery": business.get("hasDelivery"),
            "is_verified": verification.get("isVerified"),
            "verified_date": verification.get("date") or "",
            "is_deleted": owner.get("isDeleted"),
            "profile_url": f"https://999.md/ru/profile/{owner.get('login')}" if owner.get("login") else "",
        },
        "features": {item["title"]: SECTOR_RENAMES.get(item["text"], item["text"])
                     if item["title"] == "Сектор" else item["text"]
                     for g in groups for item in g["items"] if item["title"]},
        "groups": groups,
    }
    return result


def to_csv_row(ad: dict) -> dict:
    owner = ad["owner"]
    return {
        "id": ad["id"],
        "url": ad["url"],
        "title": ad["title"],
        "state": ad["state"],
        "posted": ad["posted"],
        "reseted": ad["reseted"],
        "expire": ad["expire"],
        "is_expired": ad["is_expired"],
        "views_total": ad["views_total"],
        "views_today": ad["views_today"],
        "views_since_republish": ad["views_since_republish"],
        "category": " / ".join(ad["category_path"]),
        "subcategory": ad["subcategory"],
        "subcategory_id": ad["subcategory_id"],
        "offer_type": ad["offer_type"],
        "author_type": ad["author_type"],
        "price": ad["price"],
        "currency": ad["currency"],
        "bargain": ad["bargain"],
        "price_per_m2": ad["price_per_m2"],
        "old_price": ad["old_price"],
        "old_currency": ad["old_currency"],
        "rooms": ad["rooms"],
        "area": f"{ad['area']} {ad['area_unit']}".strip() if ad["area"] is not None else "",
        "floor": ad["floor"],
        "floors_total": ad["floors_total"],
        "housing_stock": ad["housing_stock"],
        "living_room": ad["living_room"],
        "developer": ad["developer"],
        "region": ad["region"],
        "city": ad["city"],
        "district": ad["district"],
        "street": ad["street"],
        "house": ad["house"],
        "lat": ad["lat"],
        "lon": ad["lon"],
        "owner_login": owner["login"],
        "owner_id": owner["id"],
        "owner_registered": owner["registered"],
        "owner_is_business": owner["is_business"],
        "owner_business_plan": owner["business_plan"],
        "owner_verified": owner["is_verified"],
        "phones": ", ".join(ad["phones"]),
        "photos_count": len(ad["photos"]),
        "photos": " ".join(ad["photos"]),
        "description": ad["description"],
    }


def show(ad: dict) -> None:
    owner = ad["owner"]
    print(f"\n{ad['title']}")
    print(f"{ad['url']}   [{ad['state']}]")
    print("-" * 78)
    price = f"{ad['price']} {ad['currency']}" if ad["price"] is not None else "—"
    if ad["bargain"]:
        price += " (торг)"
    if ad["old_price"]:
        price += f"   было: {ad['old_price']} {ad['old_currency']}"
    print(f"Цена           : {price}")
    if ad["price_per_m2"]:
        print(f"Цена за m²     : {ad['price_per_m2']}")
    print(f"Категория      : {' / '.join(ad['category_path'])}")
    address = ", ".join(x for x in (ad["region"], ad["city"], ad["district"], ad["street"], ad["house"]) if x)
    print(f"Адрес          : {address or '—'}")
    if ad["lat"]:
        print(f"Координаты     : {ad['lat']}, {ad['lon']}   {ad['map_url']}")
    print(f"Размещено      : {ad['posted']}   обновлено: {ad['reseted']}   до: {ad['expire']}")
    print(f"Просмотры      : всего {ad['views_total']}, сегодня {ad['views_today']}")
    print(f"Телефоны       : {', '.join(ad['phones']) or '—'}")
    print(f"Автор          : {owner['login']} ({ad['author_type'] or '—'}), "
          f"с {owner['registered'] or '—'}, "
          f"{'бизнес ' + str(owner['business_plan']) if owner['is_business'] else 'частное лицо'}"
          f"{', верифицирован' if owner['is_verified'] else ''}")
    if owner["profile_url"]:
        print(f"Профиль        : {owner['profile_url']}")
    print(f"Фото           : {len(ad['photos'])}")
    for url in ad["photos"][:3]:
        print(f"                 {url}")
    if len(ad["photos"]) > 3:
        print(f"                 … ещё {len(ad['photos']) - 3}")

    print("\nХарактеристики:")
    for group in ad["groups"]:
        rendered = [i for i in group["items"] if i["text"] and i["id"] not in (13, 14, 15, 2562)]
        if not rendered:
            continue
        print(f"  [{group['group']}]")
        for item in rendered:
            print(f"    {item['title']:<26} {item['text'][:120]}")

    if ad["description"]:
        print("\nТекст объявления:")
        for line in ad["description"].splitlines():
            print(f"  {line}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Полная карточка объявления 999.md")
    parser.add_argument("target", help="ссылка вида https://999.md/ru/100237512 или просто id")
    parser.add_argument("--json", dest="json_out", default=None, help="файл JSON (по умолчанию ad-<id>.json)")
    parser.add_argument("--csv", dest="csv_out", default=None, help="дополнительно сохранить строку в CSV")
    parser.add_argument("--raw", action="store_true", help="сохранить сырой ответ API в ad-<id>.raw.json")
    parser.add_argument("--quiet", action="store_true", help="не печатать карточку в консоль")
    args = parser.parse_args()

    ad_id = ad_id_from(args.target)
    url = f"https://999.md/ru/{ad_id}"

    session = make_session(url)
    session.headers["referer"] = url

    data = fetch(ad_id, session)
    ad = parse(data, ad_id)

    if args.raw:
        raw_path = f"ad-{ad_id}.raw.json"
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        print(f"[✓] сырой ответ: {raw_path}", file=sys.stderr)

    json_path = args.json_out or f"ad-{ad_id}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(ad, fh, ensure_ascii=False, indent=2)
    print(f"[✓] JSON: {json_path}", file=sys.stderr)

    if args.csv_out:
        with open(args.csv_out, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerow(to_csv_row(ad))
        print(f"[✓] CSV: {args.csv_out}", file=sys.stderr)

    if not args.quiet:
        show(ad)
    return 0


if __name__ == "__main__":
    sys.exit(main())
