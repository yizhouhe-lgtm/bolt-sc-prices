#!/usr/bin/env python3
"""
BOLT SC — 原材料价格监控 Dashboard 生成器 v2
================================================
数据源:
  - 铝/铜: 世铝网 cnal.com 转载长江有色报价 (requests)
  - 钕:    dailymetalprice.com 国际钕价 (requests, USD→CNY)
  - PC/ABS/硅钢: 生意社 100ppi.com (Playwright 无头浏览器, 绕过JS反爬)

用法:
  python generate_dashboard.py              # 爬取真实价格
  python generate_dashboard.py --test       # 用模拟数据测试
"""

import os
import re
import json
import time
import logging
import datetime
import argparse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
#  Material Definitions
# ============================================================

MATERIALS = [
    {
        "en": "Aluminum A00",
        "desc": "A00 铝锭, 长江有色均价",
        "unit": "CNY/t",
        "cat": "Non-Ferrous",
        "icon": "🔩",
        "color": "#3068a8",
        "source_name": "长江有色",
        "fetch_method": "cnal",
        "fetch_key": "A00铝",
    },
    {
        "en": "Copper 1#",
        "desc": "1# 电解铜, 长江有色均价",
        "unit": "CNY/t",
        "cat": "Non-Ferrous",
        "icon": "⚡",
        "color": "#b86e2c",
        "source_name": "长江有色",
        "fetch_method": "cnal",
        "fetch_key": "1# 铜",
    },
    {
        "en": "Neodymium (Nd)",
        "desc": "金属钕, 国际现货 (USD→CNY)",
        "unit": "CNY/kg",
        "cat": "Rare Earth",
        "icon": "🧲",
        "color": "#6e4fa0",
        "source_name": "DailyMetalPrice",
        "fetch_method": "dailymetal",
        "fetch_key": "nd",
    },
    {
        "en": "PC Resin",
        "desc": "注塑级, 华东现货",
        "unit": "CNY/t",
        "cat": "Plastics",
        "icon": "♻️",
        "color": "#1e806c",
        "source_name": "生意社",
        "fetch_method": "syi_pw",
        "fetch_key": "386",
    },
    {
        "en": "ABS Resin",
        "desc": "通用级, 华东现货",
        "unit": "CNY/t",
        "cat": "Plastics",
        "icon": "♻️",
        "color": "#1e806c",
        "source_name": "生意社",
        "fetch_method": "syi_pw",
        "fetch_key": "381",
    },
    {
        "en": "Silicon Steel (Oriented)",
        "desc": "取向硅钢 0.3mm, 华东现货",
        "unit": "CNY/t",
        "cat": "Steel",
        "icon": "🏗️",
        "color": "#7a6540",
        "source_name": "生意社",
        "fetch_method": "syi_pw",
        "fetch_key": "1045",
    },
]


# ============================================================
#  Price Fetcher
# ============================================================

