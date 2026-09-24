#!/usr/bin/env python3

from __future__ import annotations

import argparse
import difflib
import html as htmllib
import json
import math
import re
import statistics
import sys
import time
import unicodedata

import requests

import db
from scrape_999 import USER_AGENT

WMS = "https://geodata.gov.md/geoserver/contestare/wms"
LAYER = "S1"
EARTH_R = 6378137.0
BOX_HALF = 7.54
PAUSE = 0.5
RINGS = (12.0, 25.0)
DIRECTIONS = 8
TOLERANCES = (0.05, 0.10)
STREET_MIN = 0.6


def to_mercator(lat: float, lon: float) -> tuple[float, float]:
    x = math.radians(lon) * EARTH_R
    y = EARTH_R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def probe_points(lat: float, lon: float, rings=RINGS):
    x, y = to_mercator(lat, lon)
    k = 1 / math.cos(math.radians(lat))
    yield x, y
    for r in rings:
        for i in range(DIRECTIONS):
            a = 2 * math.pi * i / DIRECTIONS
            yield x + r * k * math.cos(a), y + r * k * math.sin(a)


def feature_info(session: requests.Session, x: float, y: float) -> list[dict]:
    params = {
        "service": "WMS", "version": "1.1.1", "request": "GetFeatureInfo",
        "exceptions": "application/json", "layers": LAYER, "query_layers": LAYER,
        "x": 51, "y": 51, "width": 101, "height": 101, "srs": "EPSG:3857",
        "bbox": f"{x - BOX_HALF},{y - BOX_HALF},{x + BOX_HALF},{y + BOX_HALF}",
        "feature_count": 10, "info_format": "application/json",
    }
    for attempt in range(3):
        try:
            resp = session.get(WMS, params=params, timeout=40)
            resp.raise_for_status()
            return resp.json().get("features") or []
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))
    return []


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*",
                            "Accept-Language": "ru,ro;q=0.9,en;q=0.8"})
    return session


SUMMARY_RE = re.compile(r"<summary[^>]*>.*?<strong>\s*([\d.]+)\s*</strong>", re.S)
FIELD_RE = re.compile(r"<li>\s*([^<:]+?)\s*:\s*<strong>(.*?)</strong>", re.S)
TAG_RE = re.compile(r"<[^>]+>")
UPDATED_RE = re.compile(r"Data actualiz[ăa]rii:\s*([\d.]+)")
APT_RE = re.compile(r"\bap\.\s*(\S+)\s*$", re.I)

BUILDING_FIELDS = {
    "adresa": "address", "clasificator": "classifier", "anul constr": "year_built",
    "numarul de etaje": "floors", "starea": "condition", "gaz": "gas", "materialul": "walls",
    "apa": "water", "canalizare": "sewer", "complet electrificat": "electrified",
    "valoarea estimata": "value_lei",
}
UNIT_FIELDS = {
    "adresa": "address", "suprafata": "area", "tipul": "kind", "etajul": "floor",
    "veceu": "wc", "baie": "bath", "ultimul etaj": "last_floor", "valoarea estimata": "value_lei",
}


def plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower().strip()


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(TAG_RE.sub("", value))).strip()


def to_int(value):
    digits = re.sub(r"[^\d]", "", value or "")
    return int(digits) if digits else None


def to_float(value):
    try:
        return float((value or "").replace(",", ".").strip())
    except ValueError:
        return None


def fields(segment: str, names: dict) -> tuple[dict, dict]:
    known, extra = {}, {}
    for label, value in FIELD_RE.findall(segment):
        label, value = clean(label), clean(value)
        key = next((col for prefix, col in names.items() if plain(label).startswith(prefix)), None)
        if key and key not in known:
            known[key] = value
        else:
            extra[label] = value
    return known, extra


