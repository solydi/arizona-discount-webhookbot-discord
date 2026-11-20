import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

URL = "https://arz-wiki.com/arz-rp/articles/donate-items-percent/"
WEBHOOK = "https://discord.com/api/webhooks/1572892916094079086/yWXO_4xZlghF6ejfUzADY1dCLcSr2ze_i2aQqL0fRPC4z1SUiR8zbK1yBIE5Fw0krus0"
ROLE_ID = "1572917159556939776"  # роль, которую нужно упоминать
TOP_N = 30
ONLY_TEMPORARY = True
SKIP_EXPIRED = True  # не показывать скидки, у которых срок уже истёк (кеш сайта мог устареть)

MIN_PERCENT = 20  # скидки ниже этого значения не показываем...
ALWAYS_SHOW_RAW = """
VIRGIN MOON
"""  # ...кроме этих предметов

BLACKLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blacklist.txt")

MSK = timezone(timedelta(hours=3))

SERVERS = {
    1: "Phoenix 🔥", 2: "Tucson 🌵", 3: "Scottdale ⚔️", 4: "Chandler ☃️",
    5: "Brainburg 🧠", 6: "Saint Rose 🥀", 7: "Mesa 🥴", 8: "Red-Rock 🌋",
    9: "Yuma 🦅", 10: "Surprise 🎁", 11: "Prescott 🏜️", 12: "Glendale 💀",
    13: "Kingman 👑", 14: "Winslow 🚂", 15: "Payson 🏔️", 16: "Gilbert 💫",
    17: "Show Low 💥", 18: "Casa-Grande 🏠", 19: "Page 💸", 20: "Sun-City ☀️",
    21: "Queen-Creek 👸", 22: "Sedona 🔴", 23: "Holiday 🏝️", 24: "Wednesday 🦇",
    25: "Yava 🪶", 26: "Faraway 🌌", 27: "Bumble Bee 🐝", 28: "Christmas ❄️",
    29: "Mirage 🌵", 30: "Love ❤️", 31: "Drake 🦉", 32: "Space 👽", 33: "Home 🏠",
}


def norm(s: str) -> str:
    s = s.casefold().replace("ё", "е")
    return re.sub(r"[^\w]|_", "", s)


ALWAYS_SHOW = {norm(x) for x in ALWAYS_SHOW_RAW.splitlines() if x.strip()}


