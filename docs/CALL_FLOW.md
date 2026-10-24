# Call flow: which function calls which, start to end

This traces `python run.py demo` function by function, in the order things happen. Each arrow `→` is a
call; indentation means "called from the line above". File names are clickable. Paths are relative to the
repo root; `domain/`, `app/` and `adapters/` live under `eluno_pos/`.

The same use cases run when you use the CLI (§8) or the HTTP API: only the first two or three calls differ.

Contents: [1 Start](#1-start-runpy) · [2 Mock starts](#2-the-mock-starts) · [3 Wiring](#3-wiring-posapp)
· [4 Anatomy of one HTTP call](#4-anatomy-of-one-http-call) · [5 The demo steps](#5-the-demo-steps)
· [6 End](#6-end) · [7 What the mock does with each call](#7-what-the-mock-does-with-each-call)
· [8 CLI instead of demo](#8-cli-instead-of-demo) · [9 Tests](#9-tests)

---

## 1. Start: run.py

```
you: python run.py demo
run.py  main(argv=["demo"])                                    run.py:30
  → from eluno_pos.demo import run
  → run(argv[1:])                                              demo.py:81
```

[run.py `main`](../run.py#L30) only reads the first word (`demo`, `test`, `serve`, `mock`, `pos`) and hands over.

## 2. The mock starts

```
demo.py  run()                                                 demo.py:81
  → sys.stdout.reconfigure(utf-8)          (Windows console, so ₹ prints)
  → logging.basicConfig(...)               (retry/adoption warnings print as "log: ...")
  → tempfile.mkdtemp() → db = ...\eluno_pos_demo_xxxx\pos.db   (fresh database every run)
  → today = "2026-10-22"                   (DEMO_DATE; None with --live)
  → MockServer(flaky=True, today=today)                        devtools.py:27
      → free_port()                         asks the OS for an unused port            devtools.py:21
      → __enter__()                                                                    devtools.py:37
          → subprocess.Popen([python, starter-kit/mock-apis/mock_server.py, --port N, --today 2026-10-22])
          → loop: urlopen(url + "/health") until the mock answers (up to 15 s)
```

Inside the child process, [mock_server.py](../starter-kit/mock-apis/mock_server.py#L43) builds one dict `S`
from `starter-kit/data/`: customers, locations, products, inventory levels, the 12 sample orders,
prescriptions and staff. That dict *is* "Shopify + store tool + terminal" for this run.

```
  → FaultInjector()                                                                    devtools.py:61
      a transport wrapper the demo uses later to drop responses / fake outages
```

## 3. Wiring: PosApp

```
demo.py run()
  → Settings(base_url=mock.url, db_path=db, business_date="2026-10-22")               config.py:23
  → PosApp(settings, transport=faults)                                                app/__init__.py:23
      → PinnedDateClock(2026-10-22)        today is fixed, time of day is real        app/common.py:34
      → BusinessConfig.load("config/")                                                config.py:61
          → _load("tax_rates.json")  → TaxTable.from_json → TaxRate(...) per row      domain/tax.py:70
                                       (overlapping rates → ConfigError)
          → _load("offers.json")     → OfferBook.from_json → parse_offer per offer    domain/offers.py:249, :170
                                       (bad offers go to book.rejected, never used)
          → _load("reason_codes.json"), _load("pos_settings.json")
      → client("shopify"), client("smt"), client("terminal")
          → HttpClient(base_url, service, transport=faults, policy=RetryPolicy())     adapters/http.py:66
      → ShopifyAdmin(http["shopify"])                                                 adapters/shopify.py:258
      → StoreTool(http["smt"])                                                        adapters/smt.py:28
      → Terminal(http["terminal"])                                                    adapters/terminal.py:22
      → SqliteRepository(db)          runs SCHEMA: bills, refunds, series, documents,
                                      till_sessions, audit                            adapters/sqlite_repo.py:49
      → Audit(repo, clock)                                                            app/common.py:80
      → Directory(commerce, store_tool)                                               app/common.py:49
      → BillingService(...)   RefundService(...)   RemakeService(...)
        TillService(...)      ReportService(...)                                      app/*.py
  → _scenario(app, mock.url, faults)                                                  demo.py:102
```

[PosApp](../eluno_pos/app/__init__.py#L23) is the only place that names concrete adapters. Every service
receives the adapters as constructor arguments and talks to them through the interfaces in
[ports.py](../eluno_pos/ports.py).

## 4. Anatomy of one HTTP call

Every network call in every step goes through the same path. Example: find Karthik by phone.

```
ShopifyAdmin.find_customer_by_phone("+919000000005")                                  adapters/shopify.py:269
  → HttpClient.get("admin/api/2026-10/customers/search.json", {"query": "phone:+91..."})   adapters/http.py:84
      → url()   builds http://127.0.0.1:N/admin/api/2026-10/customers/search.json?query=...
      → _send("GET", url, None, idempotent=True)                                       adapters/http.py:102
          → self.transport(...)  = FaultInjector.__call__                              devtools.py:83
              no rule matches → urllib_transport(...)                                  adapters/http.py:40
                  → urllib.request.urlopen → mock do_GET → JSON bytes
          → _decode(raw) → dict
          → status 2xx → _watch_call_limit(headers) → return (payload, headers)
          (429 → _retry_after + _pause + loop · 5xx/timeout on GET → backoff + loop ·
           4xx → UpstreamRejected · see §5 step 5 and step 8 for the write cases)
  → customer_from(dict) → Customer(...)                                                adapters/shopify.py:40
```

The pure mapping functions in `adapters/shopify.py` (`customer_from`, `store_from`, `catalog_from`,
`bill_from_order`, `sale_record_from_order`, `order_payload`, `refund_payload`, `receipt_from`) are the
only code that knows Shopify's JSON. Everything above them gets domain objects.

---

## 5. The demo steps

`_scenario` ([demo.py:102](../eluno_pos/demo.py#L102)) calls `step(...)` to print a header, then calls a service.

### Step 1: Cutover

```
_scenario → app.billing.cutover()                                                     app/billing.py:264
  → directory.stores() → ShopifyAdmin.stores()                                        adapters/shopify.py:274
      → GET locations.json → store_from(...) × 8 (cached after the first call)
  → for each billing store (HQ skipped: Store.is_billing_store), for INVOICE and CREDIT_NOTE:
      → ensure_series(store, kind)                                                    app/billing.py:248
          → series_key(prefix, kind)           "ELU/HSR/FY26/" or "ELU/HSR/FY26/CN/"  domain/documents.py
          → repo.series_exists(series) → False (new database)
          → commerce.highest_document_seq(store, series)                              adapters/shopify.py:316
              → _orders(store) → get_all(orders.json?location_id=…&status=any)
              → parse_seq(invoice_no / credit_note_no, series) → highest = 2 for HSR
          → repo.init_series(series, floor=2)
          → audit("system", "series.initialised", …) → repo.audit(...)
      → repo.series_info(series) → {"floor": 2, "last": 2}
```
**You see:** `ELU/HSR/FY26/  continues after 00002`.

### Step 2: Customer lookup

```
_scenario → b.lookup("90000 00005")                                                   app/billing.py:130
  → customer(phone)                                                                   app/billing.py:120
      → normalize_phone("90000 00005") → "+919000000005"                              domain/parties.py:13
      → commerce.find_customer_by_phone(...)            (§4)
  → rx_selection(phone)                                                               app/billing.py:127
      → store_tool.prescriptions(phone)                                               adapters/smt.py:34
          → GET smt/prescriptions?phone=… → prescription_from(...) per row            adapters/smt.py:14
              → to_cd(-3.25) → -325 (dioptres as integers)                            domain/optics.py
      → select_prescription(rxs, clock.today())                                       domain/prescription.py:78
          skips future-dated and expired ones, picks RX5007
  → anomaly_flags(None, selection)                                                    domain/lens_rules.py:72
  → returns a dict; the demo prints customer + prescription id
```
**You see:** Karthik's JSON, then Kavya with `prescription=None` and three skipped Rx with reasons.

### Step 3: Lens validated against the prescription (four refusals)

```
_scenario → expect_error(lambda: b.create(req), "Sanjay, 1.56 progressive")          demo.py:40
  → BillingService.create(req)                                                        app/billing.py:196
      → quote(req)                                                                    app/billing.py:145
          → directory.store("BR")                                                     app/common.py:59
          → directory.staff(store, "S111") → store_tool.staff("BR") (GET smt/staff, cached)
          → validate_channel("walk_in")                                               domain/parties.py:27
          → customer("+919000000021")
          → commerce.catalog()        GET products.json Frame + Lens → catalog_from → Catalog (cached 5 min)
          → for pair 1:
              → catalog.frame("FR-0007")                                              domain/catalog.py:99
              → LensChoice.parse("progressive/1.56/blue_cut") → normalize_index       domain/catalog.py:84
              → catalog.lens(choice)                                                  domain/catalog.py:105
              → rx_selection(wearer phone) → RX5034 (sph -5.75)
              → check_lens(lens, sel, today, catalog, 1)                              domain/lens_rules.py:39
                  → _eye_problems(...) → "OD sphere -5.75 is outside …"               domain/lens_rules.py:21
                  → catalog.lens_family("progressive","blue_cut") → fits(...) each → "1.60, 1.67, 1.74"
                  → Issue("LENS_OUT_OF_RX_RANGE", BLOCK, …)                           domain/issues.py:16
              → anomaly_flags(lens, sel, staff, 1)
              → Pair(1, wearer, sel, frame, lens)                                     domain/cart.py:23
          → Cart(...) → structural_issues(cart)                                       domain/cart.py:81
          → commerce.stock_levels([item ids]) → stock_issues(...)                     domain/cart.py:97
          → config.offer_engine().evaluate(...)          (detail in step 4)
          → price(...) → totals(...)                     (detail in step 4)
          → blocking(issues, acknowledge) → [the BLOCK]                               domain/issues.py:36
          → Quote(...)
      → q.blocking is not empty → raise BillBlocked(...)                              domain/errors.py
  → expect_error catches BillBlocked → prints "BLOCKED" + each Issue
```
The same path runs for Kavya (`check_lens` → `RX_NOT_VALID`), Rahul (`ADD_MISSING`) and Suresh
(`anomaly_flags` → `AXIS_OFF_GRID`, a CONFIRM that `blocking()` counts because it wasn't acknowledged).

### Step 4: Offers, GST, charge, Shopify order (Karthik)

```
_scenario → pay(app, req, "upi")              demo helper                             demo.py:68
  → app.billing.create(req)                                                           app/billing.py:196
      → quote(req)          as in step 3, no issues this time. The two parts skipped there:
          OFFERS
          → cart.lines() → CartLine × 2 → .offer_line() → OfferLine × 2               domain/cart.py:71, :55
          → OfferEngine.evaluate(lines, OfferContext(today, tags, "HSR", manual=None))   domain/offers.py:393
              → _check_manual(ctx) → None (no manager discount asked)
              → for each offer in priority order:
                  → _ineligible(o, lines, ctx)   inactive / dates / manual / tag / pairs   domain/offers.py:315
                  → _targets(o, lines)           which lines or pairs it covers           domain/offers.py:331
              → candidates: [FESTIVE10], [PGX_CAMPAIGN15], [FREE_BLUECUT, PRIORITY500, BUNDLE300]
              → _apply(candidate, …) for each   percent_of / allocate, priority order    domain/offers.py:361
              → _pick(outcomes) → best_for_customer → the ₹1,800 stack                    domain/offers.py:388
              → _lost_why(...) for the losers ("does not combine…")                       domain/offers.py:428
              → OfferEvaluation(allocations, decisions)
          GST
          → price([(line_ref, mrp, hsn)…], evaluation, taxes, today)                     domain/pricing.py:54
              → price_line(...) per line                                                  domain/pricing.py:46
                  → evaluation.for_line(ref) → discounts on that line
                  → taxes.rate_for(hsn, today) → TaxRate(1800 or 500)                     domain/tax.py:80
                  → back_out_gst(gross, rate_bps) → LineTax(taxable, cgst, sgst)          domain/tax.py:49
                      → div_round_half_up(...)                                            domain/money.py
          → totals(priced) → Totals (raises if it doesn't reconcile)                    domain/pricing.py:59
      → build BillLine × 2 (prices, tax, Rx JSON via Prescription.line_property())     domain/bill.py:30
      → Bill(... status=DRAFT, request=req.to_dict())                                  domain/bill.py:73
      → repo.save_bill(bill) → _upsert("bills", …)                                     adapters/sqlite_repo.py:142
      → audit(..., "bill.created")
  → print_quote(q)                                                                     cli.py:49
  → app.billing.pay(bill_id, "upi", "S105")                                            app/billing.py:274
      → get(bill_id) → repo.get_bill → Bill.from_dict                                  status DRAFT
      → directory.store("HSR")
      → _recheck(bill)                                                                 app/billing.py:332
          → quote(BillRequest.from_dict(bill.request)) → compare every line → same
      → ensure_series(store, INVOICE) → exists already (step 1)
      → bill.start_payment("upi") → status PAYMENT_PENDING                             domain/bill.py:139
      → _save(bill) → repo.save_bill;  audit "payment.started"
      → terminal.charge(total, "upi", order_ref=bill_id)                               adapters/terminal.py:26
          → HttpClient.post("terminal/charges", body, idempotent=True)
              → _send → mock answers 503 (first time for this order_ref) → backoff → resend → 201
          → _charge(dict) → Charge(charge_id="chg_…", status="captured")
      → bill.charge_id = …;  _save(bill)          (persisted before anything else can fail)
      → with repo.transaction():                  BEGIN IMMEDIATE                       adapters/sqlite_repo.py:67
          → repo.allocate("ELU/HSR/FY26/", …) → "ELU/HSR/FY26/00003"                   adapters/sqlite_repo.py:109
              → format_number(series, 3)                                               domain/documents.py
          → bill.mark_paid(invoice_no, now, charge_id) → status PAID                   domain/bill.py:151
          → _save(bill);  audit "bill.paid"
        COMMIT
      → sync(bill)                                                                     app/billing.py:344
          → bill.note_sync_attempt() → sync_attempts = 1;  _save(bill)  (write-ahead)
          → commerce.create_order(bill, store, may_exist=False)                         adapters/shopify.py:326
              → order_payload(bill)  money → decimal strings, tax_lines, Rx properties,
                                     invoice_no in note_attributes, transaction            adapters/shopify.py:177
              → client.post("admin/api/2026-10/orders.json", payload, idempotent=False)
                  → (maybe 429 → wait Retry-After → resend; Shopify did nothing)
                  → mock creates order #1013, decrements FR-0003 at HSR
              → receipt_from(order, bill) → OrderReceipt(order_id, "#1013", line ids)    adapters/shopify.py:245
          → bill.mark_synced(...) → status SYNCED                                      domain/bill.py:167
          → _save(bill);  audit "bill.synced"
      → audit "bill.timing" (seconds)
  → print_bill(bill)                                                                   cli.py:69
_scenario → stock(...), orders_with_invoice(...)   the demo reads the mock directly to prove it
_scenario → render_text(bill, store)                                                   invoice_render.py:32
  → _kv(...), _row(...), _rate(...), format_inr(...), amount_in_words(...)             domain/money.py
```
**You see:** the quote table, the offer list with reasons, `status=SYNCED invoice=ELU/HSR/FY26/00003 shopify=#1013`,
the stock drop, and the printed invoice.

### Step 5: Wi-Fi drop: response lost, no duplicate

```
_scenario → faults.drop_response("POST", "/orders.json")                              devtools.py:74
_scenario → pay(app, Sanjay 1.60 req, "card")       same as step 4 until create_order:
  → ShopifyAdmin.create_order(...)
      → client.post(orders.json, …)
          → FaultInjector.__call__ → the mock DOES create the order → then raises TransportError
          → _send: not idempotent + no response → raise AmbiguousWrite                adapters/http.py:102
      → except AmbiguousWrite → log "checking Shopify before retrying"
      → _find_order_raw(invoice_no, store, around=today)                              adapters/shopify.py:303
          → _orders(store, yesterday, tomorrow) → find note_attributes invoice_no
      → found → receipt_from(found, bill, adopted=True)
  → mark_synced; audit "bill.synced" with adopted_existing=True
_scenario → b.pay(sanjay.bill_id, "card")   again
  → status SYNCED → returns the bill, no calls
```
**You see:** `log: order create … ambiguous`, `orders in Shopify with ELU/BR/FY26/00002: 1`, and the same charge id.

### Step 6: Family billing

```
_scenario → expect_error(create(... FR-0005 at IN ...))
  → quote → stock_issues → OUT_OF_STOCK (hint lists the stores that have it) → BillBlocked
_scenario → pay(app, family req, "upi")
  → quote: pair 2 has wearer_phone → customer("+919000000010") → Shreya
            → rx_selection(Shreya's phone) → RX5015  (one fetch per wearer, cached in `selections`)
  → create: BillLine for 2L carries wearer_customer_id, wearer_name, wearer_phone
  → pay → … → order_payload adds for_customer_id + wearer properties on Shreya's lines
```

### Step 7: Manager discount refused three ways

```
_scenario → expect_error(create(walkout with approver S106))
  → quote → directory.staff(store, "S106") → ManualDiscount(...)
  → OfferEngine.evaluate → _check_manual(ctx)                                          domain/offers.py:297
      → S106 is "sales" → raise PermissionDenied("approver_not_allowed")
  (same function: 15% → manual_discount_too_high; reason "" → reason_code_required)
_scenario → replace(walkout, approver S108, WALKOUT_SAVE)   kept for step 8
```

### Step 8: Shopify outage at the moment of sale

```
_scenario → faults.outage("POST", "/orders.json");  faults.outage("GET", "/orders.json")
_scenario → pay(app, walkout, "upi")    terminal charge works; then:
  → sync → create_order
      → client.post(orders.json) → injected 503 → _send: not idempotent → AmbiguousWrite
      → _find_order_raw → client.get_all(orders.json) → injected 503 × 5 → backoff each time
          → IntegrationError (out of attempts)
      → propagates out of create_order
  → sync: except IntegrationError → bill.sync_failed(error) → _save → audit "bill.sync_failed"
  → bill stays PAID (invoice ELU/HSR/FY26/00004 already issued)
_scenario → faults.clear()
_scenario → app.reports.day_end("HSR", today)          (detail in step 15)
  → reconcile(...) → Finding("error", "payment captured … not in Shopify yet")         domain/dayend.py:179
_scenario → app.sync_pending()                                                          app/__init__.py:48
  → billing.pending() → repo.bills(statuses=[PAID, PAYMENT_PENDING])
  → billing.sync(bill) → may_exist=True (an attempt was made)
      → create_order → _find_order_raw first → not there → POST → created → SYNCED
  → refunds.pending() → nothing
```

### Step 8 (stock) and step 9: Stock holds

```
pay (DRAFT branch), after ensure_series                                                app/billing.py:274
  → bill.frames_needed() → {(inventory_item_id, location_id): qty} for frame lines        domain/bill.py
  → commerce.stock_levels(items)          fresh Shopify count
  → with repo.transaction():
      → repo.hold_stock(bill_id, wanted, levels)                                          adapters/sqlite_repo.py
          → holds(keys, exclude_bill=bill_id)  other bills' holds
          → Shopify count − others < needed → StateConflict("stock_held")  (nothing charged)
          → else INSERT INTO stock_holds
      → bill.start_payment(method); _save(bill)
quote(...)  → repo.holds(keys, exclude_bill) → stock_issues(..., held) → STOCK_HELD issue      domain/cart.py
released by → payment declined · cancel · void · sync success (same save as mark_synced) → repo.release_stock
```

Step 8 prints Shopify's count and the hold during the outage, shows another till blocked with `STOCK_HELD`,
then shows the count at 0 and the hold gone after `sync`. Step 9 has two drafts for HSR's one FR-0005: the
first payment hangs at the terminal (hold kept), the second till is refused, and once the first completes the
second simply sees `OUT_OF_STOCK`. The steps after it are numbered one higher than the headings below.

### Step 9: Cash bill with the till; axis acknowledged

```
_scenario → app.tills.open("JN", "S120", 300000)                                       app/tills.py:26
  → directory.store/staff → repo.till_exists(...) → TillSession(...) → repo.save_till → audit
_scenario → pay(app, Suresh req with acknowledge=("AXIS_OFF_GRID",), "cash")
  → quote → anomaly_flags → AXIS_OFF_GRID (CONFIRM) → blocking(issues, ack) → []
  → create → audit "issue.acknowledged"
  → pay: method cash → repo.open_till("JN") must exist → … no terminal call …
      → inside the transaction: till.record("cash_sale", total, invoice_no, …)        domain/till.py:56
                                repo.save_till(till)
_scenario → app.tills.move("JN", "S120", "paid_out", 12000, "COURIER")                app/tills.py:42
  → config.require_reason("paid_out", "COURIER")                                       config.py:77
  → transaction: current() → init "PO-JN-" series → repo.allocate → "PO-JN-00001"
                 → session.record("paid_out", …) → save_till → audit
  (twice more: TEA_SNACKS paid-out, CHANGE_FROM_HQ paid-in)
```

### Step 10: Full refund (Karthik)

```
_scenario → app.refunds.refund("ELU/HSR/FY26/00003", "HSR", "S105", "CHANGED_MIND")   app/refunds.py:44
  → directory.store/staff;  config.require_reason("refund", …)
  → find_bill(invoice_no, store) → repo.get_bill_by_invoice → found locally           app/refunds.py:33
  → plan_refund(bill, None, refunded_line_refs(repo.refunds(bill_id)))                  domain/refund.py:117
      → RefundLine × 2: frame restock=True at 70000002, lens restock=False
  → Refund(... status REQUESTED)                                                       domain/refund.py:43
  → checks: UPI → bill.charge_id exists
  → billing.ensure_series(store, CREDIT_NOTE) → exists (step 1)
  → commerce.quote_refund(bill, refund) → POST refunds/calculate.json → RefundQuote     adapters/shopify.py:348
      maximum_refundable ≥ amount → OK
  → repo.save_refund;  audit "refund.requested"
  → terminal.refund(charge_id, amount, reason)                                          adapters/terminal.py:35
      → get_charge → POST terminal/refunds → TerminalRefund("trf_…")
  → refund.terminal_refund_id = …;  repo.save_refund          (persisted immediately)
  → _complete(refund, store)                                                            app/refunds.py:98
      → transaction: repo.allocate(CN series) → "ELU/HSR/FY26/CN/00003"
                     refund.mark_money_returned(...) → save_refund → audit
      → sync(refund)                                                                    app/refunds.py:123
          → note_sync_attempt → save
          → commerce.create_refund(bill, refund, may_exist=False)                        adapters/shopify.py:361
              → refund_payload(...) frame "return"+location, lens "no_restock", parent_id = sale txn
              → POST orders/{id}/refunds.json → mock restocks FR-0003 at HSR
          → refund.mark_synced(id) → save → audit "refund.synced"
_scenario → print_refund(...)                                                           cli.py:76
_scenario → expect_error(refund again)
  → plan_refund → every line already refunded → StateConflict("already_refunded")
```

### Step 11: Partial refund and its credit note

```
_scenario → app.refunds.refund(family.invoice_no, "IN", "S121", "DEFECTIVE_FRAME", ["2F"])
  → same as step 10, but plan_refund(bill, ["2F"], …) → only Shreya's frame
_scenario → render_credit_note_text(partial, bill, store)                               invoice_render.py:67
```

### Step 12: Refund of a pre-cutover bill (cash)

```
_scenario → app.refunds.refund("ELU/JN/FY26/00002", "JN", "S117", "CHANGED_MIND")
  → find_bill → repo.get_bill_by_invoice → None (this POS never issued it)
      → commerce.find_order(invoice_no, store)                                          adapters/shopify.py:308
          → _find_order_raw(…, around=None) → order #1011 from orders_sample.json
          → bill_from_order(order, store, catalog) → Bill(status SYNCED, source "imported")   adapters/shopify.py:85
      → repo.save_bill;  audit "bill.imported"
  → plan_refund → method "cash"
      → repo.open_till("JN") exists; drawer holds ≥ ₹5,498 → OK
  → quote_refund → save REQUESTED → (no terminal for cash)
  → _complete → transaction: allocate "ELU/JN/FY26/CN/00001"
                             till.record("cash_refund", 549800, …) → save_till
  → sync → create_refund → SYNCED
```

### Step 13: Remake

```
_scenario → app.remakes.remake(sanjay.invoice_no, "BR", "S111", "PROGRESSIVE_NON_ADAPTATION", "eluno")   app/remakes.py:31
  → config.require_reason("remake", …);  require_reason("remake_cost_bearer", "eluno")
  → refunds.find_bill(parent) → parent is SYNCED; lens line "1L" found; not refunded
  → commerce.catalog().lenses_by_variant[old.variant_id] → same lens
  → billing.rx_selection(wearer phone) → newest valid Rx
  → check_lens(...) + anomaly_flags(...) → blocking(...) → none
  → price_line("1L", mrp, hsn, [("REMAKE_GOODWILL", mrp)], …) → tax 0                 domain/pricing.py:46
  → BillLine + Bill(kind="remake", parent_invoice_no, remake_reason, remake_cost_bearer)
  → repo.save_bill;  audit "remake.created"
  → billing.pay(bill_id, "none")
      → DRAFT, total 0 → method "none"; kind "remake" → no _recheck
      → start_payment → no terminal → allocate "ELU/BR/FY26/00003" → mark_paid
      → sync → order_payload: inventory "bypass", no transactions, parent_invoice_no attrs
```
**You see:** frame stock at BR unchanged, and a zero-value invoice with a "Remake of" line.

### Step 14: Close the JN till ₹250 short

```
_scenario → s = app.tills.current("JN") → s.expected_cash_paise
_scenario → app.tills.close("JN", "S120", expected - 25000)                             app/tills.py:62
  → transaction: current() → TillSession.close(counted, …, threshold=20000)              domain/till.py:74
                   variance -25000 → abs > 20000 → variance_flagged = True
                 → save_till → audit "till.closed"
                 → audit "till.variance_alert" (notify finance);  log warning
_scenario → print_till(...)                                                              cli.py:86
```

### Step 15: Day-end reports (HSR, JN, IN)

```
_scenario → app.reports.day_end(store, today)                                           app/reports.py:29
  → directory.store(...)
  → commerce.sales_for_day(store, day, 90)                                               adapters/shopify.py:312
      → _orders(store, day-90, day+1) → sale_record_from_order(o) per order              adapters/shopify.py:137
          (orders without invoice_no are skipped)
  → build_day_end(store_code, day, records)                                              domain/dayend.py:136
      sales on day → bills, gross, discounts, taxable, CGST, SGST, by method, by channel
      refunds on day → amount, tax reversed, credit notes
  → repo.bills(store, day, [paid, synced, voided]);  repo.refunds(store, day)
  → reconcile(rep, shopify records, local bills, local refunds)                          domain/dayend.py:179
  → repo.series_gaps(...) for invoice + CN;  repo.documents(...) for cancelled numbers
  → repo.tills(store, day) → till summary + cash-vs-Shopify check + variance warning
_scenario → print_report(rep)                                                            cli.py:95
```

### Step 16: Channel by store

```
_scenario → app.reports.channels(today)                                                  app/reports.py:18
  → for each billing store: sales_for_day → build_day_end → rep.by_channel
```

### Step 17: Audit trail

```
_scenario → app.repo.audit_log(ref=rahul.bill_id)                                        adapters/sqlite_repo.py:222
  → SELECT … FROM audit WHERE ref = ? → every audit(...) call made for that bill since step 7
```

## 6. End

```
demo.py run()   (finally:)
  → app.close() → repo.close() → SQLite connection closed                               app/__init__.py:62
  → print HTTP calls / retries per service (HttpClient.calls, .retries)
  → leaving `with MockServer(...)` → MockServer.__exit__ → proc.terminate()               devtools.py:52
  → print "Demo complete." → return 0 → run.py sys.exit(0)
```

The temporary database is left in your Temp folder (its path is printed at the start) if you want to open it.

---

## 7. What the mock does with each call

All in [mock_server.py](../starter-kit/mock-apis/mock_server.py). `do_GET` (line 75) and `do_POST` (line 117)
look at the URL path:

| Our call | Mock branch | Effect on its in-memory `S` |
|---|---|---|
| `GET locations.json`, `products.json`, `customers/search.json`, `inventory_levels.json`, `orders.json` | `do_GET` | read only |
| `GET /smt/prescriptions`, `/smt/staff` | `do_GET` `/smt/` | read only |
| `POST /terminal/charges` | `do_POST` line 120 | 503 once per new order_ref, then stores a charge |
| `POST /terminal/refunds` | `do_POST` | adds to the charge's `refunded_paise` |
| `POST orders.json` | `do_POST` line 142 | checks money strings, `taxes_included`, stock; decrements stock; new order `#1013…` |
| `POST orders/{id}/refunds/calculate.json` | line 192 | computes, changes nothing |
| `POST orders/{id}/refunds.json` | line 192 | restocks `return` lines at the location; adds refund; updates `financial_status` |
| any `POST /admin/...` | before the branches | ~10% answer 429 with `Retry-After: 1.0` (`--no-flake` turns this off) |

## 8. CLI instead of demo

```
you: python -m eluno_pos bill --store HSR --staff S105 --phone 9000000005 --pair frame=FR-0003,lens=… --pay upi
eluno_pos/__main__.py → cli.main()                                                     cli.py:394
  → build_parser().parse_args(argv)                                                    cli.py:291
  → logging.basicConfig(...)
  → Settings.from_env(...)   ELUNO_POS_BASE_URL (default 127.0.0.1:8787), ELUNO_POS_DB (var/eluno_pos.db)
  → PosApp(settings)         same as §3, but real urllib transport, real clock, persistent database
  → COMMANDS["bill"](app, a) → cmd_bill                                                cli.py:175
      → bill_request(a) → PairRequest.parse("frame=…,lens=…") → BillRequest            cli.py:130
      → app.billing.create(...) → print_quote, print_bill          (step 4 of §5)
      → app.billing.pay(...)    → print_bill                         (step 4 of §5)
  → errors: BillBlocked / DomainError → message, exit 2 · IntegrationError → exit 3
  → finally: app.close()
```

Other commands follow the same shape: `cmd_refund` → `refunds.refund` (step 10), `cmd_day_end` →
`reports.day_end` (step 15), `cmd_till` → `tills.open/move/close` (steps 9 and 14), `cmd_remake` →
`remakes.remake` (step 13), `cmd_sync` → `PosApp.sync_pending` (step 8), `cmd_invoice` → `render_text`.

The HTTP API is the same again: `api/__init__.py` `Api.dispatch` matches the URL to a handler (for example
`POST /bills` → `create_bill`), and the handler calls the same service method.

## 9. Tests

```
you: python run.py test
run.py main → unittest.defaultTestLoader.discover("tests") → runs every tests/test_*.py
  unit tests       call domain functions directly with data from tests/support.py,
                   which reads starter-kit/data and config/ through the same pure mapping
                   functions (catalog_from, customer_from, prescription_from, bill_from_order …)
  integration      tests/test_integration.py: setUp → MockServer(today=2026-10-22) + PosApp + cutover;
                   each test drives the services exactly as §5 does, then reads the mock to check;
                   tearDown → app.close + mock stopped
```
