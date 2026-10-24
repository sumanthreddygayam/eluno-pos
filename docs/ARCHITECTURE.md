# Architecture

## Shape: ports and adapters

```
             CLI (eluno_pos/cli.py)        HTTP API (eluno_pos/api)        demo / tests
                        \                         |                          /
                         +------------------ application ------------------+
                         |  app/billing.py   lookup, quote, create, pay, sync, cancel, void, cutover
                         |  app/refunds.py   full / partial refund, credit note, resume
                         |  app/remakes.py   zero-charge lens remake
                         |  app/tills.py     open, paid-in/out, close, variance
                         |  app/reports.py   day-end, channel report
                         +-----------+------------------------+-----------+
                                     | uses                   | uses
                         +-----------v-----------+   +--------v---------------------------+
                         |  domain/  (pure)      |   |  ports.py  (interfaces)            |
                         |  money, tax, offers,  |   |  CommercePlatform  PrescriptionSource
                         |  prescription, lens   |   |  StaffDirectory    PaymentTerminal  |
                         |  rules, cart, pricing,|   +--------^---------------------------+
                         |  bill, refund, till,  |            | implemented by
                         |  day-end, documents   |   +--------+---------------------------+
                         +-----------------------+   |  adapters/                         |
                                                     |  shopify.py   Shopify Admin REST   |
                                                     |  smt.py       store tool           |
                                                     |  terminal.py  payment terminal     |
                                                     |  http.py      retries, timeouts    |
                                                     |  sqlite_repo  POS-owned state      |
                                                     +------------------------------------+
```

* **domain/** has no I/O and imports nothing outside the standard library and itself. Money is `int` paise,
  dioptres are `int` hundredths. `tests/test_architecture.py` enforces this.
* **app/** orchestrates use cases against the ports. It never imports the Shopify, SMT, terminal or HTTP
  adapters (also enforced), and contains no Shopify field names (enforced too).
* **adapters/shopify.py** is the only code that knows `line_items`, `tax_lines`, `note_attributes`,
  decimal-string money, `inventory_behaviour`, `restock_type`. Its mapping is pure functions,
  tested without a server.
* **app/\_\_init\_\_.py** (`PosApp`) is the composition root: the one place that picks concrete adapters.

### If Shopify is replaced in 2027
Write a new `CommercePlatform` (customers, stores, catalog, stock, create/find order, quote/create refund,
sales for a day, highest document number). Wire it in `PosApp`. Nothing in `domain/` or the use cases
changes. Data to migrate: the invoice/credit-note register stays in the POS database, so legal numbering
survives the switch.

## Who owns what

| Data | Owner | Notes |
|---|---|---|
| Customers, products, stock, orders, payments, refunds | Shopify | finance reconciles there |
| Prescriptions, staff | Store tool (SMT) | read-only from the till |
| Invoice and credit-note series, cancelled numbers | POS (`documents`, `series` tables) | gapless per store, per prefix |
| Bills in flight / outbox | POS (`bills`, `refunds`) | state machine, survives crashes |
| Till sessions and cash movements | POS (`till_sessions`) | one open session per store (DB constraint) |
| Stock holds (frames being paid for, or paid but not yet in Shopify) | POS (`stock_holds`) | subtracted from Shopify's count; released once Shopify has the sale |
| Audit trail | POS (`audit`) | who, what, when, reason code, details |
| Tax rates, offers, reason codes, thresholds | POS config (`config/*.json`) | data, validated at load |

## Checkout (pay)

```
till                 POS                                   terminal        SQLite              Shopify
 |  pay(bill, upi)    |                                        |              |                   |
 |------------------->| re-quote: stock, Rx, offers, tax       |              |                   |
 |                    |------------------------------------------------------------------------->| stock, catalog
 |                    | changed? -> refuse, nothing charged    |              |                   |
 |                    | series ready? (else refuse)            |              |                   |
 |                    | status=PAYMENT_PENDING ----------------------------->|                   |
 |                    | charge(amount, order_ref=bill_id) ---->|              |                   |
 |                    |   503 -> retry same order_ref          |              |                   |
 |                    | charge_id ------------------------------------------>| (persisted now)   |
 |                    | BEGIN: allocate invoice no, status=PAID, cash->till  |                   |
 |                    |        COMMIT ------------------------------------->|                   |
 |                    | create order (invoice no in note_attributes) ------------------------------>|
 |                    |   429 -> wait Retry-After, retry (not applied)                             |
 |                    |   lost response / 5xx -> find by invoice no; adopt if there, else retry    |
 |                    | status=SYNCED (order id, line ids, sale txn id) ---->|                   |
 |<-------------------| invoice                                              |                   |
```

If Shopify is unreachable after payment the bill stays **PAID**: the invoice is valid, the customer leaves,
and `sync` (on start-up, on demand, or on a timer) pushes it later. Day-end lists it as a finding until then.

## State machines

```
Bill:    DRAFT -> PAYMENT_PENDING -> PAID -> SYNCED
           |            (no charge yet)  |
           +-> CANCELLED                 +-> VOIDED (money returned before Shopify; number kept as cancelled)

Refund:  REQUESTED -> MONEY_RETURNED -> SYNCED
           +-> FAILED (no money moved)
```

Every transition is persisted before the next side effect, so any crash leaves a state that says exactly
what to do next. Repeating `pay` on any bill is safe.

## Failure handling matrix

| Call | Failure | Behaviour |
|---|---|---|
| any | 429 | wait `Retry-After` (capped), retry; the request was not applied |
| any | connection refused | retry; nothing left the machine |
| GET, refunds/calculate | 5xx, timeout | retry with jittered backoff (5 attempts, ~2 s worst case) |
| terminal charge | 503, timeout | retry with the same `order_ref` (idempotent by contract) |
| Shopify order/refund create | 5xx, lost response | `AmbiguousWrite`: look up by invoice / credit-note number, adopt if found, else retry |
| terminal refund | lost response | read the charge's refunded amount back before deciding to retry |
| any | 4xx | no retry; error surfaced with Shopify's message |
| invoice series | not initialised and Shopify down | refuse before charging (`series_not_ready`) |

## Running modes

* **CLI**: `python -m eluno_pos ...` (or `python run.py pos ...`), one process per command, state in SQLite.
* **HTTP API**: `python run.py serve`; one request at a time (a lock), enough for ~120 bills/day across 7 stores.
  Production: one central service with Postgres behind the same repository interface; tills are browsers.