def split_address(address: str) -> tuple[str, str]:
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if len(parts) >= 2 and re.fullmatch(r"\d[\w/-]*", parts[-1]):
        street, house = parts[-2], parts[-1]
    else:
        m = re.match(r"^(.*?)\s+(\d[\w/-]*)$", parts[-1] if parts else "")
        street, house = (m.group(1), m.group(2)) if m else ((parts[-1] if parts else ""), "")
    return re.sub(r"^sect\.?\s+\S+\s+", "", street), house


def parse_feature(feature: dict) -> dict | None:
    body = (feature.get("properties") or {}).get("html") or ""
    marks = list(SUMMARY_RE.finditer(body))
    if not marks:
        return None
    segments = [body[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(body))]
                for i, m in enumerate(marks)]
    known, extra = fields(segments[0], BUILDING_FIELDS)
    street, house = split_address(known.get("address", ""))
    updated = UPDATED_RE.search(body)
    building = {
        "code": marks[0].group(1), "address": known.get("address"), "street": street, "house": house,
        "classifier": known.get("classifier"), "year_built": to_int(known.get("year_built")),
        "floors": to_int(known.get("floors")), "condition": known.get("condition"),
        "walls": known.get("walls"), "gas": known.get("gas"), "water": known.get("water"),
        "sewer": known.get("sewer"), "electrified": known.get("electrified"),
        "value_lei": to_int(known.get("value_lei")),
        "updated": updated.group(1) if updated else None,
        "props_json": json.dumps(extra, ensure_ascii=False) if extra else None,
        "geometry": json.dumps(feature.get("geometry")) if feature.get("geometry") else None,
    }
    units = []
    for m, seg in zip(marks[1:], segments[1:]):
        u, _ = fields(seg, UNIT_FIELDS)
        apt = APT_RE.search(u.get("address", ""))
        units.append({
            "code": m.group(1), "building": building["code"], "address": u.get("address"),
            "apt": apt.group(1) if apt else None, "area": to_float(u.get("area")),
            "kind": u.get("kind"), "floor": to_int(u.get("floor")),
            "wc": u.get("wc"), "bath": u.get("bath"),
            "last_floor": {"da": 1, "nu": 0}.get(plain(u.get("last_floor", ""))),
            "value_lei": to_int(u.get("value_lei")),
        })
    return {"building": building, "units": units}


def save(conn, parsed: dict) -> None:
    b = dict(parsed["building"], fetched_at=db.now())
    conn.execute(f"INSERT OR REPLACE INTO cad_buildings ({', '.join(b)}) VALUES ({', '.join('?' * len(b))})",
                 list(b.values()))
    conn.execute("DELETE FROM cad_units WHERE building = ?", (b["code"],))
    for u in parsed["units"]:
        conn.execute(f"INSERT OR REPLACE INTO cad_units ({', '.join(u)}) VALUES ({', '.join('?' * len(u))})",
                     list(u.values()))


STREET_PREFIXES = {
    "str", "strada", "bd", "bld", "bul", "bulevardul", "sos", "soseaua", "pr", "prospectul",
    "str-la", "stradela", "al", "aleea", "pta", "piata", "tr", "trecerea", "fund", "fundac",
    "ул", "улица", "бул", "бульвар", "б-р", "шос", "шоссе", "пр", "пр-т", "проспект",
    "пер", "переулок", "пл", "площадь", "туп", "тупик", "мун", "mun", "or", "г",
}
RU_SOUNDS = [("дж", "J"), ("ч", "Ç"), ("ж", "J"), ("ш", "S"), ("щ", "S"), ("ц", "C"),
             ("х", "h"), ("кс", "ks")]
RU_LETTERS = dict(zip("бвгдзклмнпрстф", "bvgdzklmnprstf"))
RO_SOUNDS = [("che", "ke"), ("chi", "ki"), ("ghe", "Ge"), ("ghi", "Gi"),
             ("ce", "Çe"), ("ci", "Çi"), ("ge", "Je"), ("gi", "Ji"),
             ("j", "J"), ("x", "ks"), ("c", "k"), ("q", "k"), ("w", "v"), ("G", "g")]


