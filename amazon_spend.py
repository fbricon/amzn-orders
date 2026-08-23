#!/usr/bin/env python3
import argparse
import bisect
import csv
import io
import json
import locale
import os
import re
import sys
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

RETAIL_PREFIX = "retail.orderhistory"
DIGITAL_MONEY = "digital orders monetary.csv"
DIGITAL_ORDERS = "digital orders.csv"
REFUND_PREFIX = "retail.ordersreturned.payments"
NEW_RETAIL = "order history.csv"
NEW_DIGITAL = "digital content orders.csv"
NEW_REFUNDS = "refund details.csv"

def _cache_dir():
    """Per-platform cache location: %LOCALAPPDATA% on Windows, ~/Library/Caches on macOS, XDG on Linux."""
    override = os.environ.get("AMZN_ORDERS_CACHE")
    if override:
        return Path(override)
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(root) / "amzn-orders"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "amzn-orders"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return Path(xdg) / "amzn-orders" if xdg else Path.home() / ".cache" / "amzn-orders"


CACHE_DIR = _cache_dir()
FALLBACK_RATE = {"USD": Decimal("0.92"), "GBP": Decimal("1.16"), "CAD": Decimal("0.68")}
SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "INR": "₹", "CAD": "CA$", "AUD": "A$", "CHF": "CHF"}
ZERO_DECIMAL = {"JPY"}
LOCALE_READY = False


def setup_locale(preferred=None):
    """Adopt a locale for number/currency formatting; falls back to C (dot decimals).
    Returns False only when an explicitly requested locale is unavailable."""
    global LOCALE_READY
    if preferred:
        candidates = [preferred, preferred.split(".")[0]]
    else:
        candidates = [os.environ.get("LC_ALL"), os.environ.get("LC_NUMERIC"), os.environ.get("LANG"), ""]
    for loc in candidates:
        if loc is None:
            continue
        try:
            locale.setlocale(locale.LC_ALL, loc)
            LOCALE_READY = loc not in ("C", "POSIX")
            return True
        except locale.Error:
            continue
    LOCALE_READY = False
    return False

STYLES = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
}
COLOR_MODE = "auto"


def paint(text, *styles):
    if not styles:
        return text
    enabled = COLOR_MODE == "always" or (COLOR_MODE == "auto" and sys.stdout.isatty())
    if not enabled:
        return text
    return "".join(STYLES[s] for s in styles) + str(text) + STYLES["reset"]


def classify(name):
    n = Path(name).name.lower()
    if not n.endswith(".csv"):
        return None
    if n == NEW_RETAIL or n.startswith(RETAIL_PREFIX):
        return "retail"
    if n in (NEW_DIGITAL, DIGITAL_MONEY):
        return "digital_money"
    if n == DIGITAL_ORDERS:
        return "digital_dates"
    if n == NEW_REFUNDS or n.startswith(REFUND_PREFIX):
        return "refunds"
    return None


def _file_opener(path):
    @contextmanager
    def opener():
        with path.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
            yield fh
    return opener


def _zip_opener(zip_path, info):
    @contextmanager
    def opener():
        with zipfile.ZipFile(zip_path) as z:
            with z.open(info) as raw:
                yield io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
    return opener


def iter_files(paths):
    """Yield (kind, name, opener); opener() is a context manager streaming the CSV text."""
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() == ".csv":
                    kind = classify(f.name)
                    if kind:
                        yield kind, str(f), _file_opener(f)
        elif p.suffix.lower() == ".zip":
            with zipfile.ZipFile(p) as z:
                for info in z.infolist():
                    if not info.is_dir():
                        kind = classify(info.filename)
                        if kind:
                            yield kind, info.filename, _zip_opener(p, info)
        else:
            print(paint(f"warning: skipping {p} (not a folder or zip)", "yellow"), file=sys.stderr)