def load_blacklist():
    """Читает blacklist.txt при каждой проверке, перезапуск после правок не нужен."""
    exact, parts = set(), []
    try:
        with open(BLACKLIST_FILE, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("~"):
                    p = norm(line[1:])
                    if p:
                        parts.append(p)
                else:
                    n = norm(line)
                    if n:
                        exact.add(n)
    except FileNotFoundError:
        print(f"Файл {BLACKLIST_FILE} не найден: исключения не применяются")
    return exact, parts


def is_blocked(name: str, exact, parts) -> bool:
    n = norm(name)
    return n in exact or any(p in n for p in parts)


def parse_servers(td):
    """Серверы лежат в атрибуте title: «Серверы: 2, 10» (или текстом «1–32»)."""
    el = td.select_one("[title]")
    title = el.get("title", "") if el else ""
    nums = re.findall(r"\d+", title.split(":", 1)[-1]) if title else []
    if not nums:
        txt = td.get_text(" ", strip=True)
        rng = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", txt)
        if rng:
            nums = [str(i) for i in range(int(rng.group(1)), int(rng.group(2)) + 1)]
        else:
            nums = re.findall(r"\d+", txt)
    return [int(n) for n in nums]


def parse_price(td):
    """Первая цена (жирная) = цена со скидкой."""
    b = td.select_one(".arz-dt-price b") or td.find("b")
    txt = b.get_text(" ", strip=True) if b else td.get_text(" ", strip=True)
    pm = re.search(r"([\d\s\u00a0\u202f]+?)\s*(руб|az)", txt, re.I)
    if not pm:
        return "—"
    num = re.sub(r"[\s\u00a0\u202f]+", " ", pm.group(1)).strip()
    return f"{num} ₽" if pm.group(2).lower() == "руб" else f"{num} AZ"


def debug_page(resp, soup):
    html = resp.text
    flat = lambda s: re.sub(r"\s+", " ", s)
    print("--- ДИАГНОСТИКА ---")
    print("HTTP:", resp.status_code, "| размер:", len(html))
    print("table:", len(soup.find_all("table")), "| tr:", len(soup.find_all("tr")),
          "| td:", len(soup.find_all("td")))
    tr = soup.select_one("table tbody tr")
    if tr:
        print("Первая строка таблицы:", flat(str(tr))[:900])
    print("--- КОНЕЦ ДИАГНОСТИКИ ---")


def fetch_rows():
    exact, parts = load_blacklist()
    print(f"Blacklist: {len(exact)} точных, {len(parts)} по части названия")

    resp = requests.get(
        URL, headers={"User-Agent": "Mozilla/5.0 (discount-bot)"}, timeout=20
    )
    soup = BeautifulSoup(resp.text, "html.parser")

    upd = re.search(r"последнее обновление:\s*([\d\-]+ [\d:]+)", soup.get_text(" ", strip=True))
    print("Таблица на сайте обновлена:", upd.group(1) if upd else "не удалось определить")

    now = datetime.now(MSK)
    stats = {"найдено": 0, "в blacklist": 0, f"ниже {MIN_PERCENT}%": 0,
             "постоянные": 0, "просрочены": 0}
    blocked_samples = []

    rows = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 5:  # заголовок или рекламная строка
            continue

        # колонки: Название | Категория | Сервер | Цена+Скидка | Временный
        name = tds[0].get_text(" ", strip=True)
        category = tds[1].get_text(" ", strip=True)
        servers = parse_servers(tds[2])

        disc_el = tds[3].select_one(".arz-dt-disc")
        pm = re.search(r"(\d+)\s*%", (disc_el or tds[3]).get_text(" ", strip=True))
        if not name or not pm:
            continue
        percent = int(pm.group(1))
        price = parse_price(tds[3])
        stats["найдено"] += 1

        if is_blocked(name, exact, parts):
            stats["в blacklist"] += 1
            if len(blocked_samples) < 5 and name not in blocked_samples:
                blocked_samples.append(name)
            continue

        if percent < MIN_PERCENT and norm(name) not in ALWAYS_SHOW:
            stats[f"ниже {MIN_PERCENT}%"] += 1
            continue

        dm = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})", tds[4].get_text(" ", strip=True))
        if ONLY_TEMPORARY and not dm:
            stats["постоянные"] += 1
            continue

        if SKIP_EXPIRED and dm:
            d, mo, y, h, mi = (int(x) for x in dm.groups())
            if datetime(y, mo, d, h, mi, tzinfo=MSK) < now:
                stats["просрочены"] += 1
                continue

        for n in servers:
            rows.append({
                "percent": percent,
                "name": name,
                "category": category,
                "server": f"№{n} {SERVERS.get(n, '')}".strip(),
                "price": price,
            })

    # оставляем по одной строке на предмет: с максимальной скидкой
    best = {}
    for r in rows:
        key = norm(r["name"])
        cur = best.get(key)
        if cur is None or r["percent"] > cur["percent"]:
            best[key] = {**r, "extra": 0}
        elif r["percent"] == cur["percent"]:
            cur["extra"] += 1

    rows = list(best.values())
    for r in rows:
        if r["extra"]:
            r["server"] += f" +{r['extra']}"

    rows.sort(key=lambda r: r["percent"], reverse=True)

    print("Отсев по шагам:", stats, "| итого к публикации:", len(rows))
    if not rows:
        if blocked_samples:
            print("Примеры из blacklist:", blocked_samples)
        if stats["найдено"] == 0:
            debug_page(resp, soup)
    return rows[:TOP_N]


def post(rows):
    blocks = []
    total = 0
    for r in rows:
        block = (
            f"🏷️ **{ r['name'][:60] or '—'}**\n"
            f"➡️ {r['server']} · 💰 {r['price']} · 🔻 **{r['percent']}%**"
        )
        if total + len(block) + 2 > 3800:  # лимит description у embed = 4096
            break
        blocks.append(block)
        total += len(block) + 2

    payload = {
        "content": f"<@&{ROLE_ID}>",
        "allowed_mentions": {"parse": [], "roles": [ROLE_ID]},
        "embeds": [{
            "title": "🔥 Донат-скидки Arizona RP",
            "url": "https://arizona-rp.com/",
            "color": 0x2ECC71,
            "description": "\n\n".join(blocks),
            "footer": {"text": "by solodi"},
        }]
    }
    resp = requests.post(WEBHOOK, json=payload, timeout=20)
    if not resp.ok:
        print("Discord error:", resp.status_code, resp.text)


def wait_until(hour=7, minute=10):
    now = datetime.now(MSK)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    time.sleep((target - now).total_seconds())


if __name__ == "__main__":
    while True:
        try:
            rows = fetch_rows()
            if rows:
                print(rows[:3])
                post(rows)
            else:
                print("Нечего постить: все строки отсеяны (см. вывод выше)")
        except Exception as e:
            print("error:", e)
        wait_until()