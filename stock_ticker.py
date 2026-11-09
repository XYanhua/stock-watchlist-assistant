# -*- coding: utf-8 -*-
"""多股票实时行情表格小窗 v3 — 独立提醒 + 左对齐 + 全列字体颜色"""
import tkinter as tk
from tkinter import filedialog, messagebox
import urllib.request
import urllib.parse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sys
import ctypes
import re
from datetime import datetime, time as clock_time
from zoneinfo import ZoneInfo

# ===== 在这里修改要监控的股票 =====
STOCKS = ["159852", "588000", "513880"]
# ==================================

# ===== 外观与提醒配置（自动保存到同目录 config.json）=====
DEFAULT_SETTINGS = {
    "bg": "#0e1116",
    "fg": "#8a93a0",       # 表头/代码/名称/标题颜色
    "up_color": "#c96a6a", # 上涨列颜色（价格/涨跌额/涨跌%）
    "down_color": "#5fae83", # 下跌列颜色
    "alpha": 1.0,
    "transparent_bg": False,
    "window_level": "normal", # normal / topmost
    "closed_dates": [],       # 可手动维护的休市日期 YYYY-MM-DD
    "alert_cooldown": 300,    # 同一方向提醒冷却秒数
    "stocks": STOCKS,
    "alerts": {},          # {代码: {on, type(price/pct), up, down}}
}
CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
URL = "https://hq.sinajs.cn/list={code}"
TENCENT_URL = "https://qt.gtimg.cn/q={codes}"


def configure_windows_identity():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "QuantTools.WatchlistAssistant"
        )
    except (AttributeError, OSError):
        pass

def load_settings():
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            s.update(json.load(f))
        s.setdefault("alerts", {})
        s.setdefault("stocks", list(STOCKS))
        s.setdefault("up_color", DEFAULT_SETTINGS["up_color"])
        s.setdefault("down_color", DEFAULT_SETTINGS["down_color"])
        s.setdefault("window_level", DEFAULT_SETTINGS["window_level"])
        s.setdefault("transparent_bg", False)
        s.setdefault("closed_dates", [])
        s.setdefault("alert_cooldown", DEFAULT_SETTINGS["alert_cooldown"])
    except Exception:
        pass
    return s

def save_settings(s):
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def notify(title, msg):
    """通过 plyer 发送 Windows 桌面通知，失败时使用系统弹窗。"""
    try:
        from plyer import notification
        notification.notify(
            title=title,
            message=msg,
            app_name="股票盯盘",
            timeout=5,
            toast=False,
        )
        return
    except Exception:
        try:
            from ctypes import windll
            windll.user32.MessageBoxW(0, msg, title, 0x40 | 0x40000)
        except Exception:
            pass

def search_china_stocks(keyword):
    """按股票代码或中文名称搜索 A 股、场内基金和指数。"""
    keyword = keyword.strip()
    if not keyword:
        return []
    url = "https://suggest3.sinajs.cn/suggest/key=" + urllib.parse.quote(keyword)
    req = urllib.request.Request(
        url,
        headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
    )
    data = urllib.request.urlopen(req, timeout=5).read().decode("gb18030", "ignore")
    if '="' not in data:
        return []
    body = data.split('"', 2)[1]
    results = []
    seen = set()
    for item in body.split(";"):
        fields = item.split(",")
        if len(fields) < 5:
            continue
        code = (fields[3] or fields[0]).strip().lower()
        name = (fields[4] or fields[0]).strip()
        if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", code) or code in seen:
            continue
        seen.add(code)
        results.append((code, name))
    return results[:20]


def direct_china_code_results(keyword):
    """为六位证券代码提供本地兜底，搜索服务不可用时仍可添加。"""
    code = keyword.strip().lower()
    if re.fullmatch(r"(?:sh|sz|bj)\d{6}", code):
        return [(code, f"{code[2:]} · 股票/ETF/指数")]
    if not re.fullmatch(r"\d{6}", code):
        return []
    if code.startswith(("5", "6", "9")):
        markets = ("sh",)
    elif code.startswith(("1", "2", "3")):
        markets = ("sz",)
    elif code.startswith(("4", "8")):
        markets = ("bj",)
    elif code.startswith("0"):
        markets = ("sh", "sz")
    else:
        markets = ("sh",)
    return [(market + code, f"{code} · 股票/ETF/指数") for market in markets]


def is_us_code(code):
    return code.strip().upper().startswith("US:")


def search_us_stocks(keyword):
    symbol = keyword.strip().upper()
    aliases = {
        "DJI": ("DJI", "道琼斯指数"),
        "DJIA": ("DJI", "道琼斯指数"),
        "^DJI": ("DJI", "道琼斯指数"),
        "IXIC": ("IXIC", "纳斯达克综合指数"),
        "^IXIC": ("IXIC", "纳斯达克综合指数"),
        "NASDAQ": ("IXIC", "纳斯达克综合指数"),
        "GSPC": ("GSPC", "标普500指数"),
        "^GSPC": ("GSPC", "标普500指数"),
        "SPX": ("GSPC", "标普500指数"),
        "INX": ("GSPC", "标普500指数"),
    }
    if symbol in aliases:
        code, name = aliases[symbol]
        return [(f"US:{code}", f"{name} · 美股")]
    if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", symbol):
        return [(f"US:{symbol}", f"{symbol} · 美股/ETF")]
    return []


def search_stocks(keyword):
    """同时搜索 A 股、美股股票、ETF 和指数。"""
    results = []
    searchers = (
        (search_us_stocks, search_china_stocks)
        if re.search(r"[A-Za-z]", keyword)
        else (search_china_stocks, search_us_stocks)
    )
    for searcher in searchers:
        try:
            results.extend(searcher(keyword))
        except Exception:
            pass
    results.extend(direct_china_code_results(keyword))
    unique = []
    seen = set()
    for code, name in results:
        if code not in seen:
            unique.append((code, name))
            seen.add(code)
    return unique[:24]

def to_sina(code):
    code = code.strip().lower()
    if code.startswith(("sh", "sz", "bj")):
        return code
    if code.startswith(("5", "6", "9")):
        return "sh" + code
    if code.startswith(("0", "1", "2", "3")):
        return "sz" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sh" + code

def fetch_sina(code):
    req = urllib.request.Request(
        URL.format(code=to_sina(code)),
        headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
    )
    data = urllib.request.urlopen(req, timeout=4).read().decode("gbk", "ignore")
    if '="' not in data:
        return None
    body = data.split('"')[1].split(",")
    if len(body) < 4 or not body[0]:
        return None
    close = float(body[2])
    price = float(body[3]) or close
    return body[0], price, close

