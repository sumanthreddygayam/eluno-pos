# Eluno till (v1): billing that replaces Shopify POS

A till app for Eluno's seven stores. Shopify stays the system of record: every order, payment, refund
and restock the till makes lands in Shopify through the Admin API, in the shape Shopify expects.
Prescriptions come from the store tool. The till owns the per-store invoice series, offers, the cash
drawer, the audit trail and day-end reporting.

Python 3.8+, standard library only. Nothing to install, no account, no internet needed: the demo starts its
own local copy of the Shopify / store-tool / terminal mock and stops it at the end.

**Live till screen:** LIVE_URL_HERE (free tier: the first visit after a quiet spell takes up to a minute to wake up).

## The till screen

```bash
python run.py serve     # then open http://127.0.0.1:8000 in a browser
```

One process runs the till screen, the till API and its own copy of the mock, with the mock's 429/503 failures
**on**. The same thing runs on the hosted link. A salesperson can run the whole Must flow from a laptop or a tablet:

| Tab | What it does |
|---|---|
| **New bill** | Look up the customer by mobile number: flags (priority, campaign), the prescription in use, and why older ones were skipped. A number the till doesn't know becomes a new customer (created in Shopify). A prescription can be added: an eye test here, signed off by the optometrist, or a paper one from another optometrist, with who wrote it. The till checks it first: 0.25 steps, ranges, an axis with any cylinder, ADD for both eyes or neither, PD, date. It is saved in the store tool and used for the bill straight away. Add one or more pairs: frame (live stock here, and which stores have it), lens type, index and coating, and optionally a different wearer (family bill). Channel, manager's discount with approver and reason. The price re-checks on every change: GST per line, offers applied, why the others weren't, and the checks (red blocks the bill, amber needs a tick). Pay by UPI, card or cash. |
| **Bills today** | Every bill with its status, retry, cancel or void, the invoice, a refund shortcut, and each bill's audit history. Frames reserved by unfinished bills. |
| **Refund** | Find an invoice (older invoices are read from Shopify), tick lines, give a reason; money goes back the way it came, the credit note is issued, and the frame goes back on the shelf. |
| **Cash drawer** | Open with a float, paid in and out with reasons, count and close: shows the variance and flags it to finance when over the limit. |
| **Day-end** | The store's day from Shopify, checked against the till's invoices, credit notes and drawer; bills by channel for all stores. |
| **All stores** | Any date, every store side by side: bills, net sales, refunds, UPI/card/cash, the cash drawer (expected, counted, short or over) and whether everything reached Shopify. Above it, the totals and one list of every alert, worst first, filterable by severity and store: cash in Shopify that doesn't match the drawer, a drawer short or over, paid bills or refunds not yet in Shopify, Shopify totals that differ from the invoice, gaps in an invoice series, payments stuck on the card machine, frames held by unfinished bills, drawers not yet counted. "Open" jumps to that store's own day-end. |
| **Demo controls** | Hosted demo only. Turns the mock's 429/503 on or off, resets to the seed data, and arms failures the mock can't produce: a declined card, a card machine that stops answering, a customer slow to approve UPI, Wi-Fi dropping Shopify's answer, Shopify refusing orders. It also lists sample customers and things to try. |

Every payment outcome is handled on screen:
- **declined:** nothing charged, the frame stays reserved; pay another way or cancel.
- **card machine not answering:** retrying is safe and can't charge twice; only a manager can cancel.
- **paid but Shopify unreachable:** the invoice is issued and the order is sent later.

The **Network** bar at the bottom lists every Shopify, store-tool and terminal call each action made, including
each 429 or 503 and the retry that absorbed it.

`python run.py serve --no-flake` turns the mock's failures off. With `ELUNO_POS_BASE_URL` set, `serve` skips its
own mock and runs against that backend instead (no demo controls).

### Hosting it (free)

* **Render:** Dashboard → New → Blueprint → pick this repository. `render.yaml` sets up one free web service
  (`python run.py serve`, health check `/health`). Nothing to build.
* **Railway, Fly.io, Koyeb:** deploy the `Dockerfile`. The server listens on the platform's `PORT`.

Free tiers sleep when idle and start fresh: a restart (or **Reset** on the Demo tab) brings back the seed data,
with no bills. The business date is today's date in India (`ELUNO_POS_BUSINESS_DATE` pins it).