def strip_marks(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


def street_skeleton(street: str) -> str:
    text = re.sub(r"[.,]", " ", (street or "").lower())
    words = [w for w in text.split() if w not in STREET_PREFIXES]
    out = []
    for word in words:
        if re.search(r"[а-яё]", word):
            for a, b in RU_SOUNDS:
                word = word.replace(a, b)
            word = "".join(RU_LETTERS.get(ch, ch) for ch in word)
        else:
            word = word.replace("ș", "S").replace("ş", "S").replace("ț", "C").replace("ţ", "C")
            word = strip_marks(word)
            for a, b in RO_SOUNDS:
                word = word.replace(a, b)
        word = re.sub(r"[aeiouyаеёиоуыэюяйьъ]", "", word)
        word = re.sub(r"(.)\1+", r"\1", word)
        if word:
            out.append(word)
    return "".join(out)


def street_score(a: str, b: str) -> float:
    sa, sb = street_skeleton(a), street_skeleton(b)
    if not sa or not sb:
        return 0.0
    if sa in sb or sb in sa:
        return 1.0
    return difflib.SequenceMatcher(None, sa, sb).ratio()


def norm_house(house: str) -> str:
    text = re.sub(r"\s+", "", (house or "").lower())
    return text.translate(str.maketrans("абвгдеж", "abvgdej"))


def pick_building(conn, codes: list[str], street: str, house: str):
    want = norm_house(house)
    best = None
    for code in codes:
        b = conn.execute("SELECT * FROM cad_buildings WHERE code = ?", (code,)).fetchone()
        if not b or norm_house(b["house"]) != want:
            continue
        score = street_score(street, b["street"])
        if score >= STREET_MIN and (best is None or score > best[1]):
            best = (b, score)
    return best[0] if best else None


def match_ad(conn, ad) -> dict | None:
    point = conn.execute("SELECT buildings FROM cad_points WHERE point = ?", (point_key(ad["lat"], ad["lon"]),)).fetchone()
    if not point or not point["buildings"]:
        return None
    building = pick_building(conn, json.loads(point["buildings"]), ad["street"], ad["house"])
    if not building or not ad["area"] or ad["floor"] is None:
        return None
    for tol in TOLERANCES:
        rows = conn.execute(
            "SELECT code, apt, area, floor, value_lei FROM cad_units "
            "WHERE building = ? AND floor = ? AND value_lei > 0 AND area BETWEEN ? AND ? ORDER BY area",
            (building["code"], ad["floor"], ad["area"] * (1 - tol), ad["area"] * (1 + tol))).fetchall()
        if rows:
            values = [r["value_lei"] for r in rows]
            return {"building": building["code"], "match": "address", "tolerance": tol, "n": len(rows),
                    "value_min": min(values), "value_med": round(statistics.median(values)),
                    "value_max": max(values), "units": json.dumps([r["code"] for r in rows])}
    return None


def point_key(lat: float, lon: float) -> str:
    return f"{lat:.6f},{lon:.6f}"


ADS_SQL = """
SELECT a.id, a.lat, a.lon, d.street, d.house, d.area, d.floor, d.housing_stock
FROM ads a JOIN ad_details d ON d.id = a.id
WHERE a.profile = 'apartments' AND a.gone_at IS NULL AND a.lat IS NOT NULL AND d.house <> ''
"""


def survey_point(conn, session, lat: float, lon: float, streets_houses: list[tuple[str, str]],
                 rings=RINGS) -> tuple[str, int]:
    seen, probes = [], 0
    for x, y in probe_points(lat, lon, rings):
        probes += 1
        for feature in feature_info(session, x, y):
            parsed = parse_feature(feature)
            if parsed and parsed["building"]["code"] not in seen:
                save(conn, parsed)
                seen.append(parsed["building"]["code"])
        time.sleep(PAUSE)
        if seen and any(pick_building(conn, seen, s, h) for s, h in streets_houses):
            status = "found"
            break
    else:
        status = "nomatch" if seen else "empty"
    conn.execute("INSERT OR REPLACE INTO cad_points (point, buildings, status, probes, fetched_at) "
                 "VALUES (?, ?, ?, ?, ?)", (point_key(lat, lon), json.dumps(seen), status, probes, db.now()))
    conn.commit()
    return status, probes


def rematch(conn, ids: list[str] | None = None) -> tuple[int, int]:
    sql = ADS_SQL + (f" AND a.id IN ({', '.join('?' * len(ids))})" if ids else "")
    found = total = 0
    for ad in conn.execute(sql, ids or []).fetchall():
        total += 1
        result = match_ad(conn, ad)
        if result:
            found += 1
            conn.execute(f"INSERT OR REPLACE INTO ad_cadastre (ad_id, {', '.join(result)}, matched_at) "
                         f"VALUES (?, {', '.join('?' * len(result))}, ?)", [ad["id"], *result.values(), db.now()])
        else:
            conn.execute("DELETE FROM ad_cadastre WHERE ad_id = ?", (ad["id"],))
    conn.commit()
    return found, total


def main() -> int:
    parser = argparse.ArgumentParser(description="Кадастровая оценка IPCBI")
    parser.add_argument("--limit", type=int, default=0, help="не больше стольких точек за запуск")
    parser.add_argument("--refresh", type=int, default=0, help="переспросить точки старше N дней")
    parser.add_argument("--match-only", action="store_true", help="без сети, только сопоставить")
    parser.add_argument("--ad", help="одно объявление, с подробным выводом")
    args = parser.parse_args()

    conn = db.connect()
    ads = conn.execute(ADS_SQL + (" AND a.id = ?" if args.ad else ""), [args.ad] if args.ad else []).fetchall()
    points: dict[str, dict] = {}
    for ad in ads:
        p = points.setdefault(point_key(ad["lat"], ad["lon"]),
                              {"lat": ad["lat"], "lon": ad["lon"], "addr": set(), "new": True})
        p["addr"].add((ad["street"] or "", ad["house"] or ""))
        p["new"] = p["new"] and ad["housing_stock"] == "Новострой"

    if not args.match_only:
        done = {r["point"]: r["fetched_at"] for r in conn.execute("SELECT point, fetched_at FROM cad_points")}
        cutoff = None
        if args.refresh:
            cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - args.refresh * 86400))
        todo = [k for k in points if args.ad or k not in done or (cutoff and done[k] < cutoff)]
        if args.limit:
            todo = todo[:args.limit]
        session = make_session()
        stats = {"found": 0, "nomatch": 0, "empty": 0, "error": 0}
        started = time.time()
        for n, key in enumerate(todo, 1):
            p = points[key]
            try:
                rings = RINGS[:1] if p["new"] else RINGS
                status, probes = survey_point(conn, session, p["lat"], p["lon"], sorted(p["addr"]), rings)
            except requests.RequestException as exc:
                status, probes = "error", 0
                print(f"  {key}: {exc}", file=sys.stderr)
            stats[status] += 1
            if args.ad or n % 25 == 0 or n == len(todo):
                rate = n / max(time.time() - started, 1)
                print(f"точек {n}/{len(todo)} · {stats} · {rate:.2f}/с", flush=True)

    found, total = rematch(conn, [args.ad] if args.ad else None)
    print(f"сопоставлено объявлений: {found} из {total}")
    if args.ad:
        row = conn.execute("SELECT * FROM ad_cadastre WHERE ad_id = ?", (args.ad,)).fetchone()
        print(json.dumps(dict(row), ensure_ascii=False, indent=1) if row else "нет результата")
    return 0


if __name__ == "__main__":
    sys.exit(main())