def fetch_tencent(codes):
    result = {}
    market_to_original = {to_sina(code): code for code in codes}
    market_codes = ",".join(market_to_original)
    req = urllib.request.Request(
        TENCENT_URL.format(codes=market_codes),
        headers={"User-Agent": "Mozilla/5.0"},
    )
    data = urllib.request.urlopen(req, timeout=5).read().decode("gbk", "ignore")
    for line in data.splitlines():
        if '="' not in line:
            continue
        market_code = line.split("=", 1)[0].replace("v_", "").strip()
        code = market_code[2:]
        fields = line.split('"', 2)[1].split("~")
        if len(fields) < 5 or not fields[1]:
            continue
        close = float(fields[4])
        price = float(fields[3]) or close
        original_code = market_to_original.get(market_code, code)
        result[original_code] = (fields[1], price, close)
    return result


def is_china_auction_time(now=None):
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    current = now.time()
    return clock_time(9, 15) <= current < clock_time(9, 30)


def to_sina_us(code):
    symbol = code.split(":", 1)[1].strip().upper().lstrip("^")
    symbol = {"DJIA": "DJI", "GSPC": "INX", "SPX": "INX"}.get(symbol, symbol)
    return "gb_" + symbol.lower()


def fetch_sina_us(code):
    req = urllib.request.Request(
        URL.format(code=to_sina_us(code)),
        headers={
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    data = urllib.request.urlopen(req, timeout=6).read().decode("gbk", "ignore")
    if '="' not in data:
        return None
    body = data.split('"', 2)[1].split(",")
    if len(body) < 27 or not body[0]:
        return None
    price = float(body[1])
    close = float(body[26])
    if price <= 0 or close <= 0:
        return None
    extended = None
    try:
        extended_price = float(body[21])
        extended_pct = float(body[22])
        extended_diff = float(body[23])
        if extended_price > 0:
            extended = (extended_price, extended_diff, extended_pct)
    except (ValueError, IndexError):
        pass
    return body[0], price, close, extended

def fetch_all(codes):
    """获取中美行情；A 股新浪失败时自动改用腾讯行情。"""
    result = {}
    failed = []
    china_codes = [code for code in codes if not is_us_code(code)]
    us_codes = [code for code in codes if is_us_code(code)]
    auction_quotes = {}
    if china_codes and is_china_auction_time():
        try:
            auction_quotes = fetch_tencent(china_codes)
            result.update(auction_quotes)
        except Exception:
            pass
    if codes:
        with ThreadPoolExecutor(max_workers=min(8, len(codes))) as executor:
            futures = {
                executor.submit(fetch_sina_us if is_us_code(code) else fetch_sina, code): code
                for code in codes
                if code not in auction_quotes
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    result[code] = future.result()
                except Exception:
                    result[code] = None
                if result[code] is None and code in china_codes:
                    failed.append(code)

    used_fallback = False
    if failed:
        try:
            fallback = fetch_tencent(failed)
            used_fallback = bool(fallback)
            for code in failed:
                result[code] = fallback.get(code)
        except Exception:
            pass
    sources = []
    if china_codes:
        if auction_quotes:
            sources.append("腾讯竞价")
        else:
            sources.append("新浪+腾讯" if used_fallback else "新浪")
    if us_codes:
        sources.append("新浪全球")
    source = "+".join(sources) or "离线"
    failed_count = sum(value is None for value in result.values())
    if result and failed_count == len(result):
        source = "离线"
    return result, source, failed_count


class Tooltip:
    """简单悬浮提示：显示完整名称"""
    def __init__(self, widget):
        self.widget = widget
        self.tip = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _=None):
        text = getattr(self.widget, "full_text", "")
        if not text:
            return
        if self.tip and self.tip.winfo_exists():
            return
        x, y, _, _ = self.widget.bbox("insert")
        x = x + self.widget.winfo_rootx() + 20
        y = y + self.widget.winfo_rooty() + 20
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.attributes("-topmost", True)
        tk.Label(self.tip, text=text, bg="#232a33", fg="#e8edf4",
                 font=("Microsoft YaHei UI", 9), padx=6, pady=3).pack()
        self.tip.wm_geometry(f"+{x}+{y}")

    def hide(self, _=None):
        if self.tip and self.tip.winfo_exists():
            self.tip.destroy()
        self.tip = None


class TickerTable(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("盯盘助手")
        base_dir = os.path.dirname(os.path.abspath(__file__))
        icon_ico = os.path.join(base_dir, "assets", "watchlist-assistant.ico")
        icon_png = os.path.join(base_dir, "assets", "watchlist-assistant.png")
        try:
            self.iconbitmap(icon_ico)
        except tk.TclError:
            pass
        try:
            self._app_icon = tk.PhotoImage(file=icon_png)
            self.iconphoto(True, self._app_icon)
        except tk.TclError:
            self._app_icon = None
        self.overrideredirect(True)
        self.attributes("-topmost", False)
        self.configure(bg="#0e1116")
        self.geometry("500x300+80+60")
        self.minsize(380, 240)
        self.settings = load_settings()
        self.stocks = list(dict.fromkeys(self.settings.get("stocks", STOCKS)))
        self.alerts_fired = {}
        self.last_alert_at = {}
        self.refresh_in_progress = False
        self.last_market_state = None
        self._icon_ico = icon_ico
        self._native_icons = []

        column_weights = (1, 1, 1, 1, 1, 0, 0, 0)
        for column, weight in enumerate(column_weights):
            self.grid_columnconfigure(column, weight=weight)
        self.grid_columnconfigure(7, minsize=6)
        self.grid_rowconfigure(3, weight=1)

        # ---- 标题行 ----
        self.title_bar = tk.Frame(self, bg="#0e1116")
        self.title_bar.grid(row=0, column=0, columnspan=7, sticky="ew")
        self.title_lbl = tk.Label(self.title_bar, text="盯盘助手", fg="#5f6b7a",
                                  bg="#0e1116", font=("Microsoft YaHei UI", 9))
        self.title_lbl.pack(side="left", padx=(10, 8), pady=(7, 4))
        self.market_lbl = tk.Label(self.title_bar, text="--", fg="#5f6b7a",
                                   bg="#0e1116", font=("Microsoft YaHei UI", 8))
        self.market_lbl.pack(side="left", padx=(0, 8), pady=(7, 4))
        self.clock_lbl = tk.Label(self.title_bar, text="--:--:--", fg="#5f6b7a",
                                  bg="#0e1116", font=("Consolas", 9))
        self.clock_lbl.pack(side="left", padx=(0, 8), pady=(7, 4))
        self.minimize_btn = tk.Label(self.title_bar, text="—", fg="#5f6b7a", bg="#0e1116",
                                     cursor="hand2", font=("Consolas", 10))
        self.minimize_btn.bind("<Button-1>", self.minimize_window)
        self.set_btn = tk.Label(self.title_bar, text="⚙", fg="#5f6b7a", bg="#0e1116",
                                cursor="hand2", font=("Microsoft YaHei UI", 9))
        self.set_btn.bind("<Button-1>", lambda e: self.open_settings())
        self.close = tk.Label(self.title_bar, text="✕", fg="#5f6b7a", bg="#0e1116",
                              cursor="hand2", font=("Microsoft YaHei UI", 9))
        self.close.pack(side="right", padx=(2, 8), pady=(6, 0))
        self.close.bind("<Button-1>", lambda e: self.destroy())
        self.set_btn.pack(side="right", padx=(2, 2), pady=(6, 0))
        self.minimize_btn.pack(side="right", padx=(2, 2), pady=(6, 0))

        # ---- 搜索与添加 ----
        self.search_var = tk.StringVar()
        self.search_bar = tk.Frame(self, bg="#0e1116")
        self.search_bar.grid(row=1, column=0, columnspan=7, padx=(10, 8), pady=(2, 6), sticky="ew")
        self.search_entry = tk.Entry(
            self.search_bar, textvariable=self.search_var, width=24, relief="flat",
            bg="#0e1116", fg="#c8d4e6", insertbackground="#c8d4e6",
            borderwidth=0, highlightthickness=1,
            highlightbackground="#3a4658", highlightcolor="#6b7f98",
            font=("Microsoft YaHei UI", 9),
        )
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.search_entry.bind("<Return>", lambda _e: self.start_search())
        self.search_btn = tk.Button(
            self.search_bar, text="搜索", command=self.start_search, relief="flat",
            bg="#0e1116", fg="#c8d4e6", activebackground="#0e1116",
            activeforeground="#ffffff", font=("Microsoft YaHei UI", 8),
            borderwidth=0, highlightthickness=1,
            highlightbackground="#3a4658", highlightcolor="#6b7f98",
        )
        self.search_btn.pack(side="right")

        # ---- 冻结表头 ----
        self.header_frame = tk.Frame(self, bg="#0e1116")
        self.header_frame.grid(row=2, column=0, columnspan=6, sticky="ew")
        for column in range(5):
            self.header_frame.grid_columnconfigure(column, weight=1, uniform="header_columns")
        self.headers = ["代码", "名称", "最新价", "涨跌额", "涨跌%"]
        self.head_lbls = []
        for column, heading in enumerate(self.headers):
            label = tk.Label(
                self.header_frame, text=heading, fg="#454f5c", bg="#0e1116",
                font=("Microsoft YaHei UI", 8), anchor="w",
            )
            label.grid(
                row=0, column=column, padx=10 if column == 0 else 0,
                pady=(0, 2), sticky="ew",
            )
            self.head_lbls.append(label)

        # ---- 可滚动行情表格 ----
        self.table_canvas = tk.Canvas(
            self, bg="#0e1116", highlightthickness=0, borderwidth=0,
        )
        self.table_canvas.grid(row=3, column=0, columnspan=6, sticky="nsew")
        self.table_scrollbar = tk.Scrollbar(
            self, orient="vertical", command=self.table_canvas.yview,
            relief="flat", borderwidth=0,
        )
        self.table_scrollbar.grid(row=3, column=6, sticky="ns")
        self.table_canvas.configure(yscrollcommand=self.table_scrollbar.set)
        self.table_frame = tk.Frame(self.table_canvas, bg="#0e1116")
        for column in range(5):
            self.table_frame.grid_columnconfigure(column, weight=1, uniform="data_columns")
        self._table_window = self.table_canvas.create_window(
            (0, 0), window=self.table_frame, anchor="nw",
        )
        self.table_frame.bind("<Configure>", self.update_table_scrollregion)
        self.table_canvas.bind("<Configure>", self.resize_table_width)
        self.bind_all("<MouseWheel>", self.on_table_wheel, add="+")

        # ---- 数据行 ----
        self.rows = {}
        self.extended_rows = {}
        for code in self.stocks:
            self.create_stock_row(code)

        self.status_lbl = tk.Label(
            self, text="等待行情连接", fg="#5f6b7a", bg="#0e1116",
            font=("Microsoft YaHei UI", 8), anchor="w",
        )
        self.status_lbl.grid(row=4, column=0, columnspan=7, padx=10, pady=(5, 7), sticky="ew")

        # 独立右侧缩放边界，避免与滚动条拖拽事件冲突。
        self.right_resize_strip = tk.Frame(self, width=8, bg="#0e1116", cursor="sb_h_double_arrow")
        self.right_resize_strip.grid(row=0, column=7, rowspan=5, sticky="ns")

        # ---- 拖动 ----
        for w in (self, self.title_bar, self.title_lbl):
            w.bind("<ButtonPress-1>", self.start_move)
            w.bind("<B1-Motion>", self.on_move)
        self._drag = None
        self._resize = None
        self._resize_border = 8
        self.bind_all("<Motion>", self.update_resize_cursor, add="+")
        self.bind_all("<ButtonPress-1>", self.start_resize, add="+")
        self.bind_all("<B1-Motion>", self.on_resize, add="+")
        self.bind_all("<ButtonRelease-1>", self.stop_resize, add="+")
        self.bind("<Map>", self.restore_borderless, add="+")
        self.panel = None

        self.apply_theme()
        self.update_clock_and_market(initial=True)
        self.after(1000, self.loop)

    def update_table_scrollregion(self, _event=None):
        bounds = self.table_canvas.bbox("all")
        if bounds:
            self.table_canvas.configure(scrollregion=bounds)

    def resize_table_width(self, event):
        self.table_canvas.itemconfigure(self._table_window, width=event.width)

    def on_table_wheel(self, event):
        x_root, y_root = self.winfo_pointerxy()
        left = self.table_canvas.winfo_rootx()
        top = self.table_canvas.winfo_rooty()
        if not (
            left <= x_root <= left + self.table_canvas.winfo_width()
            and top <= y_root <= top + self.table_canvas.winfo_height()
        ):
            return
        steps = -int(event.delta / 120) if event.delta else 0
        if steps:
            self.table_canvas.yview_scroll(steps, "units")

    def create_stock_row(self, code):
        if code in self.rows:
            return
        row = sum(2 if is_us_code(item) else 1 for item in self.stocks[:self.stocks.index(code)])
        lbls = [
            tk.Label(self.table_frame, text=code, fg="#39424e", bg="#0e1116",
                     font=("Consolas", 9), width=10, anchor="w"),
            tk.Label(self.table_frame, text="...", fg="#8a93a0", bg="#0e1116",
                     font=("Microsoft YaHei UI", 9), width=10, anchor="w"),
            tk.Label(self.table_frame, text="--", fg="#8a93a0", bg="#0e1116",
                     font=("Consolas", 11), width=9, anchor="w"),
            tk.Label(self.table_frame, text="", fg="#8a93a0", bg="#0e1116",
                     font=("Consolas", 9), width=9, anchor="w"),
            tk.Label(self.table_frame, text="", fg="#8a93a0", bg="#0e1116",
                     font=("Consolas", 9), width=8, anchor="w"),
        ]
        for column, label in enumerate(lbls):
            label.grid(row=row, column=column, padx=10 if column == 0 else 0, pady=2, sticky="ew")
        Tooltip(lbls[1])
        for label in lbls:
            label.bind("<Button-3>", lambda event, stock_code=code: self.open_row_menu(event, stock_code))
        self.rows[code] = lbls
        if is_us_code(code):
            extended_labels = [
                tk.Label(self.table_frame, text="", fg="#687586", bg="#0e1116",
                         font=("Consolas", 8), anchor="w")
                for _ in range(3)
            ]
            for column, label in enumerate(extended_labels, start=2):
                label.grid(row=row + 1, column=column, pady=(0, 2), sticky="ew")
            self.extended_rows[code] = extended_labels

    def open_row_menu(self, event, code):
        name = getattr(self.rows[code][1], "full_text", code)
        menu = tk.Menu(
            self,
            tearoff=0,
            bg="#202733",
            fg="#e8edf4",
            activebackground="#354052",
            activeforeground="#ffffff",
        )
        menu.add_command(label=f"设置提醒 · {name}", command=lambda: self.open_alert_dialog(code))
        menu.add_command(label="上移", command=lambda: self.move_stock(code, -1))
        menu.add_command(label="下移", command=lambda: self.move_stock(code, 1))
        menu.add_separator()
        menu.add_command(label="从盯盘中删除", command=lambda: self.remove_stock(code))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def move_stock(self, code, offset):
        try:
            index = self.stocks.index(code)
        except ValueError:
            return
        target = index + offset
        if target < 0 or target >= len(self.stocks):
            return
        self.stocks[index], self.stocks[target] = self.stocks[target], self.stocks[index]
        self.settings["stocks"] = list(self.stocks)
        save_settings(self.settings)
        self.reflow_stock_rows()

    def remove_stock(self, code):
        labels = self.rows.get(code)
        if not labels:
            return
        name = getattr(labels[1], "full_text", code)
        if not messagebox.askyesno("删除盯盘", f"确定从盯盘中删除 {name}（{code}）吗？", parent=self):
            return
        for label in labels:
            label.destroy()
        for label in self.extended_rows.pop(code, []):
            label.destroy()
        self.rows.pop(code, None)
        self.stocks = [item for item in self.stocks if item != code]
        self.settings["stocks"] = list(self.stocks)
        self.settings.get("alerts", {}).pop(code, None)
        self.alerts_fired.pop(code, None)
        save_settings(self.settings)
        self.reflow_stock_rows()

    def reflow_stock_rows(self):
        row = 0
        for code in self.stocks:
            for column, label in enumerate(self.rows[code]):
                label.grid_configure(
                    row=row,
                    column=column,
                    padx=10 if column == 0 else 0,
                    pady=2,
                    sticky="ew",
                )
            for column, label in enumerate(self.extended_rows.get(code, []), start=2):
                label.grid_configure(row=row + 1, column=column, pady=(0, 2), sticky="ew")
            row += 2 if is_us_code(code) else 1

    def start_search(self):
        keyword = self.search_var.get().strip()
        if not keyword:
            return
        self.search_btn.configure(text="查询中", state="disabled")

        def work():
            try:
                results = search_stocks(keyword)
                error = None
            except Exception as exc:
                results, error = [], str(exc)
            self.after(0, lambda: self.show_search_results(results, error))

        threading.Thread(target=work, daemon=True).start()

    def show_search_results(self, results, error=None):
        self.search_btn.configure(text="搜索", state="normal")
        if error:
            messagebox.showwarning("搜索失败", "无法连接股票搜索服务，请稍后重试。", parent=self)
            return
        if not results:
            messagebox.showinfo("搜索结果", "没有找到匹配的 A 股、美股、ETF 或指数。", parent=self)
            return

        popup = tk.Toplevel(self)
        popup.title("选择并添加")
        popup.attributes("-topmost", True)
        popup.configure(bg="#151a22")
        listbox = tk.Listbox(
            popup, width=34, height=min(10, len(results)),
            bg="#151a22", fg="#d7dee8", selectbackground="#354052",
            selectforeground="#ffffff", relief="flat", font=("Microsoft YaHei UI", 9),
        )
        listbox.pack(padx=8, pady=(8, 4), fill="both", expand=True)
        for code, name in results:
            listbox.insert("end", f"{code}  {name}")
        listbox.selection_set(0)

        def add_selected(_event=None):
            selected = listbox.curselection()
            if not selected:
                return
            code, name = results[selected[0]]
            if code in self.rows:
                self.title_lbl.configure(text=f"{name} 已在盯盘列表")
                self.after(3000, lambda: self.title_lbl.configure(text="实时行情"))
            else:
                self.stocks.append(code)
                self.settings["stocks"] = list(self.stocks)
                save_settings(self.settings)
                self.create_stock_row(code)
                self.apply_theme()
                self.refresh()
            self.search_var.set("")
            popup.destroy()

        tk.Button(
            popup, text="添加到盯盘", command=add_selected,
            bg="#3a4250", fg="#e8edf4", relief="flat",
        ).pack(padx=8, pady=(4, 8), fill="x")
        listbox.bind("<Double-Button-1>", add_selected)
        popup.geometry("+%d+%d" % (self.winfo_x() + 30, self.winfo_y() + 50))

    # ---------- 外观 ----------
    def apply_theme(self):
        bg = self.settings["bg"]; fg = self.settings["fg"]
        self.configure(bg=bg)
        self.attributes("-alpha", float(self.settings.get("alpha", 1.0)))
        if sys.platform == "win32":
            try:
                transparent_color = bg if self.settings.get("transparent_bg", False) else ""
                self.attributes("-transparentcolor", transparent_color)
            except tk.TclError:
                pass
        self.apply_window_level()
        for w in self.winfo_children():
            try:
                w.configure(bg=bg)
            except Exception:
                pass
        self.title_bar.configure(bg=bg)
        self.search_bar.configure(bg=bg)
        self.header_frame.configure(bg=bg)
        self.table_canvas.configure(bg=bg)
        self.table_frame.configure(bg=bg)
        self.right_resize_strip.configure(bg=bg)
        self.table_scrollbar.configure(
            bg="#3a4658", activebackground="#55667d",
            troughcolor="#151b22", width=14,
        )
        self.title_lbl.configure(fg=fg, bg=bg)
        self.market_lbl.configure(fg=fg, bg=bg)
        self.clock_lbl.configure(fg=fg, bg=bg)
        self.status_lbl.configure(fg=fg, bg=bg)
        self.minimize_btn.configure(fg=fg, bg=bg)
        self.set_btn.configure(fg=fg, bg=bg)
        self.close.configure(fg=fg, bg=bg)
        border_color = "#566579"
        self.search_entry.configure(
            bg=bg, fg=fg, insertbackground=fg,
            highlightbackground=border_color, highlightcolor=fg,
        )
        self.search_btn.configure(
            bg=bg, fg=fg, activebackground=bg,
            highlightbackground=border_color, highlightcolor=fg,
        )
        for h in self.head_lbls:
            h.configure(fg=fg, bg=bg)
        for lbls in self.rows.values():
            lbls[0].configure(fg=fg, bg=bg)   # 代码
            lbls[1].configure(fg=fg, bg=bg)   # 名称
        for labels in self.extended_rows.values():
            for label in labels:
                label.configure(bg=bg)

    def apply_window_level(self):
        topmost = self.settings.get("window_level", "normal") == "topmost"
        self.attributes("-topmost", topmost)

    def col_color(self, chg):
        """价格列的动态颜色：按涨跌取配置色"""
        if chg > 0:
            return self.settings.get("up_color", "#c96a6a")
        if chg < 0:
            return self.settings.get("down_color", "#5fae83")
        return self.settings.get("fg", "#8a93a0")

    def choose_color(self, var, title, presets):
        pop = tk.Toplevel(self)
        pop.title(title); pop.attributes("-topmost", True)
        pop.configure(bg="#151a22")
        for i, c in enumerate(presets):
            tk.Button(pop, bg=c, width=3, relief="flat",
                      command=lambda c=c: (var.set(c), pop.destroy())
                      ).grid(row=i // 8, column=i % 8, padx=3, pady=3)

    # ---------- 设置面板 ----------
    def open_settings(self):
        if self.panel and self.panel.winfo_exists():
            return
        p = tk.Toplevel(self); self.panel = p
        p.title("设置"); p.attributes("-topmost", True)
        p.configure(bg="#151a22")
        p.protocol("WM_DELETE_WINDOW", lambda: setattr(self, "panel", None) or p.destroy())

        bg_var = tk.StringVar(value=self.settings["bg"])
        fg_var = tk.StringVar(value=self.settings["fg"])
        up_var = tk.StringVar(value=self.settings.get("up_color", "#c96a6a"))
        down_var = tk.StringVar(value=self.settings.get("down_color", "#5fae83"))
        alpha_var = tk.DoubleVar(value=float(self.settings.get("alpha", 1.0)))
        transparent_bg_var = tk.BooleanVar(value=self.settings.get("transparent_bg", False))
        level_var = tk.StringVar(
            value="始终置顶" if self.settings.get("window_level") == "topmost" else "普通窗口"
        )
        closed_dates_var = tk.StringVar(value=", ".join(self.settings.get("closed_dates", [])))
        cooldown_var = tk.StringVar(value=str(self.settings.get("alert_cooldown", 300)))

        tk.Label(p, text="背景颜色", fg="#aab4c2", bg="#151a22").grid(row=0, column=0, padx=8, pady=(10, 2), sticky="w")
        tk.Entry(p, textvariable=bg_var, width=10).grid(row=0, column=1, padx=8, sticky="w")
        tk.Button(p, text="选色", command=lambda: self.choose_color(
            bg_var, "背景色", ["#0e1116", "#1a1f2b", "#101418", "#1c2433", "#14181d", "#232a33", "#0d0d0f", "#2a2f38"])).grid(row=0, column=2, padx=4)

        tk.Label(p, text="字体颜色", fg="#aab4c2", bg="#151a22").grid(row=1, column=0, padx=8, pady=(6, 2), sticky="w")
        tk.Entry(p, textvariable=fg_var, width=10).grid(row=1, column=1, padx=8, sticky="w")
        tk.Button(p, text="选色", command=lambda: self.choose_color(
            fg_var, "字体色", ["#8a93a0", "#c8d4e6", "#e8edf4", "#ffffff", "#a3c4d9", "#d9c9a3", "#9fb8ad", "#b0a8c9"])).grid(row=1, column=2, padx=4)

        tk.Label(p, text="上涨颜色", fg="#aab4c2", bg="#151a22").grid(row=2, column=0, padx=8, pady=(6, 2), sticky="w")
        tk.Entry(p, textvariable=up_var, width=10).grid(row=2, column=1, padx=8, sticky="w")
        tk.Button(p, text="选色", command=lambda: self.choose_color(
            up_var, "上涨色", ["#c96a6a", "#ff4d4f", "#e05b5b", "#ff8a80", "#d9534f", "#e07b39", "#c7a252", "#a3b18a"])).grid(row=2, column=2, padx=4)

        tk.Label(p, text="下跌颜色", fg="#aab4c2", bg="#151a22").grid(row=3, column=0, padx=8, pady=(6, 2), sticky="w")
        tk.Entry(p, textvariable=down_var, width=10).grid(row=3, column=1, padx=8, sticky="w")
        tk.Button(p, text="选色", command=lambda: self.choose_color(
            down_var, "下跌色", ["#5fae83", "#00c853", "#4caf50", "#6fbf9a", "#3d9970", "#7fb069", "#8ab4a8", "#7e9b76"])).grid(row=3, column=2, padx=4)

        tk.Label(p, text="透明度", fg="#aab4c2", bg="#151a22").grid(row=4, column=0, padx=8, pady=(6, 2), sticky="w")
        tk.Scale(p, from_=0.3, to=1.0, resolution=0.05, orient="horizontal",
                 variable=alpha_var, length=200, bg="#151a22", fg="#aab4c2",
                 highlightthickness=0, command=lambda v: self.attributes("-alpha", float(v))
                 ).grid(row=4, column=1, columnspan=2, padx=8, sticky="w")

        tk.Checkbutton(
            p, text="背景透明", variable=transparent_bg_var,
            bg="#151a22", fg="#aab4c2", activebackground="#151a22",
            activeforeground="#e8edf4", selectcolor="#293241",
        ).grid(row=5, column=0, columnspan=3, padx=8, pady=(4, 0), sticky="w")

        tk.Label(p, text="窗口层级", fg="#aab4c2", bg="#151a22").grid(row=6, column=0, padx=8, pady=(6, 2), sticky="w")
        level_menu = tk.OptionMenu(p, level_var, "普通窗口", "始终置顶")
        level_menu.configure(bg="#3a4250", fg="#e8edf4", relief="flat", highlightthickness=0)
        level_menu["menu"].configure(bg="#202733", fg="#e8edf4")
        level_menu.grid(row=6, column=1, columnspan=2, padx=8, sticky="ew")

        tk.Label(p, text="休市日期", fg="#aab4c2", bg="#151a22").grid(row=7, column=0, padx=8, pady=(6, 2), sticky="w")
        tk.Entry(p, textvariable=closed_dates_var, width=25).grid(row=7, column=1, columnspan=2, padx=8, sticky="ew")
        tk.Label(p, text="提醒冷却(秒)", fg="#aab4c2", bg="#151a22").grid(row=8, column=0, padx=8, pady=(6, 2), sticky="w")
        tk.Entry(p, textvariable=cooldown_var, width=10).grid(row=8, column=1, padx=8, sticky="w")

        tools = tk.Frame(p, bg="#151a22")
        tools.grid(row=9, column=0, columnspan=3, padx=8, pady=(10, 2), sticky="ew")
        tk.Button(tools, text="导入盯盘", command=self.import_watchlist,
                  bg="#2d3644", fg="#e8edf4", relief="flat").pack(side="left", fill="x", expand=True, padx=(0, 3))
        tk.Button(tools, text="导出盯盘", command=self.export_watchlist,
                  bg="#2d3644", fg="#e8edf4", relief="flat").pack(side="left", fill="x", expand=True, padx=(3, 0))

        def apply():
            try:
                self.settings["bg"] = bg_var.get()
                self.settings["fg"] = fg_var.get()
                self.settings["up_color"] = up_var.get()
                self.settings["down_color"] = down_var.get()
                self.settings["alpha"] = float(alpha_var.get())
                self.settings["transparent_bg"] = bool(transparent_bg_var.get())
                self.settings["window_level"] = "topmost" if level_var.get() == "始终置顶" else "normal"
                dates = [item.strip() for item in closed_dates_var.get().replace("，", ",").split(",") if item.strip()]
                for item in dates:
                    datetime.strptime(item, "%Y-%m-%d")
                self.settings["closed_dates"] = dates
                self.settings["alert_cooldown"] = max(0, int(cooldown_var.get()))
                save_settings(self.settings)
                self.apply_theme()
                self.recolor_prices()
                p.destroy(); self.panel = None
            except Exception:
                messagebox.showwarning("设置", "请输入有效数值", parent=p)
        tk.Button(p, text="应用并保存", command=apply, bg="#3a4250", fg="#e8edf4",
                  relief="flat").grid(row=10, column=0, columnspan=3, pady=(12, 10))
        p.geometry("+%d+%d" % (self.winfo_x() + 40, self.winfo_y() + 40))

    def import_watchlist(self):
        path = filedialog.askopenfilename(
            title="导入盯盘列表",
            filetypes=[("文本或 JSON", "*.txt *.json"), ("所有文件", "*.*")],
            parent=self,
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as file:
                content = file.read().strip()
            if path.lower().endswith(".json"):
                loaded = json.loads(content)
                candidates = loaded.get("stocks", []) if isinstance(loaded, dict) else loaded
            else:
                candidates = content.replace("，", ",").replace("\n", ",").split(",")
            added = 0
            for value in candidates:
                code = str(value).strip().lower()
                if code.startswith(("sh", "sz", "bj")):
                    code = code[2:]
                if len(code) != 6 or not code.isdigit() or code in self.rows:
                    continue
                self.stocks.append(code)
                self.create_stock_row(code)
                added += 1
            self.settings["stocks"] = list(self.stocks)
            save_settings(self.settings)
            self.apply_theme()
            self.refresh()
            messagebox.showinfo("导入完成", f"已添加 {added} 只股票。", parent=self)
        except Exception as exc:
            messagebox.showwarning("导入失败", f"无法读取该文件：{exc}", parent=self)

    def export_watchlist(self):
        path = filedialog.asksaveasfilename(
            title="导出盯盘列表",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("JSON 文件", "*.json")],
            parent=self,
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as file:
                if path.lower().endswith(".json"):
                    json.dump({"stocks": self.stocks}, file, ensure_ascii=False, indent=2)
                else:
                    file.write("\n".join(self.stocks))
        except Exception as exc:
            messagebox.showwarning("导出失败", f"无法保存文件：{exc}", parent=self)

    # ---------- 每只股票提醒 ----------
    def open_alert_dialog(self, code):
        name = getattr(self.rows[code][1], "full_text", code)
        p = tk.Toplevel(self); p.attributes("-topmost", True)
        p.title(f"提醒设置 · {name}")
        p.configure(bg="#151a22")
        a = self.settings["alerts"].get(code, {})
        on = tk.BooleanVar(value=a.get("on", False))
        typ = tk.StringVar(value=a.get("type", "pct"))
        up_v = tk.StringVar(value=str(a.get("up", "")))
        down_v = tk.StringVar(value=str(a.get("down", "")))

        tk.Label(p, text=f"{name} ({code})", fg="#e8edf4", bg="#151a22",
                 font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, columnspan=3, padx=10, pady=(10, 4), sticky="w")
        tk.Checkbutton(p, text="启用提醒", variable=on, bg="#151a22", fg="#aab4c2",
                       selectcolor="#2a3038", activebackground="#151a22").grid(row=1, column=0, columnspan=3, padx=10, sticky="w")
        tk.Radiobutton(p, text="按价格", variable=typ, value="price", bg="#151a22",
                       fg="#aab4c2", selectcolor="#2a3038", activebackground="#151a22").grid(row=2, column=0, padx=10, sticky="w")
        tk.Radiobutton(p, text="按涨跌幅", variable=typ, value="pct", bg="#151a22",
                       fg="#aab4c2", selectcolor="#2a3038", activebackground="#151a22").grid(row=2, column=1, sticky="w")

        tk.Label(p, text="高于(价格/涨幅%)", fg="#aab4c2", bg="#151a22").grid(row=3, column=0, padx=10, pady=(8, 2), sticky="w")
        tk.Entry(p, textvariable=up_v, width=12).grid(row=3, column=1, columnspan=2, padx=4, sticky="w")
        tk.Label(p, text="低于(价格/跌幅%)", fg="#aab4c2", bg="#151a22").grid(row=4, column=0, padx=10, sticky="w")
        tk.Entry(p, textvariable=down_v, width=12).grid(row=4, column=1, columnspan=2, padx=4, sticky="w")

        def save():
            try:
                conf = {"on": bool(on.get()), "type": typ.get(),
                        "up": float(up_v.get()) if up_v.get().strip() else None,
                        "down": float(down_v.get()) if down_v.get().strip() else None}
                self.settings["alerts"][code] = conf
                self.alerts_fired.pop(code, None)
                save_settings(self.settings)
                p.destroy()
            except Exception:
                messagebox.showwarning("提醒设置", "请输入有效数值", parent=p)
        tk.Button(p, text="保存", command=save, bg="#3a4250", fg="#e8edf4",
                  relief="flat").grid(row=5, column=0, columnspan=3, pady=(12, 10))

    # ---------- 拖动 ----------
    def start_move(self, e):
        try:
            if e.widget.winfo_toplevel() is not self or e.widget is self.table_scrollbar:
                self._drag = None
                return
        except tk.TclError:
            self._drag = None
            return
        if self.edge_at(e.x_root, e.y_root):
            return
        self._drag = (e.x_root - self.winfo_x(), e.y_root - self.winfo_y())

    def on_move(self, e):
        if self._drag and not self._resize:
            try:
                if e.widget.winfo_toplevel() is not self:
                    return
            except tk.TclError:
                return
            self.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def minimize_window(self, _event=None):
        self.overrideredirect(False)
        self.update_idletasks()
        self.apply_native_windows_icon()
        self.iconify()

    def restore_borderless(self, event=None):
        # ????????????????? <Map> ?????????????????????
        if event is not None and getattr(event, "widget", self) is not self:
            return
        if self.state() == "normal" and not self._resize and not self._drag:
            self.after_idle(self.finish_restore)

    def finish_restore(self):
        try:
            if not self.overrideredirect():
                self.overrideredirect(True)
        except tk.TclError:
            self.overrideredirect(True)
        self.apply_native_windows_icon()

    def apply_native_windows_icon(self):
        if sys.platform != "win32" or not os.path.exists(self._icon_ico):
            return
        try:
            user32 = ctypes.windll.user32
            window_handle = user32.GetParent(self.winfo_id()) or self.winfo_id()
            wm_seticon = 0x0080
            if self._native_icons:
                user32.SendMessageW(window_handle, wm_seticon, 0, self._native_icons[0])
                user32.SendMessageW(window_handle, wm_seticon, 1, self._native_icons[1])
                return
            load_from_file = 0x0010
            image_icon = 1
            small_icon = user32.LoadImageW(
                None, self._icon_ico, image_icon, 16, 16, load_from_file
            )
            large_icon = user32.LoadImageW(
                None, self._icon_ico, image_icon, 32, 32, load_from_file
            )
            if small_icon:
                user32.SendMessageW(window_handle, wm_seticon, 0, small_icon)
            if large_icon:
                user32.SendMessageW(window_handle, wm_seticon, 1, large_icon)
            if small_icon and large_icon:
                self._native_icons = [small_icon, large_icon]
        except (AttributeError, OSError, tk.TclError):
            pass

    def edge_at(self, x_root, y_root):
        left = x_root - self.winfo_rootx()
        top = y_root - self.winfo_rooty()
        right = self.winfo_width() - left
        bottom = self.winfo_height() - top
        border = self._resize_border
        horizontal = "w" if left <= border else "e" if right <= border else ""
        vertical = "n" if top <= border else "s" if bottom <= border else ""
        return vertical + horizontal

    def update_resize_cursor(self, event):
        if self._resize:
            return
        try:
            if event.widget.winfo_toplevel() is not self:
                return
            if event.widget is self.table_scrollbar:
                return
        except tk.TclError:
            return
        edge = self.edge_at(event.x_root, event.y_root)
        cursors = {
            "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
            "e": "sb_h_double_arrow", "w": "sb_h_double_arrow",
            "ne": "size_ne_sw", "sw": "size_ne_sw",
            "nw": "size_nw_se", "se": "size_nw_se",
        }
        try:
            event.widget.configure(cursor=cursors.get(edge, ""))
        except tk.TclError:
            pass

    def start_resize(self, event):
        try:
            if event.widget.winfo_toplevel() is not self:
                # ?????????????????????????????
                # ?????????/??????????????????
                self._resize = None
                self._drag = None
                return
            if event.widget is self.table_scrollbar:
                # ?????????????????/?????
                # ????????????????????????????
                self._resize = None
                self._drag = None
                return
        except tk.TclError:
            return
        edge = self.edge_at(event.x_root, event.y_root)
        if not edge:
            # ????????????????????????????????
            # ??????? _drag???????? start_move ????????
            self._resize = None
            return
        self._drag = None
        self._resize = {
            "edge": edge,
            "x": event.x_root,
            "y": event.y_root,
            "win_x": self.winfo_x(),
            "win_y": self.winfo_y(),
            "width": self.winfo_width(),
            "height": self.winfo_height(),
        }

    def on_resize(self, event):
        if not self._resize:
            return
        # ????????????????????/???????
        # ?????????????????????????????
        try:
            if event.widget.winfo_toplevel() is not self:
                return
        except tk.TclError:
            self._resize = None
            return
        state = self._resize
        dx = event.x_root - state["x"]
        dy = event.y_root - state["y"]
        x, y = state["win_x"], state["win_y"]
        width, height = state["width"], state["height"]
        min_width, min_height = self.minsize()
        edge = state["edge"]
        if "e" in edge:
            width = max(min_width, width + dx)
        if "s" in edge:
            height = max(min_height, height + dy)
        if "w" in edge:
            new_width = max(min_width, width - dx)
            x += width - new_width
            width = new_width
        if "n" in edge:
            new_height = max(min_height, height - dy)
            y += height - new_height
            height = new_height
        self.geometry(f"{width}x{height}+{x}+{y}")

    def stop_resize(self, _event=None):
        self._resize = None
        self._drag = None

    # ---------- 数据刷新 ----------
    def refresh(self):
        if self.refresh_in_progress:
            return
        self.refresh_in_progress = True

        def work():
            try:
                payload = fetch_all(self.stocks)
            except Exception:
                payload = ({code: None for code in self.stocks}, "离线", len(self.stocks))
            self.after(0, lambda: self.finish_refresh(payload))
        threading.Thread(target=work, daemon=True).start()

    def finish_refresh(self, payload):
        self.refresh_in_progress = False
        data, source, failed_count = payload
        self.show(data)
        now_text = datetime.now().strftime("%H:%M:%S")
        if failed_count:
            self.status_lbl.configure(
                text=f"连接异常 · {source} · {failed_count} 项失败 · 最近更新 {now_text}",
                fg=self.settings.get("down_color", "#5fae83"),
            )
        else:
            self.status_lbl.configure(
                text=f"数据正常 · {source} · 更新于 {now_text}",
                fg=self.settings.get("fg", "#8a93a0"),
            )

    def market_state(self, now=None):
        """返回 A 股当前交易阶段和是否应持续刷新行情。"""
        now = now or datetime.now()
        if now.strftime("%Y-%m-%d") in set(self.settings.get("closed_dates", [])):
            return "节假日休市", False
        if now.weekday() >= 5:
            return "周末休市", False

        current = now.time()
        if current < clock_time(9, 15):
            return "未开盘", False
        if current < clock_time(9, 25):
            return "集合竞价", True
        if current < clock_time(9, 30):
            return "开盘准备", True
        if current < clock_time(11, 30):
            return "交易中", True
        if current < clock_time(13, 0):
            return "午间休市", False
        if current < clock_time(14, 57):
            return "交易中", True
        if current < clock_time(15, 0):
            return "收盘竞价", True
        if current < clock_time(15, 3):
            # 行情源在 15:00 后通常还需要数秒到数十秒写入最终收盘价。
            return "收盘结算", True
        if current < clock_time(15, 5):
            return "盘后定价准备", False
        if current < clock_time(15, 30):
            # 科创板、创业板股票可在此阶段进行盘后固定价格交易。
            return "盘后定价交易", True
        if current < clock_time(15, 33):
            # 给行情源保留写入盘后最终成交价的结算时间。
            return "盘后结算", True
        return "已收盘", False

    def us_market_state(self, now=None):
        """返回纽约市场交易阶段；自动处理中美时差和夏令时。"""
        now = now or datetime.now()
        new_york_now = now.astimezone(ZoneInfo("America/New_York"))
        if new_york_now.weekday() >= 5:
            return "周末休市", False
        current = new_york_now.time()
        if current < clock_time(4, 0):
            return "未开盘", False
        if current < clock_time(9, 30):
            return "盘前交易", True
        if current < clock_time(16, 0):
            return "交易中", True
        if current < clock_time(20, 0):
            return "盘后交易", True
        return "已收盘", False

    def update_clock_and_market(self, initial=False):
        now = datetime.now()
        china_state, china_active = self.market_state(now)
        has_us = any(is_us_code(code) for code in self.stocks)
        has_china = any(not is_us_code(code) for code in self.stocks)
        if has_us:
            us_state, us_active = self.us_market_state(now)
            active = china_active or us_active if has_china else us_active
            state = f"A股{china_state} · 美股{us_state}" if has_china else f"美股{us_state}"
        else:
            state, active = china_state, china_active
        self.clock_lbl.configure(text=now.strftime("%H:%M:%S"))
        self.market_lbl.configure(text=state)

        state_changed = state != self.last_market_state
        if initial or active:
            self.refresh()
        elif state_changed:
            # 任一市场切换到非活跃阶段时补抓一次最终成交价。
            self.refresh()
        self.last_market_state = state

    def recolor_prices(self):
        """颜色设置变化后重刷价格列颜色（保持已显示数值）"""
        for code, lbls in self.rows.items():
            if lbls[2].cget("text") in ("--", ""):
                continue
            try:
                chg = float(lbls[4].cget("text").replace("%", ""))
            except Exception:
                chg = 0
            c = self.col_color(chg)
            lbls[2].configure(fg=c); lbls[3].configure(fg=c); lbls[4].configure(fg=c)

    def show(self, data):
        us_state, _ = self.us_market_state()
        show_extended = us_state != "交易中"
        for code, lbls in self.rows.items():
            r = data.get(code)
            if r is None:
                lbls[1].config(text="--")
                lbls[2].config(text="--", fg="#8a93a0")
                lbls[3].config(text="", fg="#8a93a0")
                lbls[4].config(text="", fg="#8a93a0")
                for label in self.extended_rows.get(code, []):
                    label.config(text="")
                continue
            name, price, close = r[:3]
            extended = r[3] if len(r) > 3 else None
            chg = (price - close) / close * 100 if close else 0
            diff = price - close
            c = self.col_color(chg)
            lbls[1].config(text=name)
            lbls[1].full_text = name
            lbls[2].config(text=f"{price:,.3f}", fg=c)
            lbls[3].config(text=f"{diff:+.3f}", fg=c)
            lbls[4].config(text=f"{chg:+.2f}%", fg=c)
            extended_labels = self.extended_rows.get(code, [])
            if extended_labels and extended and show_extended:
                extended_price, extended_diff, extended_pct = extended
                extended_color = self.col_color(extended_pct)
                extended_labels[0].config(text=f"{extended_price:,.3f}", fg=extended_color)
                extended_labels[1].config(text=f"{extended_diff:+.3f}", fg=extended_color)
                extended_labels[2].config(text=f"{extended_pct:+.2f}%", fg=extended_color)
                self.check_alert(code, name, extended_price, price, extended_pct)
            else:
                for label in extended_labels:
                    label.config(text="")
                self.check_alert(code, name, price, close, chg)

    def check_alert(self, code, name, price, close, chg):
        """按每只股票的独立配置提醒：支持按价格或按涨跌幅"""
        try:
            conf = self.settings["alerts"].get(code) or {}
            if not conf.get("on"):
                return
            mode = conf.get("type", "pct")
            value = price if mode == "price" else chg
            up, down = conf.get("up"), conf.get("down")
            fired = self.alerts_fired.get(code)

            # 离开已触发区域后解除锁定，允许未来再次越线提醒。
            if fired == "up" and (up is None or value < up):
                self.alerts_fired.pop(code, None)
                fired = None
            elif fired == "down" and (down is None or value > down):
                self.alerts_fired.pop(code, None)
                fired = None

            if up is not None and value >= up and fired != "up":
                if mode == "price":
                    title = f"价格提醒 {name}"
                    message = f"{name} 现价 {price:,.3f}，已达 {up:,.3f}"
                else:
                    title = f"涨幅提醒 {name}"
                    message = f"{name} 现价 {price:,.3f}，涨幅 {chg:+.2f}% ≥ {up:+.2f}%"
                self.send_alert(code, "up", title, message)
            elif down is not None and value <= down and fired != "down":
                if mode == "price":
                    title = f"价格提醒 {name}"
                    message = f"{name} 现价 {price:,.3f}，已跌破 {down:,.3f}"
                else:
                    title = f"跌幅提醒 {name}"
                    message = f"{name} 现价 {price:,.3f}，跌幅 {chg:+.2f}% ≤ {down:+.2f}%"
                self.send_alert(code, "down", title, message)
        except Exception:
            pass

    def send_alert(self, code, direction, title, message):
        now = datetime.now().timestamp()
        key = (code, direction)
        cooldown = max(0, int(self.settings.get("alert_cooldown", 300)))
        if now - self.last_alert_at.get(key, 0) < cooldown:
            return
        self.alerts_fired[code] = direction
        self.last_alert_at[key] = now
        threading.Thread(target=notify, args=(title, message), daemon=True).start()

    def loop(self):
        if self.winfo_exists():
            self.update_clock_and_market()
            self.after(1000, self.loop)


if __name__ == "__main__":
    configure_windows_identity()
    TickerTable().mainloop()