## Run it

Clone or download the ZIP, open a terminal in the folder, and run:

```bash
python run.py demo      # on macOS/Linux use python3 if "python" is not Python 3
```

That one command is all. It takes about 40 seconds and ends with `Two-tills scenario complete.` Every business
result is the same on every machine and every run: offers, prices, GST, invoice and credit-note numbers,
Shopify order numbers, refunds, stock, till variances and day-end totals. Only the things that are meant to
differ change: the local port, temporary folder names, the random bill ids, clock times and measured seconds.

Other commands:

```bash
python run.py demo      # 18 feature steps, the business day and the two-tills scenario (below), on fresh flaky mocks (~40 s)
python run.py day       # just the business day: five orders across all seven stores, end to end, with day-end totals
python run.py race      # just the two-tills scenario: one last frame, two tills 1 s apart, second by second
python run.py bench     # checkout time per step, retries, many tills at once, Wi-Fi trouble (see docs/CHECKOUT_TIMING.md)
python run.py test      # 114 unit + integration tests (the integration ones run against the mock with 429/503 on)
```

`demo` pins the business date to 2026-10-22, the date the seed data is built around, so the output is the
same whenever you run it (`python run.py demo --live` uses today's date; `--no-day` skips the business day).
The 18 steps cover every Must item plus the failure cases: a lost Shopify response, a Shopify outage at the
moment of sale, a repeated `pay`, an out-of-range lens, two tills wanting the last frame, a cash drawer that
comes up short, and more.

The **business day** (`eluno_pos/day.py`) runs on its own fresh Shopify and POS database, so its five orders
are the only orders of the day across all seven stores:

| Time | Order | Store | What happens |
|---|---|---|---|
| 10:40 | 1 Rajesh | KMG | PGX15 beats the stack; terminal 503 and Shopify 429 retried; 16:30 frame-only refund |
| 11:15 | 2 Farhan | JN | card declined (nothing charged, frame released), pays cash; 18:00 full cash refund with credit note |
| 12:30 | 3 Harish + Rohit | VC | family bill, two prescriptions, SECONDPAIR50 stack beats a manager 10%; lost response adopted |
| 14:00 | 4 Varun | MM | quote goes stale (stock moved out), draft cancelled; Shopify rejects the order; manager voids; number kept as cancelled |
| 15:20 | 5 Bhavana | HSR | 1.56 lens blocked, 1.67 sold; Shopify lost mid-sale, evening sync adopts; 17:45 zero-charge remake |

It ends with all tills closed (one Rs 300 short, flagged), the day-end for every store, all-store totals, the
channel report, the founder's morning summary, and a checklist showing where each Must, Should, the stretch
and the constraints were exercised, with the evidence read back from Shopify, the terminal and the till.
A test (`BusinessDay`) fails if any of them stops being covered.

The **two-tills scenario** (`eluno_pos/race.py`) runs two POS processes on one store database, one second
apart, for the store's last unit of a frame, and prints the stock and order list both tills see:

| Case | Till 1 | What Till 2 sees |
|---|---|---|
| A | payment slow (3 s) but succeeds | at 1 s: reserved by another till; afterwards: out of stock |
| B | customer returns the frame | back on the shelf: Till 2 sells it |
| C | card declined | still reserved (the bill isn't in Shopify's orders) until Till 1 cancels the bill; then Till 2 sells it |
| D | terminal stops answering | reserved; the salesperson can't cancel, the store manager can (after checking the terminal); then Till 2 sells it |

Interactive use:

```bash
python run.py mock                                   # terminal 1: the starter-kit mock on :8787 (flaky)
python -m eluno_pos cutover                          # once: start each store's series after Shopify POS's numbers
python -m eluno_pos lookup --phone 9000000005
python -m eluno_pos quote --store HSR --staff S105 --phone 9000000005 --pair frame=FR-0003,lens=progressive/1.60/blue_cut
python -m eluno_pos bill  --store HSR --staff S105 --phone 9000000005 --pair frame=FR-0003,lens=progressive/1.60/blue_cut --pay upi
python -m eluno_pos invoice ELU/HSR/FY26/00003
python -m eluno_pos refund ELU/HSR/FY26/00003 --store HSR --staff S105 --reason CHANGED_MIND [--lines 1F]
python -m eluno_pos day-end --store HSR [--date 2026-10-22] [--json]
python -m eluno_pos -h                               # all commands: till, remake, void, sync, channels, audit, ...
ELUNO_POS_BASE_URL=http://127.0.0.1:8787 python run.py serve   # till screen + API on :8000 against that mock
```

Family billing adds `wearer=` to a pair: `--pair frame=FR-0014,lens=single_vision/1.50/anti_reflective,wearer=9000000010`.
A manager discount looks like `--discount pct=10,approver=S108,reason=WALKOUT_SAVE`. A CONFIRM check (e.g.
an axis off the 5-degree grid) is acknowledged with `--ack AXIS_OFF_GRID`. Settings: `ELUNO_POS_BASE_URL`
(default `http://127.0.0.1:8787`), `ELUNO_POS_DB` (default `var/eluno_pos.db`). API routes are listed at the
top of `eluno_pos/api/__init__.py`; money in the API is integer paise.

## What is built

**Must: all seven, end to end**

| # | Requirement | Where |
|---|---|---|
| 1 | Customer by phone (Shopify) + latest valid prescription (store tool); priority/campaign flags | `app/billing.py` `lookup`, `domain/prescription.py` |
| 2 | Cart of pairs; lens from the matrix, validated against the Rx (sphere, cylinder, ADD) with actionable reasons and fitting alternatives | `domain/lens_rules.py`, `domain/cart.py` |
| 3 | Offers from `offers.json`: dates, conditions, stackability, priority, caps, explicit policy, a reason for every offer | `domain/offers.py` |
| 4 | GST per line by HSN and date; taxable, CGST, SGST reconciled to the paisa | `domain/tax.py`, `domain/pricing.py` |
| 5 | Terminal charge, then the Shopify order with line items, tax lines, discounts, the sale transaction, Rx on the lens line, per-store invoice number | `app/billing.py` `pay`, `adapters/shopify.py` |
| 6 | Full refund via Shopify: tax reversed per line, frame restocked at the billing store, lens not, refund against the sale, credit note | `app/refunds.py`, `domain/refund.py` |
| 7 | Day-end per store and date from Shopify: bills, gross, taxable, tax, discounts, refunds, split by payment method, reconciled with the till and the POS ledger | `domain/dayend.py`, `app/reports.py` |

**Should: all five.** Cash till (float, cash sales/refunds, paid-ins/outs with reason codes, counted close,
variance flagged over Rs 200). Sales channel on every order, plus a channel-by-store report. Partial refund
by line. Printable invoice and credit note (text that reproduces `sample_invoice.txt` byte for byte, and
HTML). Audit trail (who, what, when, reason code).

**Stretch: one.** Billing-time anomaly flags: axis off the 5-degree grid, sphere change over 1.00 D,
first-time progressive (with the salesperson's badge), external prescription.

**From the stakeholder thread, beyond the brief:** family billing (one bill, several wearers and
prescriptions); zero-charge remakes that don't touch frame stock; manager discount with an approver and
reason code (no approval chain); refunds of bills made before cutover; Wi-Fi drops (bills persist; `pay`
is safe to repeat; Shopify writes are deduplicated); invoice series continuing after Shopify POS's numbers;
stock holds, so two tills can't sell the same last frame, even while Shopify is unreachable.

## How it is put together

Ports and adapters: a pure domain core (`eluno_pos/domain`), use cases (`eluno_pos/app`) that talk only to
interfaces (`eluno_pos/ports.py`), and adapters for Shopify, the store tool, the terminal and SQLite.
Shopify's shapes live in one file, `adapters/shopify.py`, and a test fails if they leak elsewhere. Details,
sequence diagrams and the failure-handling matrix: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Every
judgement call, with its reason: [docs/DECISIONS.md](docs/DECISIONS.md). Every case as a lettered flow
(A→B, A→C, …) with the decision at each branch: [docs/FLOWS.md](docs/FLOWS.md). Which function calls
which, file by file, from `python run.py demo` to the last line of output: [docs/CALL_FLOW.md](docs/CALL_FLOW.md).

The decisions most worth reading:

* **Invoice number after payment, before Shopify.** A failed payment never burns a number. A captured
  payment always has one, even when Shopify is down; the order syncs later from an outbox.
* **The invoice number is the Shopify idempotency key.** After an ambiguous write, the adapter looks the
  number up before trying again and adopts the order it finds.
* **Best offer for the customer, explained.** Each offer shows why it was or wasn't applied. The policy is
  one setting.
* **Day-end reads Shopify and checks it against the till and the POS ledger.** Mismatches are listed, not
  hidden.

## Tests

`python run.py test` runs 114 tests in about 45 seconds:

* **Tax**: every line of the 12 seed orders back-computed to the paisa; rates by date; a rate change is a data change.
* **Offers**: stacking vs priority, caps, dates, conditions, second-pair tie-break, manual override rules, offers-as-data, malformed offers rejected.
* **Refunds**: seed refund reproduced exactly; partial refunds; double refund refused; tax reversed at the charged rate.
* **Clinical**: Rx selection (future-dated, expired), lens limits, ADD rules, anomaly flags, stock and Rx-able frame checks.
* **Till and day-end**: seed till sessions replayed; day-end figures checked against the seed orders.
* **Shopify mapping**: payload shapes; every seed order round-trips; the sample invoice rendered byte for byte.
* **HTTP retries**: 429 Retry-After, 503, lost responses, 4xx, pagination.
* **Repository**: gapless numbering with 8 threads; rollback never burns a number.
* **Architecture**: the domain is pure; Shopify stays in its adapter.
* **Integration** (flaky mock plus injected faults):
  * the full Must flow;
  * a lost order response, a lost response *and* a failed lookup, an outage then `sync`, and a void, with exactly one Shopify order each time;
  * a refund that survives a lost response and an outage;
  * cash with the till;
  * family billing, a pre-cutover refund, a remake;
  * the HTTP API: parallel stores, a double-clicked Pay charging once, the till screen's endpoints (live stock
    minus reserved frames, refundable lines), the per-request network trace, static files without path traversal;
  * the hosted demo: Shopify refusing orders then syncing, and reset to the seed.

The till screen itself was driven end to end in headless Chrome at laptop and tablet widths (lookup, offers,
declined card then UPI, Shopify outage then sync, two tills on the last frame, refund, cash drawer, day-end)
with the mock's 429/503 on and no console errors.

## Known gaps and what's next

* **Not built:** split payments (part cash, part card); quantity above 1 on a line (each unit is its own line);
  staff authentication (staff ids are trusted; production would use the store tool's SSO and a manager PIN).
* **Policies not enforced:** the 7-day frame return window and "lenses not returnable once dispensed" are
  printed on the invoice but not enforced; that needs Finance and Retail's rules.
* **Recorded, not sent:** the finance alert for till variance goes to the audit trail and the log. The 10 am
  founder email isn't built, but the per-store day-end JSON it needs exists.
* **Mock limits:** the mock has no cursor pagination. The adapter follows `Link` headers, but that path is only
  unit-tested. Shopify is steering new apps to the GraphQL Admin API; behind the port that is an adapter change.
* **Deployment:** SQLite on one node. With several tills on different machines, the same repository interface
  goes on a central Postgres, and `sync` runs on a timer (today it runs on start-up and on demand). The hosted
  demo has no sign-in: anyone with the link can bill, and reset it.
* **Out of scope per the brief:** e-invoicing (IRN), printer hardware. Customer and prescription creation were
  out of scope too; they are now built, against write endpoints added to the mock (below). In production they need
  Shopify's customer write scope and a prescription write API in the store tool.

## Where AI tools helped, and where they misled me

I built this with Claude Code as my main tool, and it wrote most of the code. My job was deciding what to build, pushing back when the output didn't fit the brief or the data, and refusing to believe anything that wasn't backed by a test, a measurement or the seed files. Below is what it was good at, and where I had to correct it.

### Where it helped

* **Breadth, fast.** In a few hours it produced the shape of a real system: ports and adapters, a bill state machine, an outbox to Shopify, gapless invoice series, refunds, the cash drawer and day-end. That left my time for judgement: what happens when the card machine stops answering, and who is allowed to cancel then.
* **Reading the data closely.** It found things in the seed files that were easy to miss:
  * prescriptions dated after "today", which are data-entry errors that must never be billed against;
  * an external prescription with an axis off the 5° grid;
  * seed order #1001, where a better offer combination existed (₹1,800 off against the ₹1,424.85 the old system gave).
* **Tools to test failure.** The mock can only fail in one way: random 429s and 503s. The AI built a fault injector that can also lose a response after the server has applied the write, take Shopify down, decline a card or stall a UPI approval. It also built a timing bench that simulates Shopify's rate limit. Most of the hard cases in this repo exist because these tools made them cheap to reproduce.
* **Showing what's happening.** Every API response carries a trace of the Shopify, store-tool and terminal calls it made. The till screen shows each 429 and 503, and the retry that absorbed it, while you bill. That came from asking it "how would a reviewer *see* the retries?"
* **Tests as a habit.** It wrote tests alongside the code: money math to the paisa against all 12 seed orders, idempotency, two tills racing for one frame.

### Where it misled me

Both mistakes were the same kind: the AI had the right pieces, but put the decisions in the wrong order. With money, the order of the steps *is* the logic.

**1. The offer algorithm**

The AI's first offer engine got the order of its decisions wrong. It weighed offers against each other before settling which ones were allowed on this bill and which could be combined. When the order is wrong, the till can show a discount that the rules don't allow, combine offers that must not be combined, and tell the salesperson the wrong reason. On a festive day, with eight offers live, that becomes either lost margin or an argument at the counter. I rewrote the algorithm in three steps, in this order:

1. **Check the limits first.** Each offer must pass its own rules before it is considered: active, within its dates, the customer's tag (priority, campaign), the minimum number of pairs, something in the cart it applies to, and a manager's approval and reason for the manual discount. An offer that fails is dropped, and the reason is recorded.
2. **Then stackable or not.** Each non-stackable offer is tried on its own. All the stackable offers are tried together, in priority order, each on what is left of the line after the one before, with each offer's cap applied.
3. **Then the highest.** The combination that saves the customer the most wins (ties go to priority, and the policy is one setting). Every offer that lost gets a reason saying what beat it.

On Karthik Reddy's progressive bill at HSR, this gives three stackable offers together: the free blue-cut upgrade (₹1,000), the priority customer offer (₹500) and frame + lens together (₹300), so ₹1,800 off. The other five offers show on screen with the reason each didn't apply. `tests/test_offers.py` pins this behaviour down: stacking versus priority, caps, dates, conditions, the second-pair tie-break and the manual-discount rules.

**2. The order of the payment steps**

The AI's first payment flow put the steps in a risky order. It charged the customer first, and only then set up the store's invoice series by asking Shopify for the last number used. In every normal run that worked. But a test that takes Shopify down at a store's first sale showed what really happens: the customer is charged, and no invoice number can be issued. Separately, nothing stopped two tills from selling the same last frame while one customer was still paying. I set the order so that anything that can fail happens *before* money moves:

1. **Check the price and stock again.** If anything changed since the customer said yes, stop and confirm with them.
2. **Reserve the frame.** Other tills now see it as reserved, so they can't sell it while this customer pays.
3. **Make sure the invoice series is ready.** If Shopify is unreachable and the series isn't set up, refuse here, with nothing charged.
4. **Charge on the card machine,** and save the charge id at once, so a retry can never charge twice.
5. **Issue the invoice number,** in the same database step that marks the bill paid.
6. **Create the Shopify order.** If Shopify is down, the customer still leaves with the invoice and the order is sent later. The invoice number makes sure it is sent only once.

Each step is saved before the next one starts. So a Wi-Fi drop, a declined card or a Shopify outage at any point leaves a bill that can safely be retried or cancelled, never a customer who was charged without an invoice.

It is very fast and genuinely useful, but nothing it decides is right until the order of the steps has been checked against what happens when one of them fails.

## Changes to the starter kit

The starter kit is in `starter-kit/` so that one clone runs. Changes to `mock_server.py`, each marked
"eluno-pos addition" in the file:

* `--today YYYY-MM-DD` pins the date it stamps on orders (in IST, so the demo gives the same result in any
  timezone), for reproducible demos and tests. Without the flag it behaves as shipped.
* `POST /admin/api/2026-10/customers.json` creates a customer, the way Shopify does (422 if the phone is
  already taken; writes can 429 like the others).
* `POST /smt/prescriptions` records a prescription in the store tool and gives it the next RX number.

POS-owned data (tax rates, offers) is copied to `config/`, with `reason_codes.json` and `pos_settings.json` added.