def _hdr(name):
    """Normalize a CSV header for tolerant matching: 'Order-ID'/'order id'/'OrderID' all match."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def to_decimal(s):
    # Accepts '1234.56', '1,234.56' and European-style '1.234,56' / '12,34'.
    s = re.sub(r"[^\d.,+-]", "", s.strip())
    if not s:
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s:
        parts = s.split(",")
        if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
            s = "".join(parts)
        else:
            s = s.replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        return None


def to_date(s):
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


class Data:
    def __init__(self):
        self.retail = Counter()
        self.digital = Counter()
        self.refunds = Counter()
        self.packet_info = {}
        self.files = 0


def scan(paths):
    data = Data()
    deferred = []
    for kind, name, opener in iter_files(paths):
        data.files += 1
        if kind == "digital_money":
            deferred.append(opener)
            continue
        _scan_csv(data, kind, opener)

    for opener in deferred:
        _scan_csv(data, "digital_money", opener)
    return data


def _scan_csv(data, kind, opener):
    with opener() as fh:
        reader = csv.DictReader(fh)
        fields = {}
        for fn in reader.fieldnames or []:
            fields.setdefault(_hdr(fn), fn)

        def col(row, key):
            return (row.get(fields.get(_hdr(key))) or "").strip()

        def num(row, *keys):
            for k in keys:
                v = to_decimal(col(row, k))
                if v is not None:
                    return v
            return None

        local = Counter()
        if kind == "retail":
            for r in reader:
                cur = col(r, "Currency").upper()
                amt = num(r, "Total Owed", "Total Amount")
                if not cur or amt is None:
                    continue
                local[(col(r, "Order ID"), to_date(col(r, "Order Date")), cur, amt, col(r, "Product Name"))] += 1
        elif kind == "digital_dates":
            for r in reader:
                pid = col(r, "DeliveryPacketId")
                d = to_date(col(r, "OrderDate")) or to_date(col(r, "Order Date"))
                oid = col(r, "OrderId") or col(r, "Order ID")
            prev = data.packet_info.get(pid)
            if prev is None:
                data.packet_info[pid] = (d, oid)
            else:
                merged = (d or prev[0], oid or prev[1])
                if merged != prev:
                    data.packet_info[pid] = merged
            return
        elif kind == "digital_money":
            for r in reader:
                cur = (col(r, "BaseCurrencyCode") or col(r, "Base Currency Code")).upper()
                amt = num(r, "TransactionAmount", "Transaction Amount")
                if not cur or amt is None:
                    continue
                pid = col(r, "DeliveryPacketId") or col(r, "Delivery Packet ID")
                pdate, poid = data.packet_info.get(pid, (None, ""))
                oid = col(r, "Order ID") or poid or pid
                d = to_date(col(r, "Fulfilled Date")) or to_date(col(r, "Order Date")) or pdate
                local[(oid, d, cur, amt)] += 1
        elif kind == "refunds":
            for r in reader:
                if col(r, "Status") != "Completed" and col(r, "Payment Status") != "Completed":
                    continue
                cur = col(r, "Currency").upper()
                amt = num(r, "AmountRefunded", "Refund Amount")
                if not cur or amt is None:
                    continue
                d = to_date(col(r, "RefundCompletionDate")) or to_date(col(r, "Refund Date"))
                local[(col(r, "OrderID") or col(r, "Order ID"), d, cur, amt)] += 1
        else:
            return
        target = {"retail": data.retail, "digital_money": data.digital, "refunds": data.refunds}[kind]
        for key, n in local.items():
            target[key] = max(target[key], n)


class Rates:
    """Daily FX series stored as EUR-per-unit; converts to any target currency via cross rate."""

    def __init__(self, target="EUR"):
        self.target = target.upper()
        self.series = {}
        self.approx_used = set()
        self.offline = set()

    def _fetch(self, cur, first, last):
        url = f"https://api.frankfurter.app/{first}-01-01..{last}-12-31?from={cur}&to=EUR"
        req = urllib.request.Request(url, headers={"User-Agent": "amzn-orders/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.load(resp, parse_float=Decimal)["rates"]
        return {date.fromisoformat(k): v["EUR"] for k, v in payload.items() if v.get("EUR")}

    def _load_one(self, cur, years):
        """Load one currency's rates from cache, backfilling all missing years in a single ranged fetch."""
        merged, missing = {}, []
        for y in sorted(years):
            try:
                cached = json.loads((CACHE_DIR / f"{cur}-{y}.json").read_text(encoding="utf-8"))
                merged.update({date.fromisoformat(k): Decimal(str(v)) for k, v in cached.items()})
            except Exception:
                missing.append(y)
        fetched = False
        if missing:
            try:
                daily = self._fetch(cur, min(missing), max(missing))
                merged.update(daily)
                for y in missing:
                    chunk = {d: v for d, v in daily.items() if d.year == y}
                    if chunk:
                        (CACHE_DIR / f"{cur}-{y}.json").write_text(
                            json.dumps({d.isoformat(): str(v) for d, v in sorted(chunk.items())}), encoding="utf-8")
                fetched = True
            except Exception as e:
                print(paint(f"warning: FX fetch failed for {cur}: {e}", "yellow"), file=sys.stderr)
        if merged:
            days = sorted(merged)
            return cur, (days, [merged[d] for d in days]), False
        return cur, None, not fetched

    def load(self, currencies, years):
        """Load daily rates for currencies+target; sets .offline to currencies with NO rate data."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        wanted = [c for c in (set(currencies) | {self.target}) if c != "EUR"]
        years = set(years) or {date.today().year}
        self.offline = set()
        with ThreadPoolExecutor(max_workers=min(len(wanted) or 1, 8)) as pool:
            for cur, series, is_offline in pool.map(lambda c: self._load_one(c, years), wanted):
                if series:
                    self.series[cur] = series
                if is_offline:
                    self.offline.add(cur)
        return self.offline

    def _eur_per_unit(self, cur, day):
        if cur == "EUR":
            return Decimal("1")
        if cur in self.series:
            days, vals = self.series[cur]
            idx = bisect.bisect_right(days, day) - 1 if day else len(days) - 1
            return vals[max(idx, 0)]
        rate = FALLBACK_RATE.get(cur)
        if rate is not None:
            self.approx_used.add(cur)
        return rate

    def get(self, cur, day=None):
        """Rate cur -> target, or None when no data is available."""
        if cur == self.target:
            return Decimal("1")
        base = self._eur_per_unit(cur, day)
        tgt = self._eur_per_unit(self.target, day) if self.target != "EUR" else Decimal("1")
        if base is None or tgt is None:
            return None
        return base / tgt


def csym(code):
    return SYMBOLS.get(code, code + " ")


def fmt(x, decimals=2):
    x = x.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    if LOCALE_READY:
        return locale.format_string(f"%.{decimals}f", x, grouping=True)
    return f"{x:,.{decimals}f}"


def money(amount, code):
    """Amount with its symbol placed per the active locale (e.g. €1.234,56 or 1 234,56 €)."""
    sign = "-" if amount < 0 else ""
    text = fmt(abs(amount), minor_units(code))
    sym = csym(code)
    if not LOCALE_READY:
        return f"{sign}{sym}{text}"
    conv = locale.localeconv()
    sep = " " if conv["p_sep_by_space"] else ""
    if conv["p_cs_precedes"]:
        return f"{sign}{sym}{sep}{text}"
    return f"{sign}{text}{sep}{sym}"


def minor_units(code):
    return 0 if code.upper() in ZERO_DECIMAL else 2


def fx_universe(data):
    """Currencies and activity days across all scanned rows."""
    curs, days = set(), set()
    for ctr in (data.retail, data.digital, data.refunds):
        for row in ctr:
            if row[1]:
                days.add(row[1])
            if row[2]:
                curs.add(row[2])
    return curs, days


def load_rates_or_exit(data, target):
    rates = Rates(target)
    curs, days = fx_universe(data)
    no_data = rates.load(curs, {d.year for d in days})
    if target != "EUR" and target in no_data and target not in FALLBACK_RATE:
        print(paint(f"error: no exchange rates available for {target} (offline?)", "red"), file=sys.stderr)
        sys.exit(1)
    return rates


def report(data, target="EUR", rates=None):
    purchases = [
        row
        for row, n in data.retail.items()
        for _ in range(n)
    ] + [
        (oid, d, cur, amt, "(digital)")
        for (oid, d, cur, amt), n in data.digital.items()
        for _ in range(n)
    ]
    refund_rows = [row for row, n in data.refunds.items() for _ in range(n)]

    spent_cur = defaultdict(Decimal)
    refund_cur = defaultdict(Decimal)
    orders = defaultdict(set)
    spent_year_cur = defaultdict(lambda: defaultdict(Decimal))
    for oid, d, cur, amt, name in purchases:
        spent_cur[cur] += amt
        orders[cur].add(oid)
        if d:
            spent_year_cur[d.year][cur] += amt
    for oid, d, cur, amt in refund_rows:
        refund_cur[cur] += amt

    currencies = sorted(set(spent_cur) | set(refund_cur), key=lambda c: -spent_cur[c])
    if rates is None:
        rates = load_rates_or_exit(data, target)

    def to_target(amount, cur, d):
        rate = rates.get(cur, d)
        return None if rate is None else amount * rate

    total_spent = Decimal("0")
    total_refund = Decimal("0")
    excluded_n = Counter()
    excluded_sum = defaultdict(Decimal)
    for _, d, c, a, _ in purchases:
        v = to_target(a, c, d)
        if v is None:
            excluded_n[c] += 1
            excluded_sum[c] += a
        else:
            total_spent += v
    for _, d, c, a in refund_rows:
        v = to_target(a, c, d)
        if v is None:
            excluded_n[c] += 1
            excluded_sum[c] += a
        else:
            total_refund += v

    lines = [paint("AMAZON SPENDING OVERVIEW", "bold", "cyan"),
             paint(f"{data.files} csv file(s) scanned · {len(purchases)} purchases · {len(refund_rows)} refunds", "dim"), ""]

    lines.append(paint("Per currency (original amounts)", "bold"))
    lines.append(paint(f"  {'CUR':<5}{'orders':>7}{'spent':>13}{'refunded':>11}{'net':>13}", "dim"))
    for cur in currencies:
        s, r = spent_cur[cur], refund_cur[cur]
        dec = minor_units(cur)
        ref = f"-{fmt(r, dec)}" if r else fmt(Decimal(0), dec)
        cells = (
            f"{cur:<5}",
            f"{len(orders[cur]):>7}",
            paint(f"{fmt(s, dec):>13}"),
            paint(f"{ref:>11}", "red" if r else "dim"),
            paint(f"{fmt(s - r, dec):>13}", "green"),
        )
        lines.append("  " + "".join(cells))
    lines.append("")

    lines.append(paint(f"Converted to {target}", "bold"))
    for cur in currencies:
        if cur == target:
            continue
        if cur in excluded_n:
            lines.append(f"  {cur:<5}  {paint(f'no FX data — {excluded_n[cur]} transaction(s) {money(excluded_sum[cur], cur)} excluded', 'red')}")
            continue
        s = sum((v for _, d, c, a, _ in purchases if c == cur for v in [to_target(a, c, d)] if v is not None), Decimal("0"))
        r = sum((v for _, d, c, a in refund_rows if c == cur for v in [to_target(a, c, d)] if v is not None), Decimal("0"))
        note = paint(" (approx rate)", "yellow") if cur in rates.offline or cur in rates.approx_used else ""
        seg = f"{money(s, cur):>14}"
        if r:
            seg += f"  {paint(money(-r, cur), 'red')}"
        lines.append(f"  {cur:<5}{seg}{note}")
    net = total_spent - total_refund
    lines.append(paint(f"  TOTAL {money(net, target)}", "bold", "green"))
    if rates.approx_used:
        lines.append(paint(f"  (approximate FX rates used for: {', '.join(sorted(rates.approx_used))})", "yellow"))
    if excluded_n:
        n = sum(excluded_n.values())
        lines.append(paint(f"  ! {n} transaction(s) missing from totals — no FX data for: {', '.join(sorted(excluded_n))}", "red"))
        print(paint(f"warning: no exchange rates for {', '.join(sorted(excluded_n))}; {n} transaction(s) excluded from {target} totals", "yellow"), file=sys.stderr)
    if total_refund:
        lines.append(paint(f"  ({money(total_spent, target)} spent − {money(total_refund, target)} refunded)", "dim"))
    lines.append("")

    yearly = defaultdict(Decimal)
    for _, d, c, a, _ in purchases:
        if d:
            v = to_target(a, c, d)
            if v is not None:
                yearly[d.year] += v
    for _, d, c, a in refund_rows:
        if d:
            v = to_target(a, c, d)
            if v is not None:
                yearly[d.year] -= v
    refund_year_cur = defaultdict(lambda: defaultdict(Decimal))
    for _, d, c, a in refund_rows:
        if d:
            refund_year_cur[d.year][c] += a
    ref_by_year = {y: ", ".join(f"-{money(v, c)}" for c, v in sorted(refund_year_cur[y].items()) if v) for y in yearly}
    refw = max(8, max((len(s) for s in ref_by_year.values()), default=0))
    lines.append(paint(f"By year (net {target})", "bold"))
    lines.append(paint(f"  {'YEAR':<6}{'NET':>14}  {'REFUNDED':>{refw}}  SPENT (original)", "dim"))
    for y in sorted(yearly):
        spent = ", ".join(money(v, c) for c, v in sorted(spent_year_cur[y].items()) if v)
        val = paint(f"{money(yearly[y], target):>14}", "bold", "green" if yearly[y] >= 0 else "red")
        refs = ref_by_year[y]
        ref_cell = paint(f"{refs:>{refw}}", "red") if refs else f"{'':>{refw}}"
        lines.append(f"  {y:<6}{val}  {ref_cell}  {paint(spent, 'dim')}")
    year_total = sum(yearly.values(), Decimal("0"))
    total_label = paint(f"{'TOTAL':<6}", "bold")
    tr_cell = paint(f"{money(-total_refund, target):>{refw}}", "red") if total_refund else f"{'':>{refw}}"
    lines.append(f"  {total_label}{paint(f'{money(year_total, target):>14}', 'bold', 'green')}  {tr_cell}")

    ranked = [(v, d, c, a, nm) for _, d, c, a, nm in purchases for v in [to_target(a, c, d)] if v is not None]
    top = sorted(ranked, key=lambda t: t[0], reverse=True)[:10]
    lines.append("")
    lines.append(paint(f"Top 10 largest purchases ({target})", "bold"))
    for e, d, c, a, nm in top:
        name = (nm or "(unnamed)")
        if len(name) > 48:
            name = name[:47].rstrip() + "…"
        extra = "" if c == target else "  " + paint(f"({money(a, c)})", "dim")
        lines.append(f"  {d or '?'}  {paint(f'{money(e, target):>9}', 'cyan')}{extra}  {name}")

    lines.append("")
    cache_disp = str(CACHE_DIR).replace(str(Path.home()), "~", 1)
    lines.append(paint(
        f"FX: ECB daily reference rates via frankfurter.app — purchases at purchase-date rate,"
        f" refunds at refund-date rate · cached in {cache_disp}",
        "dim",
    ))

    print("\n".join(lines))


def main():
    global COLOR_MODE
    if sys.platform == "win32":
        os.system("")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Overview of Amazon spending from GDPR export folders/zips")
    ap.add_argument("paths", nargs="+", help="Zip file(s) and/or extracted folder(s)")
    ap.add_argument("--currency", metavar="CUR", default="EUR", help="Main display currency (default: EUR)")
    ap.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    ap.add_argument("--locale", metavar="LOCALE", default=None,
                    help="Locale for number/currency formatting (e.g. fr_FR); defaults to your environment")
    args = ap.parse_args()
    COLOR_MODE = args.color
    if not setup_locale(args.locale):
        if args.locale:
            print(paint(f"warning: unknown locale {args.locale!r}; using environment default", "yellow"), file=sys.stderr)
            setup_locale()
    currency = args.currency.upper()
    if len(currency) != 3 or not currency.isalpha():
        ap.error(f"invalid --currency {args.currency!r}: expected a 3-letter code like USD, CAD, GBP")

    data = scan(args.paths)
    if not data.files:
        print("No Amazon order CSVs found.", file=sys.stderr)
        sys.exit(1)
    rates = load_rates_or_exit(data, currency)
    report(data, currency, rates)


if __name__ == "__main__":
    main()
