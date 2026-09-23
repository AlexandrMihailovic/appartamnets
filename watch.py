#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

import db

HERE = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(HERE, "watch.pid")
LOG_FILE = os.path.join(HERE, "watch.log")

_stop = False


def on_signal(signum, frame):
    global _stop
    _stop = True
    print(f"\n[i] получен сигнал {signum}, завершаю после текущего шага…", flush=True)


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def read_pid() -> int | None:
    if not os.path.exists(PID_FILE):
        return None
    try:
        pid = int(open(PID_FILE).read().strip())
    except (ValueError, OSError):
        return None
    return pid if alive(pid) else None


def run_step(name: str, args: list[str]) -> bool:
    print(f"\n── {name} ── {stamp()}", flush=True)
    started = time.time()
    try:
        result = subprocess.run(
            [sys.executable, "-u", *args], cwd=HERE,
            stdout=sys.stdout, stderr=subprocess.STDOUT, timeout=3600,
        )
    except subprocess.TimeoutExpired:
        print(f"[!] {name}: превышен час, шаг прерван", flush=True)
        return False
    except Exception as exc:
        print(f"[!] {name}: {type(exc).__name__}: {exc}", flush=True)
        return False

    took = time.time() - started
    if result.returncode != 0:
        print(f"[!] {name}: код возврата {result.returncode} ({took:.0f} с)", flush=True)
        return False
    print(f"[✓] {name}: готово за {took:.0f} с", flush=True)
    return True


def summary(since: str) -> None:
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM runs WHERE finished >= ? ORDER BY id", (since,)).fetchall()
    if not rows:
        print("[i] сводка: сборщик не отметился", flush=True)
    for row in rows:
        print(f"[итог] {row['profile']:<11} в выдаче {row['total']:>6}   "
              f"новых {row['added']:>4}   ушли {row['gone']:>4}   "
              f"подешевели {row['price_down']:>4}   подорожали {row['price_up']:>4}", flush=True)

    fresh = conn.execute(
        "SELECT a.profile p, COUNT(*) c FROM ads a WHERE a.first_seen >= ? GROUP BY 1", (since,)).fetchall()
    for row in fresh:
        for item in conn.execute(
            "SELECT id, title, price, currency FROM ads WHERE profile = ? AND first_seen >= ? "
            "ORDER BY price LIMIT 5", (row["p"], since)
        ):
            price = f"{item['price']:.0f} {item['currency']}" if item["price"] else "—"
            print(f"    + {item['id']}  {price:>14}  {item['title'][:64]}", flush=True)

    drops = conn.execute(
        "SELECT id, title, price, prev_price, currency FROM ads "
        "WHERE price_changed_at >= ? AND prev_price > price ORDER BY (price - prev_price) LIMIT 5",
        (since,)).fetchall()
    for item in drops:
        delta = item["price"] - item["prev_price"]
        pct = delta / item["prev_price"] * 100 if item["prev_price"] else 0
        print(f"    ↓ {item['id']}  {item['prev_price']:.0f} → {item['price']:.0f} {item['currency']} "
              f"({delta:+.0f}, {pct:+.1f}%)  {item['title'][:48]}", flush=True)
    conn.close()


def cycle(number: int, details_limit: int | None, workers: int) -> None:
    since = db.now()
    print("\n" + "═" * 78, flush=True)
    print(f"ЦИКЛ {number} · старт {stamp()}", flush=True)
    print("═" * 78, flush=True)

    if not run_step("collect.py — список объявлений", ["collect.py"]):
        return
    if _stop:
        return

    details = ["details.py", "--workers", str(workers)]
    if details_limit:
        details += ["--limit", str(details_limit)]
    run_step("details.py — карточки новых лотов", details)
    if _stop:
        return

    run_step("details.py --changed — карточки после смены цены",
             ["details.py", "--changed", "--workers", str(workers)])

    print(f"\n── сводка цикла {number} ──", flush=True)
    summary(since)


def main() -> int:
    parser = argparse.ArgumentParser(description="Фоновое обновление данных 999.md")
    parser.add_argument("--interval", type=float, default=30, help="пауза между циклами, минут (по умолчанию 30)")
    parser.add_argument("--once", action="store_true", help="один цикл и выход")
    parser.add_argument("--workers", type=int, default=6, help="потоков для сбора карточек")
    parser.add_argument("--details-limit", type=int, default=None,
                        help="не больше N карточек за цикл (защита от долгих прогонов)")
    parser.add_argument("--status", action="store_true", help="показать состояние и выйти")
    parser.add_argument("--stop", action="store_true", help="остановить фоновый процесс и выйти")
    args = parser.parse_args()

    running = read_pid()

    if args.status:
        if running:
            age = time.time() - os.path.getmtime(PID_FILE)
            print(f"[✓] работает, pid {running}, запущен {age/3600:.1f} ч назад")
            print(f"    лог: tail -f {LOG_FILE}")
        else:
            print("[i] не запущен")
        conn = db.connect()
        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            print(f"    последний сбор: {row['finished']} ({row['profile']}, найдено {row['total']})")
        for key, value in db.stats(conn).items():
            print(f"    {key}: {value}")
        return 0

    if args.stop:
        if not running:
            print("[i] нечего останавливать")
            return 0
        os.kill(running, signal.SIGTERM)
        for _ in range(50):
            if not alive(running):
                break
            time.sleep(0.2)
        print(f"[✓] процесс {running} остановлен" if not alive(running)
              else f"[!] процесс {running} ещё жив, добейте: kill -9 {running}")
        return 0

    if running:
        print(f"[!] уже запущен, pid {running}. Остановить: python3 watch.py --stop")
        return 1

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    with open(PID_FILE, "w") as fh:
        fh.write(str(os.getpid()))

    print(f"[✓] обновление запущено, pid {os.getpid()}, интервал {args.interval:g} мин", flush=True)
    print(f"    остановить: python3 watch.py --stop", flush=True)

    number = 0
    try:
        while not _stop:
            number += 1
            try:
                cycle(number, args.details_limit, args.workers)
            except Exception as exc:
                print(f"[!] цикл {number} упал: {type(exc).__name__}: {exc}", flush=True)

            if args.once or _stop:
                break

            nxt = datetime.now() + timedelta(minutes=args.interval)
            print(f"\n[i] сон до {nxt.strftime('%H:%M:%S')}", flush=True)
            deadline = time.time() + args.interval * 60
            while time.time() < deadline and not _stop:
                time.sleep(1)
    finally:
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    print(f"\n[✓] остановлено, циклов выполнено: {number}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
