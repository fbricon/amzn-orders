#!/usr/bin/env python3
import argparse
import bisect
import csv
import io
import json
import re
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict
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

CACHE_DIR = Path.home() / ".cache" / "amzn-orders"
FALLBACK_RATE = {"USD": Decimal("0.92"), "GBP": Decimal("1.16"), "CAD": Decimal("0.68")}
SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "CAD": "CA$", "AUD": "A$", "CHF": "CHF "}

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


def iter_files(paths):
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() == ".csv":
                    kind = classify(f.name)
                    if kind:
                        yield kind, str(f), f.read_text(encoding="utf-8-sig", errors="replace")
        elif p.suffix.lower() == ".zip":
            with zipfile.ZipFile(p) as z:
                for info in z.infolist():
                    if not info.is_dir():
                        kind = classify(info.filename)
                        if kind:
                            yield kind, info.filename, z.read(info).decode("utf-8-sig", errors="replace")
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
    for kind, name, text in iter_files(paths):
        data.files += 1
        if kind == "digital_money":
            deferred.append(text)
            continue
        _scan_csv(data, kind, text)

    for text in deferred:
        _scan_csv(data, "digital_money", text)
    return data


def _scan_csv(data, kind, text):
    reader = csv.DictReader(io.StringIO(text))
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
            if prev is None or (d and not prev[0]):
                data.packet_info[pid] = (d, oid)
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

    def load(self, currencies, years):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        wanted = set(currencies) | {self.target}
        years = set(years) or {date.today().year}
        offline = []
        for cur in wanted:
            if cur == "EUR":
                continue
            merged = {}
            missing = []
            caches = {}
            for y in sorted(years):
                cf = CACHE_DIR / f"{cur}-{y}.json"
                caches[y] = cf
                try:
                    merged.update({date.fromisoformat(k): v for k, v in json.loads(cf.read_text(), parse_float=Decimal).items()})
                except Exception:
                    missing.append(y)
            for y in missing:
                try:
                    url = f"https://api.frankfurter.app/{y}-01-01..{y}-12-31?from={cur}&to=EUR"
                    req = urllib.request.Request(url, headers={"User-Agent": "amzn-orders/1.0"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        payload = json.load(resp, parse_float=Decimal)["rates"]
                    daily = {date.fromisoformat(k): v["EUR"] for k, v in payload.items() if v.get("EUR")}
                    merged.update(daily)
                    cf.write_text(json.dumps({k.isoformat(): str(v) for k, v in sorted(daily.items())}))
                except Exception:
                    offline.append(cur)
            if merged:
                days = sorted(merged)
                self.series[cur] = (days, [merged[d] for d in days])
            else:
                offline.append(cur)
        approx = set(offline)
        if self.target != "EUR" and self.target in approx:
            approx |= wanted - {"EUR"}
        return approx

    def _eur_per_unit(self, cur, day):
        if cur == "EUR":
            return Decimal("1")
        if cur in self.series:
            days, vals = self.series[cur]
            idx = bisect.bisect_right(days, day) - 1 if day else len(days) - 1
            return vals[max(idx, 0)]
        return FALLBACK_RATE.get(cur)

    def get(self, cur, day=None):
        if cur == self.target:
            return Decimal("1")
        base = self._eur_per_unit(cur, day)
        tgt = self._eur_per_unit(self.target, day) if self.target != "EUR" else Decimal("1")
        if base is None or tgt in (None, Decimal("0")):
            return Decimal("0")
        return base / tgt


def csym(code):
    return SYMBOLS.get(code, code + " ")


def fmt(x):
    return f"{x.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def report(data, target="EUR"):
    sym = csym(target)
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
    days = set()
    for oid, d, cur, amt, name in purchases:
        spent_cur[cur] += amt
        orders[cur].add(oid)
        if d:
            spent_year_cur[d.year][cur] += amt
            days.add(d)
    for oid, d, cur, amt in refund_rows:
        refund_cur[cur] += amt
        if d:
            days.add(d)

    currencies = sorted(set(spent_cur) | set(refund_cur), key=lambda c: -spent_cur[c])
    rates = Rates(target)
    approx = rates.load(currencies, {d.year for d in days})
    if target != "EUR" and target in approx and target not in FALLBACK_RATE:
        print(paint(f"error: no exchange rates available for {target} (offline?)", "red"), file=sys.stderr)
        sys.exit(1)

    def to_target(amount, cur, d):
        return amount * rates.get(cur, d)

    total_spent = sum((to_target(a, c, d) for _, d, c, a, _ in purchases), Decimal("0"))
    total_refund = sum((to_target(a, c, d) for _, d, c, a in refund_rows), Decimal("0"))

    lines = [paint("AMAZON SPENDING OVERVIEW", "bold", "cyan"),
             paint(f"{data.files} csv file(s) scanned · {len(purchases)} purchases · {len(refund_rows)} refunds", "dim"), ""]

    lines.append(paint("Per currency (original amounts)", "bold"))
    lines.append(paint(f"  {'CUR':<5}{'orders':>7}{'spent':>13}{'refunded':>11}{'net':>13}", "dim"))
    for cur in currencies:
        s, r = spent_cur[cur], refund_cur[cur]
        ref = f"-{fmt(r)}" if r else "0.00"
        cells = (
            f"{cur:<5}",
            f"{len(orders[cur]):>7}",
            paint(f"{fmt(s):>13}"),
            paint(f"{ref:>11}", "red" if r else "dim"),
            paint(f"{fmt(s - r):>13}", "green"),
        )
        lines.append("  " + "".join(cells))
    lines.append("")

    lines.append(paint(f"Converted to {target}", "bold"))
    for cur in currencies:
        if cur == target:
            continue
        s = sum((to_target(a, c, d) for _, d, c, a, _ in purchases if c == cur), Decimal("0"))
        r = sum((to_target(a, c, d) for _, d, c, a in refund_rows if c == cur), Decimal("0"))
        note = paint(" (approx rate)", "yellow") if cur in approx else ""
        seg = f"{f'{sym}{fmt(s)}':>14}"
        if r:
            seg += f"  {paint('-' + sym + fmt(r), 'red')}"
        lines.append(f"  {cur:<5}{seg}{note}")
    net = total_spent - total_refund
    lines.append(paint(f"  TOTAL {sym}{fmt(net)}", "bold", "green"))
    if total_refund:
        lines.append(paint(f"  ({fmt(total_spent)} spent − {fmt(total_refund)} refunded)", "dim"))
    lines.append("")

    yearly = defaultdict(Decimal)
    for _, d, c, a, _ in purchases:
        if d:
            yearly[d.year] += to_target(a, c, d)
    for _, d, c, a in refund_rows:
        if d:
            yearly[d.year] -= to_target(a, c, d)
    refund_year_cur = defaultdict(lambda: defaultdict(Decimal))
    for _, d, c, a in refund_rows:
        if d:
            refund_year_cur[d.year][c] += a
    ref_by_year = {y: ", ".join(f"-{c} {fmt(v)}" for c, v in sorted(refund_year_cur[y].items()) if v) for y in yearly}
    refw = max(8, max((len(s) for s in ref_by_year.values()), default=0))
    lines.append(paint(f"By year (net {target})", "bold"))
    lines.append(paint(f"  {'YEAR':<6}{'NET':>14}  {'REFUNDED':>{refw}}  SPENT (original)", "dim"))
    for y in sorted(yearly):
        spent = ", ".join(f"{c} {fmt(v)}" for c, v in sorted(spent_year_cur[y].items()) if v)
        val = paint(f"{sym + fmt(yearly[y]):>14}", "bold", "green" if yearly[y] >= 0 else "red")
        refs = ref_by_year[y]
        ref_cell = paint(f"{refs:>{refw}}", "red") if refs else f"{'':>{refw}}"
        lines.append(f"  {y:<6}{val}  {ref_cell}  {paint(spent, 'dim')}")
    year_total = sum(yearly.values(), Decimal("0"))
    total_label = paint(f"{'TOTAL':<6}", "bold")
    tr_cell = paint(f"{'-' + sym + fmt(total_refund):>{refw}}", "red") if total_refund else f"{'':>{refw}}"
    lines.append(f"  {total_label}{paint(f'{sym + fmt(year_total):>14}', 'bold', 'green')}  {tr_cell}")

    top = sorted(((to_target(a, c, d), d, c, a, nm) for _, d, c, a, nm in purchases), key=lambda t: t[0], reverse=True)[:10]
    lines.append("")
    lines.append(paint(f"Top 10 largest purchases ({target})", "bold"))
    for e, d, c, a, nm in top:
        name = (nm or "(unnamed)")
        if len(name) > 48:
            name = name[:47].rstrip() + "…"
        extra = "" if c == target else "  " + paint(f"({c} {fmt(a)})", "dim")
        lines.append(f"  {d or '?'}  {paint(f'{sym}{fmt(e):>9}', 'cyan')}{extra}  {name}")

    lines.append("")
    lines.append(paint(
        f"FX: ECB daily reference rates via frankfurter.app — purchases at purchase-date rate,"
        f" refunds at refund-date rate · cached in ~/.cache/amzn-orders",
        "dim",
    ))

    print("\n".join(lines))


def main():
    global COLOR_MODE
    ap = argparse.ArgumentParser(description="Overview of Amazon spending from GDPR export folders/zips")
    ap.add_argument("paths", nargs="+", help="Zip file(s) and/or extracted folder(s)")
    ap.add_argument("--currency", metavar="CUR", default="EUR", help="Main display currency (default: EUR)")
    ap.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    args = ap.parse_args()
    COLOR_MODE = args.color
    currency = args.currency.upper()
    if len(currency) != 3 or not currency.isalpha():
        ap.error(f"invalid --currency {args.currency!r}: expected a 3-letter code like USD, CAD, GBP")

    data = scan(args.paths)
    if not data.files:
        print("No Amazon order CSVs found.", file=sys.stderr)
        sys.exit(1)
    report(data, currency)


if __name__ == "__main__":
    main()
