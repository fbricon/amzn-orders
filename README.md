# amzn-orders

Summarize how much money you've spent on Amazon, from the order history in your
[Amazon data export](https://www.amazon.com/gp/help/customer/display.html?nodeId=GYQTSSRHKUEM2QAF)
(GDPR "Request my data" download).

Works with extracted folders and/or `.zip` archives — pass as many as you want.
Overlapping sources are deduplicated automatically.

## Getting your order history from Amazon

Amazon doesn't offer a simple "export all orders" button; instead you request
your personal data (GDPR-style export), which includes your full order history:

1. Sign in to your Amazon account.
2. Go to **Account & Lists → Account → Privacy → Request your data**
   (direct link: [amazon.com/hz/privacy-central/data-requests](https://www.amazon.com/hz/privacy-central/data-requests/preview)).
   On other marketplaces use the same path, e.g. `amazon.de`, `amazon.ca`, `amazon.fr`.
3. Click **Request all your data** (or select at least the *Orders*, *Digital
   Orders* and *Returns & Refunds* categories).
4. Submit. Amazon processes the request and sends a download **link by email** —
   usually within a few hours, but it can take up to 30 days.
5. Download the `.zip` file(s). No need to extract them — this tool reads zips directly:

   ```bash
   python3 amazon_spend.py ~/Downloads/Amazon-*.zip
   ```

Tips:

- If your history spans many years, Amazon may deliver **several archives**
  (e.g. `Retail.OrderHistory.1`, `.2`, …) or you may need separate requests for
  older periods — pass them all in one command; duplicates are removed automatically.
- Shop on multiple Amazon sites/accounts (`.com`, `.ca`, `.de`…)? Request the
  data from **each account**, then pass every download together. Amounts are
  reported per currency, so mixed-marketplace totals stay honest.

## Usage

```bash
python3 amazon_spend.py PATH [PATH...]
```

Examples:

```bash
# a single extracted export
python3 amazon_spend.py ~/Downloads/amazon-orders

# one or more zip archives
python3 amazon_spend.py ~/Downloads/Amazon-Order-History.zip

# mix of zips and folders (e.g. exports from different marketplaces/accounts)
python3 amazon_spend.py ~/Downloads/amazon-orders ~/Downloads/amazon-old-export.zip
```

Options:

| Flag | Description |
|------|-------------|
| `--currency CUR` | Main display currency for converted totals (default: `EUR`, e.g. `--currency USD`) |
| `--locale LOCALE` | Locale for number/currency formatting (e.g. `fr_FR`); defaults to your environment. Formats amounts per local convention (`20 428,74 €` vs `$20,428.74`); zero-decimal currencies like JPY drop cents |
| `--color auto\|always\|never` | Colored output (default: `auto`) |

Requires Python 3.9+ (standard library only).

## Development

Run the tests with:

```bash
python3 -m unittest test_amazon_spend
```

## What it reports

1. **Per currency (original amounts)** — orders, total spent, refunds, net
   (e.g. EUR, CAD, USD kept separate).
2. **Converted to a main currency** (default EUR, see `--currency`) — every
   purchase converted at the historical exchange rate of its purchase date
   (ECB daily rates via [frankfurter.app](https://frankfurter.app)), with a
   grand total. Refunds are converted at their refund date and netted out.
3. **By year** — net spending per year in the main currency, with
   original-currency amounts.
4. **Top 10 largest purchases** — date, converted amount, original amount, product name.

Sample output:

```
AMAZON SPENDING OVERVIEW
8 csv file(s) scanned · 528 purchases · 17 refunds

Per currency (original amounts)
  CUR   orders        spent   refunded          net
  EUR      243     9,040.85    -450.94     8,589.91
  CAD       50     4,369.17    -146.12     4,223.05
  USD        6       135.21       0.00       135.21

Converted to EUR
  ...
  TOTAL €12,187.14
```

## How it works

The tool scans each source for these CSVs and ignores everything else
(both the classic GDPR layout and the newer "Your Amazon Orders" layout):

| File pattern | Used for |
|--------------|----------|
| `Retail.OrderHistory*.csv` or `Order History.csv` | Retail purchases (`Total Owed`/`Total Amount` per line item) |
| `Digital Orders Monetary.csv` (+ `Digital Orders.csv`) or `Digital Content Orders.csv` | Digital purchases (Kindle, apps…) |
| `Retail.OrdersReturned.Payments*.csv` or `Refund Details.csv` | Completed refunds |

Identical rows found in multiple sources are counted once — even across the
two export layouts — so you can safely combine overlapping exports.

Exchange rates are cached per currency/year in `~/.cache/amzn-orders`.

- If the API is unreachable for a currency that has a built-in approximation,
  that flat rate is used and every affected line is flagged `(approx rate)`,
  with a note under the grand total.
- If no rate data exists at all for a currency (offline and no approximation),
  its transactions are excluded from converted totals with a loud warning
  rather than silently counted as zero.

## Limitations

- Totals reflect what Amazon charged; bank FX fees or card conversion spread are not included.
- Conversions are estimates based on ECB reference rates on the purchase date.
- Cancelled/unshipped orders may still appear if Amazon exported them with an amount owed.

## License

[MIT](LICENSE)