class PriceFetcher:

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._cnal_cache = None  # 世铝网只抓一次
        self._pw_browser = None
        self._pw_context = None

    def close(self):
        if self._pw_browser:
            self._pw_browser.close()
            self._pw_browser = None

    # ----------------------------------------------------------
    #  世铝网 cnal.com — 铝 & 铜 (长江有色报价)
    # ----------------------------------------------------------

    def _fetch_cnal_page(self):
        """抓取世铝网长江有色基本金属行情页，返回解析后的价格字典"""
        if self._cnal_cache is not None:
            return self._cnal_cache

        result = {}
        try:
            # 先获取列表页，找到今天的行情链接
            list_url = "https://market.cnal.com/changjiang/"
            resp = self.session.get(list_url, timeout=20)
            resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.text, "html.parser")

            # 找 "长江有色基本金属行情" 链接 (当天或最近一天)
            detail_url = None
            for a in soup.find_all("a", href=True):
                if "基本金属行情" in a.get_text():
                    href = a["href"]
                    if not href.startswith("http"):
                        href = "https://market.cnal.com" + href
                    detail_url = href
                    break

            if not detail_url:
                logger.warning("cnal: 未找到基本金属行情链接")
                self._cnal_cache = result
                return result

            logger.info(f"  cnal: fetching {detail_url}")
            resp2 = self.session.get(detail_url, timeout=20)
            resp2.encoding = "utf-8"
            soup2 = BeautifulSoup(resp2.text, "html.parser")

            # 解析表格: 金属类别 | 价格区间 | 日均价 | 涨跌
            for table in soup2.find_all("table"):
                rows = table.find_all("tr")
                for row in rows:
                    cells = row.find_all("td")
                    if len(cells) >= 4:
                        name = cells[0].get_text(strip=True)
                        avg_price_text = cells[2].get_text(strip=True).replace(",", "")
                        change_text = cells[3].get_text(strip=True).replace(",", "")
                        try:
                            price = float(avg_price_text)
                            change = float(change_text) if change_text else 0
                            result[name] = {"price": price, "change": change}
                        except ValueError:
                            continue

            if result:
                logger.info(f"  cnal: 解析到 {len(result)} 种金属")
            else:
                logger.warning("  cnal: 表格解析失败，尝试文本搜索")
                text = soup2.get_text()
                # Fallback: 从文本中搜索 "A00铝 ... 25080" 模式
                for pattern_name in ["A00铝", "1# 铜", "1#铜"]:
                    m = re.search(
                        pattern_name + r"[^\d]*?([\d,]+)\s*[-–]\s*([\d,]+)\s+([\d,]+)",
                        text,
                    )
                    if m:
                        avg = float(m.group(3).replace(",", ""))
                        result[pattern_name] = {"price": avg, "change": 0}

        except Exception as e:
            logger.warning(f"cnal fetch failed: {e}")

        self._cnal_cache = result
        return result

    def fetch_cnal(self, key: str) -> dict | None:
        """从世铝网获取铝/铜价格"""
        data = self._fetch_cnal_page()
        # key 可能是 "A00铝" 或 "1# 铜"，做模糊匹配
        for name, info in data.items():
            if key.replace(" ", "") in name.replace(" ", ""):
                return {"price": info["price"], "change_pct": 0}
        # 宽松匹配
        for name, info in data.items():
            if key[:2] in name:
                return {"price": info["price"], "change_pct": 0}
        return None

    # ----------------------------------------------------------
    #  DailyMetalPrice.com — 钕 (国际价, CNY/kg)
    # ----------------------------------------------------------

    def fetch_dailymetal(self, metal_code: str) -> dict | None:
        """从 dailymetalprice.com 获取金属价格 (CNY/kg)"""
        url = (
            f"https://www.dailymetalprice.com/metalprices.php"
            f"?c={metal_code}&u=kg&d=1&x=CNY"
        )
        try:
            resp = self.session.get(url, timeout=15)
            resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.text, "html.parser")

            table = soup.find("table")
            if table:
                rows = table.find_all("tr")
                for row in rows[1:]:  # 跳过表头
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        price_text = cells[0].get_text(strip=True)
                        # 移除货币符号，处理逗号
                        price_text = re.sub(r"[¥,$€£]", "", price_text)
                        price_text = price_text.replace(",", "").strip()
                        price = float(price_text)
                        if price > 0:
                            return {"price": round(price, 2), "change_pct": 0}

            # Fallback: 尝试 USD 版本然后换算
            url_usd = (
                f"https://www.dailymetalprice.com/metalprices.php"
                f"?c={metal_code}&u=kg&d=1&x=USD"
            )
            resp2 = self.session.get(url_usd, timeout=15)
            soup2 = BeautifulSoup(resp2.text, "html.parser")
            table2 = soup2.find("table")
            if table2:
                for row in table2.find_all("tr")[1:]:
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        price_text = cells[0].get_text(strip=True)
                        price_text = re.sub(r"[¥,$€£]", "", price_text)
                        price_text = price_text.replace(",", "").strip()
                        price_usd = float(price_text)
                        if price_usd > 0:
                            # 粗略汇率 USD→CNY
                            cny = round(price_usd * 7.1, 2)
                            logger.info(f"  dailymetal: USD {price_usd} → CNY {cny}")
                            return {"price": cny, "change_pct": 0}

        except Exception as e:
            logger.warning(f"dailymetal fetch failed for {metal_code}: {e}")
        return None

    # ----------------------------------------------------------
    #  生意社 100ppi — PC/ABS/硅钢 (Playwright 无头浏览器)
    # ----------------------------------------------------------

    def _get_browser(self):
        """懒加载 Playwright 浏览器"""
        if self._pw_browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._pw_browser = self._pw.chromium.launch(headless=True)
            self._pw_context = self._pw_browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
            )
        return self._pw_context

    def fetch_syi_pw(self, product_id: str) -> dict | None:
        """用 Playwright 无头浏览器从 100ppi.com 获取价格"""
        url = f"https://www.100ppi.com/price/detail-{product_id}.html"
        try:
            ctx = self._get_browser()
            page = ctx.new_page()
            logger.info(f"  playwright: loading {url}")
            page.goto(url, wait_until="networkidle", timeout=30000)
            # 等安全检查通过，最多等 15 秒
            page.wait_for_timeout(3000)

            html = page.content()
            page.close()

            soup = BeautifulSoup(html, "html.parser")
            price = None

            # 策略 1: 从表格找价格
            for table in soup.find_all("table"):
                rows = table.find_all("tr")
                if len(rows) > 1:
                    cells = rows[1].find_all("td")
                    if len(cells) >= 2:
                        nums = re.findall(
                            r"[\d,]+\.?\d*",
                            cells[1].get_text().replace(",", ""),
                        )
                        if nums:
                            val = float(nums[0])
                            if val > 100:  # 价格应该 > 100
                                price = val
                                break

            # 策略 2: 文本搜索
            if price is None:
                text = soup.get_text()
                matches = re.findall(
                    r"(?:价格|均价|报价)[：:\s]*(\d[\d,.]+)", text
                )
                for m in matches:
                    val = float(m.replace(",", ""))
                    if val > 100:
                        price = val
                        break

            # 策略 3: 找所有看起来像价格的大数字
            if price is None:
                text = soup.get_text()
                candidates = re.findall(r"\b(\d{4,6}(?:\.\d{1,2})?)\b", text)
                # 按大小排序，取最合理的
                nums = sorted([float(c) for c in candidates if 1000 < float(c) < 500000], reverse=True)
                if nums:
                    # 取中位数附近的值，避免极端值
                    price = nums[len(nums) // 2]

            if price:
                return {"price": price, "change_pct": 0}
            else:
                logger.warning(f"  playwright: 未能从页面解析出价格")
                # 打印一些调试信息
                text = soup.get_text()[:500]
                logger.info(f"  page text preview: {text[:200]}")

        except Exception as e:
            logger.warning(f"playwright fetch failed for {product_id}: {e}")
        return None

    # ----------------------------------------------------------
    #  汇总接口
    # ----------------------------------------------------------

    def fetch_price(self, material: dict) -> dict:
        """获取单个材料的当日价格"""
        method_name = material["fetch_method"]
        key = material["fetch_key"]
        result = None

        if method_name == "cnal":
            result = self.fetch_cnal(key)
        elif method_name == "dailymetal":
            result = self.fetch_dailymetal(key)
        elif method_name == "syi_pw":
            result = self.fetch_syi_pw(key)

        now = datetime.datetime.now()
        return {
            "en": material["en"],
            "desc": material["desc"],
            "price": result["price"] if result else 0,
            "unit": material["unit"],
            "change": result.get("change_pct", 0) if result else 0,
            "cat": material["cat"],
            "src": material["source_name"],
            "quoteDate": now.strftime("%Y-%m-%d"),
            "icon": material["icon"],
            "color": material["color"],
            "fetched": result is not None,
        }

    def fetch_all(self) -> list[dict]:
        """获取所有材料价格"""
        results = []
        for mat in MATERIALS:
            logger.info(f"Fetching: {mat['en']} ({mat['fetch_method']}) ...")
            data = self.fetch_price(mat)
            status = "✅" if data["fetched"] else "❌"
            logger.info(f"  {status} {data['en']}: {data['price']} {data['unit']}")
            results.append(data)
        self.close()
        return results


# ============================================================
#  History Manager
# ============================================================

HISTORY_FILE = "price_history.json"
MONTHLY_HISTORY_FILE = "monthly_history.json"

def load_json(path):
    if Path(path).exists():
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def update_history(prices: list[dict]) -> list[dict]:
    history = load_json(HISTORY_FILE)
    for p in prices:
        key = p["en"]
        if key not in history:
            history[key] = []
        if p["price"] > 0:
            history[key].append(p["price"])
        history[key] = history[key][-8:]
        hist = history[key]
        while len(hist) < 8:
            hist.insert(0, hist[0] if hist else 0)
        p["hist"] = hist
    save_json(HISTORY_FILE, history)
    return prices


# ============================================================
#  Demo Data
# ============================================================

DEMO_DATA = [
    {"en":"Aluminum A00","desc":"A00 铝锭, 长江有色均价","price":25080,"unit":"CNY/t","change":1.37,"cat":"Non-Ferrous","src":"长江有色","quoteDate":"2026-04-16","icon":"🔩","color":"#3068a8","hist":[24500,25200,25200,24800,23200,23500,24600,25080]},
    {"en":"Copper 1#","desc":"1# 电解铜, 长江有色均价","price":102510,"unit":"CNY/t","change":1.5,"cat":"Non-Ferrous","src":"长江有色","quoteDate":"2026-04-16","icon":"⚡","color":"#b86e2c","hist":[98800,99200,100800,101100,101600,102000,102400,102510]},
    {"en":"Neodymium (Nd)","desc":"金属钕, 国际现货 (USD→CNY)","price":1088,"unit":"CNY/kg","change":2.1,"cat":"Rare Earth","src":"DailyMetalPrice","quoteDate":"2026-04-16","icon":"🧲","color":"#6e4fa0","hist":[1020,1035,1048,1055,1060,1070,1075,1088]},
    {"en":"PC Resin","desc":"注塑级, 华东现货","price":15200,"unit":"CNY/t","change":0.8,"cat":"Plastics","src":"生意社","quoteDate":"2026-04-16","icon":"♻️","color":"#1e806c","hist":[14600,14700,14800,14900,15000,14950,15100,15200]},
    {"en":"ABS Resin","desc":"通用级, 华东现货","price":9850,"unit":"CNY/t","change":-0.4,"cat":"Plastics","src":"生意社","quoteDate":"2026-04-16","icon":"♻️","color":"#1e806c","hist":[10100,10050,10000,9980,9920,9900,9880,9850]},
    {"en":"Silicon Steel (Oriented)","desc":"取向硅钢 0.3mm, 华东现货","price":12850,"unit":"CNY/t","change":0.5,"cat":"Steel","src":"生意社","quoteDate":"2026-04-16","icon":"🏗️","color":"#7a6540","hist":[12500,12520,12550,12600,12650,12700,12800,12850]},
]


# ============================================================
#  HTML Dashboard Template
# ============================================================

DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOLT SC — Material Cost Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;500;600;700&family=Outfit:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#f6f4f0;--bg2:#edeae4;--surface:#fff;--surface-glass:rgba(255,255,255,0.72);--border:#e2ddd4;--border2:#d5cfc4;--text:#1a1714;--text2:#3d3830;--text-dim:#7a7265;--text-muted:#a49d92;--accent:#c8553a;--accent2:#a8432d;--accent-bg:rgba(200,85,58,0.06);--accent-bg2:rgba(200,85,58,0.12);--green:#2d8a4e;--green-soft:#e8f5ec;--red:#c8553a;--red-soft:#fceee9;--blue:#3068a8;--blue-soft:#e6eff8;--purple:#6e4fa0;--purple-soft:#f0ebf7;--teal:#1e806c;--teal-soft:#e4f4f0;--orange:#b86e2c;--orange-soft:#faf0e4}
body{font-family:'Outfit',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased}
.ambient{position:fixed;inset:0;pointer-events:none;z-index:0;overflow:hidden}
.ambient .orb{position:absolute;border-radius:50%;filter:blur(120px);opacity:0.18}
.ambient .o1{width:600px;height:600px;background:#c8553a;top:-200px;right:-100px}
.ambient .o2{width:500px;height:500px;background:#3068a8;bottom:-150px;left:-100px}
.ambient .o3{width:400px;height:400px;background:#2d8a4e;top:40%;left:50%;transform:translate(-50%,-50%)}
.shell{position:relative;z-index:1;max-width:1360px;margin:0 auto;padding:48px 32px 72px}
header{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:52px;flex-wrap:wrap;gap:24px;padding-bottom:32px;border-bottom:1px solid var(--border)}
.brand .eyebrow{font-size:11px;font-weight:600;letter-spacing:3px;text-transform:uppercase;color:var(--accent);margin-bottom:10px}
.brand h1{font-family:'Playfair Display',serif;font-size:44px;font-weight:700;color:var(--text);letter-spacing:-1px;line-height:1.05}
.brand h1 span{background:linear-gradient(135deg,var(--accent),#d47a3a,var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.brand .sub{color:var(--text-dim);font-size:14px;margin-top:8px}
.meta{text-align:right;display:flex;flex-direction:column;align-items:flex-end;gap:8px}
.badge{display:inline-flex;align-items:center;gap:8px;font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:500;padding:7px 16px;border-radius:99px}
.badge-ok{color:var(--green);background:var(--green-soft);border:1px solid rgba(45,138,78,0.2)}
.badge-ok::before{content:'';width:7px;height:7px;border-radius:50%;background:var(--green);animation:p 2s infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}
.badge-date{color:var(--text-dim);background:var(--bg2);border:1px solid var(--border)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:44px}
.card{background:var(--surface-glass);backdrop-filter:blur(20px);border:1px solid var(--border);border-radius:14px;padding:22px 24px;transition:all .25s;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,transparent,var(--accent),transparent);opacity:0;transition:opacity .3s}
.card:hover{transform:translateY(-3px);box-shadow:0 12px 40px rgba(0,0,0,.06)}.card:hover::before{opacity:1}
.card .ck{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:2px;color:var(--text-muted);margin-bottom:10px}
.card .cv{font-family:'IBM Plex Mono',monospace;font-size:30px;font-weight:600;line-height:1}
.card .cv.up{color:var(--green)}.card .cv.dn{color:var(--red)}
.card .cs{font-size:11px;color:var(--text-dim);margin-top:6px}
.filters{display:flex;gap:8px;margin-bottom:28px;flex-wrap:wrap}
.fb{padding:8px 22px;border:1px solid var(--border);border-radius:99px;background:var(--surface);color:var(--text-dim);font-size:13px;font-weight:500;cursor:pointer;transition:all .2s}
.fb:hover{border-color:var(--border2);color:var(--text2);background:var(--bg2)}
.fb.on{background:var(--accent-bg2);border-color:var(--accent);color:var(--accent);font-weight:600}
.tw{background:var(--surface-glass);backdrop-filter:blur(20px);border:1px solid var(--border);border-radius:14px;overflow:hidden;margin-bottom:44px;box-shadow:0 4px 24px rgba(0,0,0,.03)}
table{width:100%;border-collapse:collapse}
thead th{padding:14px 20px;text-align:left;font-size:10px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:2px;background:var(--bg2);border-bottom:1px solid var(--border)}
thead th:first-child{padding-left:28px}
tbody td{padding:20px;font-size:13px;border-bottom:1px solid rgba(226,221,212,.6);transition:background .15s}
tbody td:first-child{padding-left:28px}
tbody tr:last-child td{border-bottom:none}
tbody tr{cursor:pointer}tbody tr:hover td{background:var(--accent-bg)}
.mc{display:flex;align-items:center;gap:14px}
.ci{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}
.mi .n{font-weight:600;font-size:13.5px}.mi .d{font-size:11.5px;color:var(--text-dim);margin-top:2px}
.pv{font-family:'IBM Plex Mono',monospace;font-size:15px;font-weight:600}
.uv{color:var(--text-muted);font-size:11px;font-family:'IBM Plex Mono',monospace}
.dv{color:var(--text-dim);font-size:11.5px;font-family:'IBM Plex Mono',monospace;white-space:nowrap}
.ch{font-family:'IBM Plex Mono',monospace;font-size:11.5px;font-weight:600;display:inline-flex;align-items:center;gap:4px;padding:4px 12px;border-radius:99px}
.ch.up{color:var(--green);background:var(--green-soft)}.ch.dn{color:var(--red);background:var(--red-soft)}.ch.fl{color:var(--text-muted);background:var(--bg2)}
.sp{display:flex;align-items:flex-end;gap:2px;height:32px}
.sb{width:5px;border-radius:2px;transition:all .2s}
tbody tr:hover .sb{opacity:1!important;transform:scaleY(1.08)}
.sv{color:var(--text-muted);font-size:11px}
.cb{background:var(--surface-glass);backdrop-filter:blur(20px);border:1px solid var(--border);border-radius:14px;overflow:hidden;margin-bottom:44px;box-shadow:0 4px 24px rgba(0,0,0,.03)}
.ch2{display:flex;justify-content:space-between;align-items:center;padding:22px 28px;border-bottom:1px solid var(--border);flex-wrap:wrap;gap:12px}
.ch2 h3{font-family:'Playfair Display',serif;font-size:22px;font-weight:600}
.ch2 select{background:var(--bg2);border:1px solid var(--border);color:var(--text2);padding:8px 14px;border-radius:8px;font-size:12px;font-family:'Outfit',sans-serif;cursor:pointer}
.chb{padding:28px}canvas{width:100%!important;height:300px!important}
footer{text-align:center;padding:28px 0 0;color:var(--text-muted);font-size:11px;border-top:1px solid var(--border);margin-top:24px}
footer a{color:var(--accent);text-decoration:none;font-weight:500}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.sp{display:none}}
@media(max-width:600px){.shell{padding:24px 16px 48px}.brand h1{font-size:30px}.cards{grid-template-columns:1fr 1fr}.card .cv{font-size:22px}thead th,tbody td{padding:12px 14px}}
@keyframes ri{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
.a{animation:ri .55s cubic-bezier(.22,.68,.36,1) both}.a1{animation-delay:.06s}.a2{animation-delay:.12s}.a3{animation-delay:.18s}.a4{animation-delay:.24s}
</style>
</head>
<body>
<div class="ambient"><div class="orb o1"></div><div class="orb o2"></div><div class="orb o3"></div></div>
<div class="shell">
  <header class="a">
    <div class="brand">
      <div class="eyebrow">BOLT SC ⚡ Supply Chain</div>
      <h1>Material <span>Cost Monitor</span></h1>
      <div class="sub">China Spot Price Dashboard — Raw Materials Tracking</div>
    </div>
    <div class="meta">
      <div class="badge badge-ok">Updated</div>
      <div class="badge badge-date">__GENERATED_AT__</div>
    </div>
  </header>
  <div class="cards a a1" id="cards"></div>
  <div class="filters a a2" id="filters"></div>
  <div class="tw a a3"><table><thead><tr><th>Material</th><th>Spot Price (¥)</th><th>Unit</th><th>Quote Date</th><th>Weekly Chg</th><th>8-Week Trend</th><th>Source</th></tr></thead><tbody id="tb"></tbody></table></div>
  <div class="cb a a4"><div class="ch2"><h3>Price Trend</h3><select id="sel"></select></div><div class="chb"><canvas id="cv"></canvas></div></div>
  <footer>Sources: 长江有色 (cnal.com) · DailyMetalPrice · 生意社 100ppi | For procurement reference only</footer>
</div>
<script>
const DATA=__LIVE_DATA_PLACEHOLDER__;
const colorBg={"#6e4fa0":"var(--purple-soft)","#3068a8":"var(--blue-soft)","#b86e2c":"var(--orange-soft)","#2d8a4e":"var(--green-soft)","#1e806c":"var(--teal-soft)","#7a6540":"rgba(122,101,64,0.08)"};
DATA.forEach(d=>{if(!d.bg)d.bg=colorBg[d.color]||"var(--bg2)"});
const CATS=["All",...new Set(DATA.map(d=>d.cat))];
const WK=["W-7","W-6","W-5","W-4","W-3","W-2","W-1","Now"];
function fp(d){return d.price>=1000?d.price.toLocaleString("en-US",{maximumFractionDigits:0}):d.price.toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2})}
function ms(h,c){const mn=Math.min(...h),mx=Math.max(...h),r=mx-mn||1;return'<div class="sp">'+h.map((v,i)=>{const ht=Math.max(4,((v-mn)/r)*32);return'<div class="sb" style="height:'+ht+'px;background:'+c+';opacity:'+(i===h.length-1?1:0.35)+'"></div>'}).join("")+"</div>"}
!function(){const e=document.getElementById("cards"),u=DATA.filter(d=>d.change>0).length,dn=DATA.filter(d=>d.change<0).length;let mu=DATA[0],md=DATA[0];DATA.forEach(d=>{if(d.change>mu.change)mu=d;if(d.change<md.change)md=d});e.innerHTML='<div class="card"><div class="ck">Tracked</div><div class="cv">'+DATA.length+'</div><div class="cs">'+(CATS.length-1)+' categories</div></div><div class="card"><div class="ck">Gainers</div><div class="cv up">+'+u+'</div><div class="cs">Rising this week</div></div><div class="card"><div class="ck">Decliners</div><div class="cv dn">'+dn+'</div><div class="cs">Falling this week</div></div><div class="card"><div class="ck">Top Gainer</div><div class="cv up" style="font-size:17px">'+mu.en+'</div><div class="cs">+'+mu.change+'%</div></div><div class="card"><div class="ck">Top Decliner</div><div class="cv dn" style="font-size:17px">'+md.en+'</div><div class="cs">'+md.change+'%</div></div>'}();
!function(){const e=document.getElementById("filters");e.innerHTML=CATS.map((c,i)=>'<button class="fb'+(i===0?" on":"")+'" data-c="'+c+'">'+c+"</button>").join("");e.querySelectorAll(".fb").forEach(b=>{b.onclick=()=>{e.querySelectorAll(".fb").forEach(x=>x.classList.remove("on"));b.classList.add("on");rt(b.dataset.c)}})}();
function rt(cat){const items=cat==="All"?DATA:DATA.filter(d=>d.cat===cat);document.getElementById("tb").innerHTML=items.map(d=>{const cl=d.change>0?"up":d.change<0?"dn":"fl",ar=d.change>0?"▲":d.change<0?"▼":"—",i=DATA.indexOf(d);return'<tr data-i="'+i+'"><td><div class="mc"><div class="ci" style="background:'+d.bg+'">'+d.icon+'</div><div class="mi"><div class="n">'+d.en+'</div><div class="d">'+d.desc+'</div></div></div></td><td class="pv">¥'+fp(d)+'</td><td class="uv">'+d.unit+'</td><td class="dv">'+d.quoteDate+'</td><td><span class="ch '+cl+'">'+ar+" "+Math.abs(d.change)+"%</span></td><td>"+ms(d.hist,d.color)+'</td><td class="sv">'+d.src+"</td></tr>"}).join("");document.querySelectorAll("#tb tr").forEach(tr=>{tr.onclick=()=>{document.getElementById("sel").value=DATA.indexOf(DATA[+tr.dataset.i]);dc(+tr.dataset.i)}})}
rt("All");
!function(){const s=document.getElementById("sel");DATA.forEach((d,i)=>{const o=document.createElement("option");o.value=i;o.textContent=d.icon+" "+d.en;s.appendChild(o)});s.onchange=e=>dc(+e.target.value)}();
function dc(idx){const d=DATA[idx],cv=document.getElementById("cv"),cx=cv.getContext("2d"),dp=devicePixelRatio||1,rc=cv.parentElement.getBoundingClientRect();cv.width=rc.width*dp;cv.height=300*dp;cv.style.width=rc.width+"px";cv.style.height="300px";cx.scale(dp,dp);const W=rc.width,H=300,p={t:32,r:80,b:48,l:82},dt=d.hist,mn=Math.min(...dt)*.996,mx=Math.max(...dt)*1.004,xs=(W-p.l-p.r)/(dt.length-1),ys=(H-p.t-p.b)/(mx-mn||1),c=d.color;cx.clearRect(0,0,W,H);for(let i=0;i<=5;i++){const y=p.t+(H-p.t-p.b)*i/5;cx.beginPath();cx.moveTo(p.l,y);cx.lineTo(W-p.r,y);cx.strokeStyle="rgba(0,0,0,0.05)";cx.lineWidth=1;cx.stroke();cx.fillStyle="#a49d92";cx.font="11px IBM Plex Mono";cx.textAlign="right";const v=mx-(mx-mn)*i/5;cx.fillText(v>=1000?"¥"+Math.round(v).toLocaleString("en-US"):"¥"+v.toFixed(2),p.l-12,y+4)}cx.textAlign="center";cx.fillStyle="#a49d92";cx.font="11px IBM Plex Mono";dt.forEach((_,i)=>cx.fillText(WK[i],p.l+i*xs,H-p.b+24));const g=cx.createLinearGradient(0,p.t,0,H-p.b);g.addColorStop(0,c+"25");g.addColorStop(1,c+"02");cx.beginPath();cx.moveTo(p.l,H-p.b);dt.forEach((v,i)=>cx.lineTo(p.l+i*xs,H-p.b-(v-mn)*ys));cx.lineTo(p.l+(dt.length-1)*xs,H-p.b);cx.closePath();cx.fillStyle=g;cx.fill();cx.beginPath();cx.strokeStyle=c;cx.lineWidth=2.5;cx.lineJoin="round";cx.lineCap="round";dt.forEach((v,i)=>{const x=p.l+i*xs,y=H-p.b-(v-mn)*ys;i===0?cx.moveTo(x,y):cx.lineTo(x,y)});cx.stroke();dt.forEach((v,i)=>{const x=p.l+i*xs,y=H-p.b-(v-mn)*ys;if(i===dt.length-1){cx.beginPath();cx.arc(x,y,7,0,Math.PI*2);cx.fillStyle=c+"18";cx.fill()}cx.beginPath();cx.arc(x,y,i===dt.length-1?4.5:2.5,0,Math.PI*2);cx.fillStyle=i===dt.length-1?c:"#fff";cx.fill();cx.strokeStyle=c;cx.lineWidth=2;cx.stroke()});const lx=p.l+(dt.length-1)*xs,ly=H-p.b-(dt[dt.length-1]-mn)*ys;cx.fillStyle=c;cx.font="bold 14px IBM Plex Mono";const lbl=dt[dt.length-1]>=1000?"¥"+Math.round(dt[dt.length-1]).toLocaleString("en-US"):"¥"+dt[dt.length-1].toFixed(2);const lblW=cx.measureText(lbl).width;if(lx+12+lblW>W-4){cx.textAlign="right";cx.fillText(lbl,lx-12,ly-10)}else{cx.textAlign="left";cx.fillText(lbl,lx+12,ly-2)};cx.fillStyle="#7a7265";cx.font="13px Outfit";cx.textAlign="left";cx.fillText(d.icon+" "+d.en+" · 8-Week Trend",p.l,20)}
dc(0);window.addEventListener("resize",()=>dc(+document.getElementById("sel").value));
</script>
</body>
</html>"""


def generate_html(data, generated_at):
    template = DASHBOARD_TEMPLATE
    data_json = json.dumps(data, ensure_ascii=False, indent=2)
    template = template.replace("__LIVE_DATA_PLACEHOLDER__", data_json)
    template = template.replace("__GENERATED_AT__", generated_at)
    return template


# ============================================================
#  Main
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--output", default="docs/index.html")
    args = parser.parse_args()

    now = datetime.datetime.now()
    generated_at = now.strftime("%b %d, %Y %H:%M")

    if args.test:
        logger.info("🧪 Test mode — using demo data")
        prices = DEMO_DATA
    else:
        logger.info("🌐 Fetching live prices...")
        fetcher = PriceFetcher()
        prices = fetcher.fetch_all()
        prices = update_history(prices)

    html = generate_html(prices, generated_at)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")

    logger.info(f"✅ Dashboard: {output_path}")
    logger.info(f"   Materials: {len(prices)}")

    # CSV backup
    try:
        import pandas as pd
        Path("data").mkdir(exist_ok=True)
        df = pd.DataFrame(prices)
        df.to_csv(f"data/prices_{now:%Y%m%d}.csv", index=False, encoding="utf-8-sig")
        df.to_csv("data/latest.csv", index=False, encoding="utf-8-sig")
    except ImportError:
        pass


if __name__ == "__main__":
    main()
