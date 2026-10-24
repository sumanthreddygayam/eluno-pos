# Flows: every case, every branch

This document follows each till flow from start to finish. Each case is a lettered tree: `A → B` is one
step, and a node with several arrows out of it (`C → D`, `C → E`) is a decision. Every branch shows
**what happens** and **why we decided it that way**. Error codes in `backticks` are the `code` the CLI and
API return. Money is in rupees here for readability; the code holds integer paise.

Contents

1. [Cutover: invoice series start](#1-cutover-invoice-series-start)
2. [Customer lookup and prescription choice](#2-customer-lookup-and-prescription-choice)
3. [Building the cart (pairs)](#3-building-the-cart-pairs)
4. [Lens against prescription (clinical checks)](#4-lens-against-prescription-clinical-checks)
5. [Anomaly flags](#5-anomaly-flags-stretch)
6. [Stock and frame checks](#6-stock-and-frame-checks)
7. [Offers](#7-offers)
8. [Manager discount](#8-manager-discount)
9. [GST per line](#9-gst-per-line)
10. [Create the bill (draft)](#10-create-the-bill-draft)
11. [Pay (checkout)](#11-pay-checkout)
12. [Push to Shopify (sync / outbox)](#12-push-to-shopify-sync--outbox)
13. [Cancel and void](#13-cancel-and-void)
14. [Refund (full and partial)](#14-refund-full-and-partial)
15. [Resuming an interrupted refund](#15-resuming-an-interrupted-refund)
16. [Bills from before cutover](#16-bills-from-before-cutover)
17. [Remake](#17-remake)
18. [Family billing](#18-family-billing)
19. [Cash till](#19-cash-till)
20. [Day-end report](#20-day-end-report)
21. [Channel report](#21-channel-report)
22. [Network calls: retry decisions](#22-network-calls-retry-decisions)
23. [Wi-Fi drop: what the till sees](#23-wi-fi-drop-what-the-till-sees)
24. [How errors reach the till](#24-how-errors-reach-the-till)

---

## 1. Cutover: invoice series start

`python -m eluno_pos cutover` (also attempted at API start-up). Code: `app/billing.py` `cutover`, `ensure_series`.

```
A  For each billing store (MM, HSR, BR, KMG, JN, IN, VC; HQ is a warehouse and is skipped)
└─ A→B  For each series: invoice  ELU/HSR/FY26/   and credit note  ELU/HSR/FY26/CN/
    ├─ B→C  Series already in the POS database → nothing to do
    └─ B→D  Series is new → read every order (and its refunds) at that location from Shopify
         ├─ D→E  Shopify answers → floor = highest number already issued (e.g. HSR invoices: 00002)
         │        └─ E→F  Series created; the next invoice is floor + 1 (ELU/HSR/FY26/00003)
         └─ D→G  Shopify unreachable → refuse: `series_not_ready`; nothing is charged anywhere
```

| Path | Decision and why |
|---|---|
| A→B | One series per store **prefix**. The prefix comes from Shopify location data (`invoice_prefix`), so a new financial year is a data change and starts a fresh series. |
| D→E→F | Shopify POS already issued `ELU/HSR/FY26/00001` and `00002`. Restarting at 1 would create duplicate legal numbers, so we continue after the highest. |
| D→G | The floor needs Shopify. If it's down we refuse **before** any payment, never after. (This was a bug the integration tests caught: the first sale after go-live could be charged with no invoice possible.) |
| lazy fallback | If cutover was never run, `pay` does the same check before charging. |

---

## 2. Customer lookup and prescription choice

`lookup --phone …` / `GET /customers/{phone}`. Code: `domain/parties.py` `normalize_phone`, `domain/prescription.py` `select_prescription`.

```
A  Phone typed at the till ("90000 00005", "9000000005", "919000000005", "+919000000005")
├─ A→X  Not a 10-digit Indian mobile → `bad_phone`
└─ A→B  Normalised to +91XXXXXXXXXX
    └─ B→C  Shopify customers/search by phone (exact match only)
        ├─ C→Y  No customer → `customer_not_found` ("register them in the store tool first")
        └─ C→D  Customer found → name, tags → billing flags [PRIORITY] [CAMPAIGN:…] [FAMILY]
            └─ D→E  Store tool: all prescriptions for that phone; sorted newest first by date
                ├─ E→F  Dated after today → skipped ("data-entry error")          e.g. Kavya RX5018
                ├─ E→G  valid_until before today → skipped ("expired on …")     e.g. Priya RX5001
                ├─ E→H  First one with issued ≤ today ≤ valid_until → CHOSEN     e.g. Karthik RX5007
                └─ E→I  None usable → "no valid prescription"; skipped list shows why
```

| Path | Decision and why |
|---|---|
| A→B | Staff type phones in many shapes; normalising avoids a "customer not found" that is really a formatting problem. |
| C→D | Flags shown on the bill are the same cohorts the queue tool uses (Rohan's requirement 3). |
| E→F | The store-tool data contains prescriptions dated in the future (RX5018, RX5023, RX5032…). A test can't happen after today, so these are never used, and the till says why. |
| E→H | "Latest valid" = newest **usable today**, not newest on file. We sort ourselves rather than trusting the source order. |
| E→I | No valid Rx is not an error at lookup; it only blocks **lens** lines later (sunglasses can still be sold). |

---

## 3. Building the cart (pairs)

`quote` / `bill` / `POST /quotes`. Code: `app/billing.py` `quote`, `domain/cart.py`.

```
A  Request: store, staff, customer phone, channel, pairs[]
├─ A→X1 Store unknown → `store_not_found`;  HQ → `not_a_store`
├─ A→X2 Staff not on that store's list → `staff_not_in_store`
├─ A→X3 Channel not walk_in / whatsapp_booked / online_pickup → `bad_channel`
├─ A→X4 No pairs → `cart_empty`
└─ A→B  For each pair (numbered 1, 2, …)
    ├─ B→C  wearer= given → look that customer up too (family billing, §18); else wearer = payer
    ├─ B→D  frame= SKU → catalog frame (unknown → `frame_not_found`)
    ├─ B→E  lens= type/index/coating → lens matrix
    │    ├─ E→X5 Malformed ("progressive/abc/x") → `bad_lens`
    │    ├─ E→X6 Combination not in matrix → `lens_not_found` (lists valid types/indices/coatings)
    │    └─ E→F  Lens found → wearer's prescription (§2, fetched once per wearer) → clinical checks (§4, §5)
    └─ B→G  Pair becomes lines:  frame → "1F",  lens → "1L"  (pair 2 → "2F", "2L")
         ├─ G→H  frame + lens  = spectacles
         ├─ G→I  lens only     = customer's own frame
         └─ G→J  frame only    = sunglasses or plano frame (no prescription needed)
└─ A→K  All pairs built → structural + stock checks (§6) → offers (§7) → GST (§9) → quote
```

| Path | Decision and why |
|---|---|
| B→G | A **pair** is the unit: offers ("second pair", "frame + lens bundle"), Rx and remakes all reason about pairs, so the cart models them directly. `pair_ref` goes to Shopify on every line. |
| G→H/I/J | All three shapes appear in the seed orders (#1006 lens only, #1004 sunglasses only). |
| one unit per line | Quantity is always 1; two identical frames are two pairs. Refunds and remakes then work line by line with no quantity splitting. |

---

## 4. Lens against prescription (clinical checks)

Code: `domain/lens_rules.py` `check_lens`. Limits come from the lens's `rx_limits` in the catalog, never from code.

```
A  Lens chosen for a pair
├─ A→B  No usable prescription → BLOCK `RX_NOT_VALID` (lists skipped Rx and why; "book an eye test")
└─ A→C  Prescription chosen → check each eye (OD right, OS left)
    ├─ C→D  Sphere outside [sph_min, sph_max] or |cylinder| > cyl_abs_max
    │    └─ D→E  BLOCK `LENS_OUT_OF_RX_RANGE`
    │         ├─ E→F  Other indices of the same type + coating fit → hint "Indices that fit: 1.60, 1.67, 1.74"
    │         └─ E→G  None fit → hint "refer to the optometrist"
    ├─ C→H  Lens needs an ADD (progressive / bifocal)
    │    ├─ H→I  ADD missing on either eye → BLOCK `ADD_MISSING` ("choose single vision or record the ADD")
    │    ├─ H→J  ADD outside [add_min, add_max] → BLOCK `ADD_OUT_OF_RANGE`
    │    └─ H→K  ADD present and in range → OK
    ├─ C→L  Single vision but Rx has an ADD → INFO `ADD_NOT_USED` ("confirm distance-only")
    └─ C→M  Everything within limits → no issue
```

| Path | Decision and why |
|---|---|
| D→E | Dr. Iyer's case: a 1.56 lens for a −6.00 customer comes back from the lab. Blocked at the till, with the reason. |
| E→F | A block alone stops the sale; offering the indices that **would** fit lets staff fix it on the spot. |
| limits inclusive | −4.00 on a lens rated −4.00 to +4.00 is allowed (tested). |
| C→L | Info, not a block: some customers do want distance-only glasses. |
| dioptres as integers | −6.00 D is held as −600, so a range check never compares floats. |

---

## 5. Anomaly flags (stretch)

Code: `domain/lens_rules.py` `anomaly_flags`. Severity CONFIRM means "allowed only if staff acknowledge it" (`--ack CODE`, audited).

```
A  Prescription chosen (and lens, when there is one)
├─ A→B  Any axis not a multiple of 5° → CONFIRM `AXIS_OFF_GRID`                    e.g. Suresh RX5999 (87°, 92°)
│    ├─ B→C  Not acknowledged → bill blocked until it is
│    └─ B→D  Acknowledged with --ack AXIS_OFF_GRID → proceeds; audit "issue.acknowledged" with who
├─ A→E  Sphere moved > 1.00 D since the previous prescription → CONFIRM `SPH_JUMP`
├─ A→F  Progressive lens and no earlier Rx had an ADD (or notes say first-time) → INFO `FIRST_PROGRESSIVE`
│    └─ F→G  Salesperson lacks the progressive_expert badge → hint "involve one who does"
└─ A→H  Prescription source is external → INFO `EXTERNAL_RX`
```

| Path | Decision and why |
|---|---|
| A→B CONFIRM | **Block** would stop valid external prescriptions written by other optometrists. **Ignore** would repeat Dr. Iyer's transcription errors. Confirm-with-audit sits between the two. |
| unknown ack | Acknowledging a code that isn't raised on this bill → `bad_acknowledgement` (stops blanket acks). |
| F→G | Uses the staff badge data that already exists in the store tool. |

---

## 6. Stock and frame checks

Code: `domain/cart.py` `structural_issues`, `stock_issues`.

```
A  Cart built
├─ A→B  Pair with neither frame nor lens → BLOCK `PAIR_EMPTY`
├─ A→C  Frame product archived → BLOCK `FRAME_DISCONTINUED` (it appears in history, not at the till)
├─ A→D  Lens on a frame with rx_able = false → BLOCK `FRAME_NOT_RX_ABLE`   e.g. FR-0004 sunglasses
│    └─ D→E  Same sunglasses with no lens → allowed
└─ A→F  Available at THIS store = Shopify's count − frames held by other tills' bills (§6b)
     ├─ F→G  Enough → OK
     ├─ F→K  Shopify has it, but another till is paying for it → BLOCK `STOCK_HELD` ("wait or pick another frame")
     └─ F→H  Not enough → BLOCK `OUT_OF_STOCK`
          ├─ H→I  Other locations have it → hint "Available at MM (5), BR (5), HQ (5)…; transfer it or pick another"
          └─ H→J  Nowhere → hint "pick another frame"
```

**6b. Stock holds: the same frame can't be sold twice.** Code: `adapters/sqlite_repo.py` `hold_stock`, `holds`, `release_stock`; `app/billing.py` `pay`, `sync`.

```
L  Bill starts payment (DRAFT → PAYMENT_PENDING)
└─ L→M  Fresh Shopify count, then ONE locked DB transaction:
         Shopify count − other bills' holds ≥ frames this bill needs?
     ├─ M→N  Yes → hold saved for this bill + status PAYMENT_PENDING (together)
     └─ M→X  No (another till got there first) → `stock_held`; nothing charged, bill stays DRAFT
N  The hold lives while the sale is real but Shopify doesn't know yet
     ├─ N→O  Payment declined → KEPT (customer may pay another way) until the bill is paid or CANCELLED
     ├─ N→P  Payment pending (terminal unreachable) → KEPT; only a store manager can cancel it (after checking the terminal)
     ├─ N→Q  PAID, Shopify down (step 8) → KEPT until sync
     ├─ N→R  SYNCED → released in the same save (Shopify's count now has the sale)
     └─ N→S  VOIDED / CANCELLED → released
```

| Path | Decision and why |
|---|---|
| F | Checked **before** charging, because frames are stocked per store. |
| M | Shopify only deducts stock when the order is created. Between "payment started" and "order in Shopify" (seconds normally, longer in an outage), Shopify still shows the frame as available. The hold covers that gap for every till sharing the POS database. |
| M one transaction | Two tills reading "1 left" at the same instant: the database lets only one hold it (tested with 6 threads). |
| R | Released only when Shopify's own number includes the sale, so there's no moment where the frame counts as available twice or as sold twice. |
| O, P | A failed or unfinished payment never reaches Shopify's orders, and its frame isn't sellable until that bill is paid or cancelled. Day-end lists bills still holding frames so none are forgotten. See `python run.py race`. |
| limit | Holds protect the tills. A channel that reads Shopify directly (the website) only sees the sale once the order lands: instantly normally, after `sync` in an outage. |
| at order create | The order is sent with `decrement_ignoring_policy`: once money is taken and the frame is in the customer's hand, a stale stock count must not make Shopify reject the order. |

---

## 7. Offers

Code: `domain/offers.py`. Offers are data (`config/offers.json`), validated at load; a malformed offer is rejected, never half-applied.

**7a. Is an offer eligible?** (checked in this order; the first failure is the reason shown)

```
A  Offer from offers.json
├─ A→B  active = false → "inactive"                                            MONSOON20
├─ A→C  today outside valid_from..valid_to (inclusive) → "not valid on …"       FESTIVE10 after 15 Nov
├─ A→D  needs a role (manager) and none was requested → "manual discount; only with approval"
├─ A→E  customer_tag not on the PAYER → "customer is not tagged …"             PGX_CAMPAIGN15, PRIORITY500
├─ A→F  min_pairs_in_cart not met (pairs WITH a lens) → "needs 2 pairs"         SECONDPAIR50
├─ A→G  nothing in the cart matches its scope → "no qualifying …"
│     frame scope: frame lines · lens scope: lens_type / coating / index_min · pair: has frame AND lens · cart: all lines
└─ A→H  Eligible → candidate
```

**7b. Which eligible offers go together?**

```
H  Eligible offers
├─ H→I  each non-stackable offer → a candidate ON ITS OWN
├─ H→J  all stackable offers together → ONE candidate (adding a stackable never lowers the saving)
└─ I,J→K  Choose by policy (config/pos_settings.json "offer_policy")
     ├─ K→L  best_for_customer (default) → biggest saving; tie → lower priority number; then fewer offers
     └─ K→M  priority_first → candidate holding the lowest priority number
```

**7c. How a chosen candidate is applied**

```
N  Offers of the chosen candidate, in priority order (lower number first)
├─ N→O  percent → per line, on what is LEFT of the line after earlier offers, rounded half-up
├─ N→P  flat on a line (lens) → min(value, what is left)
├─ N→Q  flat on a pair or the cart → split across those lines pro rata to what is left (exact to the paisa)
├─ N→R  "on: lowest_priced_lens_line" → only the cheapest lens; tie → the later pair
├─ N→S  max_discount_paise (FESTIVE10 cap ₹2,000) → total capped, re-split pro rata
└─ N→T  a line never goes below ₹0
```

**7d. What staff see for every offer:** `+` applied (amount, "capped" if the cap hit) or `-` with a reason:
"inactive", "not valid on…", "not tagged…", or "does not combine with other offers; X saves the customer
more (₹1,800 vs ₹1,424.85)".

| Path | Decision and why |
|---|---|
| H→I/J | `stackable` and `priority` both exist; the data never says what happens when they conflict. Decision: stackable means "combines with other stackables"; non-stackable means "alone". |
| K→L | Picking the best for the customer is defensible on the floor and auditable. It shows up on seed order #1001: the old system gave ₹1,424.85, the engine finds ₹1,800. |
| K→M | One setting away if the business prefers predictability. |
| N→Q pro rata | GST differs per line (18% frame, 5% lens), so where a flat discount lands changes the tax. Pro rata is neutral. The seed put cart/pair discounts on the frame only; that's a question for finance. |
| Meena's combo | FESTIVE10 is non-stackable in the data, so it can't combine with SECONDPAIR50 + PRIORITY500. Flip `stackable` in `offers.json` and it does, with no code change (tested). |

---

## 8. Manager discount

`--discount pct=10,approver=S108,reason=WALKOUT_SAVE`. Code: `domain/offers.py` `_check_manual`, offer `MANAGER_OVERRIDE`.

```
A  Discount requested on a bill
├─ A→B  Approver not on this store's staff list → `staff_not_in_store`
├─ A→C  Approver not a store_manager with can_override_discount → `approver_not_allowed`
├─ A→D  Approver from another store → `approver_wrong_store`
├─ A→E  Reason code missing or not in reason_codes.json → `reason_code_required`
├─ A→F  Above the cap (10%) → `manual_discount_too_high`
└─ A→G  Valid → becomes a NON-STACKABLE candidate (§7b)
     ├─ G→H  Beats the automatic offers → applied; approver + reason on the bill and in Shopify note_attributes
     └─ G→I  Loses (e.g. 5% < the ₹1,800 stack) → not applied; customer gets the better deal
└─ A→J  Either way → audit "discount.manual_override" with approver, reason, requested_by, applied yes/no
```

| Path | Decision and why |
|---|---|
| A→G | Meena: "no approval chain". Vikram: "approver and reason code". Both satisfied: the manager **is** the approver (one step, their id and a code), and there's no chain. |
| G→I | Not recorded on the Shopify order when it lost, so the order doesn't claim a discount it didn't get; still audited. |
| config | An open-ended percent offer without `requires.role` is rejected when the config loads: "no manual discount without an approver" is enforced on the data too. |

---

## 9. GST per line

Code: `domain/tax.py`. Rates: `config/tax_rates.json` (HSN → rate with dates).

```
A  Line: MRP (incl. GST) − its discounts = gross
└─ A→B  Rate for the line's HSN on the bill date
    ├─ B→X  No rate covers that date → quote refused: `tax_rate_missing` ("finance to add it")
    └─ B→C  Rate found (frames 18%, lenses 5% since 2025-09-22, 12% before)
        └─ C→D  taxable = gross × 10000 / (10000 + rate_bps), half-up to the paisa
            └─ D→E  tax = gross − taxable
                └─ E→F  CGST = tax / 2 half-up; SGST = the remainder (odd paisa goes to CGST)
                    └─ F→G  taxable + CGST + SGST = gross exactly; bill totals are re-checked to reconcile
```

| Path | Decision and why |
|---|---|
| B | Rates carry dates because they changed (lens 12% → 5%). A rate change is a data change (tested). |
| B→X | Fail closed: billing an item with a guessed rate would create an invoice finance must re-issue. |
| D–F | Exactly the formula in `validate_seed.py`; every line of the 12 seed orders matches to the paisa (tested). |
| overlap | Two rates covering the same day for one HSN → config refused at load. |

---

## 10. Create the bill (draft)

`bill …` without `--pay`, or `POST /bills`. Code: `app/billing.py` `create`.

```
A  Quote built (§3–§9)
├─ A→B  Any BLOCK, or any CONFIRM not acknowledged → `bill_blocked`, each issue with its fix; nothing saved
└─ A→C  Clean → Bill saved as DRAFT (lines, Rx snapshot, offers + reasons, original request)
     ├─ C→D  audit bill.created
     ├─ C→E  manager discount requested → audit discount.manual_override
     └─ C→F  CONFIRM codes acknowledged → audit issue.acknowledged (who, which code)
```

| Path | Decision and why |
|---|---|
| A→C | The draft is in the database before any money moves, so a Wi-Fi drop or a closed laptop never loses the bill. |
| original request kept | So `pay` can re-price it exactly (§11 B→C). |

---

## 11. Pay (checkout)

`pay BILL --method upi|card|cash`, `bill … --pay`, `POST /bills/{id}/pay`. Code: `app/billing.py` `pay`.

**11a. What state is the bill in?**

```
A  pay(bill, method)
├─ A→S  SYNCED → return it as is (the till retried after a dropped connection)
├─ A→P  PAID (money taken, not yet in Shopify) → go to sync (§12)
├─ A→X  CANCELLED / VOIDED → `bill_state`
├─ A→R  PAYMENT_PENDING (an earlier attempt was interrupted) → resume at 11c
└─ A→B  DRAFT → 11b
```

**11b. Before any money moves**

```
B  Draft
├─ B→B1 Total ₹0 → method "none" (no terminal, no cash)
├─ B→X1 Bad method → `bad_payment_method`
├─ B→C  Re-price from the saved request (stock, Rx, offers, tax as of now)
│    ├─ C→X2 Now blocked (stock gone, Rx expired at midnight) → `bill_blocked`; nothing charged, still DRAFT
│    ├─ C→X3 Price differs from what the customer agreed → `quote_changed`; nothing charged
│    └─ C→D  Same → continue
├─ D→X4 Cash and no open till at this store → `till_not_open`
├─ D→X5 Invoice series not ready and Shopify down (§1 D→G) → `series_not_ready`
├─ D→X6 Another till is already paying for the same last frame → `stock_held` (§6b); nothing charged
└─ D→E  Frames held + status PAYMENT_PENDING saved in one transaction; audit payment.started
```

**11c. Take the money**

```
E  PAYMENT_PENDING
├─ E→F  UPI / card and no charge id yet → terminal charge(amount, order_ref = bill_id)
│    ├─ F→F1 503 → retried with the SAME order_ref (idempotent by contract)
│    ├─ F→X6 Declined (4xx) → back to DRAFT; `payment_declined`; nothing charged
│    ├─ F→X7 Unreachable → stays PAYMENT_PENDING; `terminal_unreachable` ("retry, same reference, no double charge")
│    └─ F→G  Captured → charge id SAVED IMMEDIATELY
├─ E→G  UPI / card and a charge id already saved (resume) → never charge again
└─ E→G  Cash or ₹0 → nothing to charge here
```

**11d. Issue the invoice**

```
G  Money secured
└─ G→H  ONE database transaction:
     ├─ H→H1 next number in the store's series (ELU/HSR/FY26/00003)
     ├─ H→H2 bill → PAID (invoice no, paid time, charge id)
     ├─ H→H3 cash → till movement "cash_sale" (if the till closed meanwhile → whole transaction undone, retry)
     └─ H→I  commit → audit bill.paid → push to Shopify (§12) → audit bill.timing (seconds)
```

| Path | Decision and why |
|---|---|
| C | The customer said yes to a number. If anything changed since, we don't charge silently. |
| X6 vs X7 | Declined is definite (nothing charged, back to draft). Unreachable is uncertain, so the bill stays pending and the retry reuses the same `order_ref`. |
| F→G | The charge id is written the moment it is known. The mock terminal creates a second charge if POSTed again after success, so "never re-charge a bill that has a charge id" is the guard. |
| H | Invoice number **after** payment: a failed payment never burns a number (Finance wants no gaps). **Before** Shopify: a Shopify outage can't stop the customer getting a valid invoice. |
| H1–H3 in one transaction | Number, status and cash drawer can't disagree after a crash. |
| timing | Measured on every bill for the 10-second requirement: 0.1–2 s with the mock's 429s, ~2 s worst case when Shopify is down. |

---

## 12. Push to Shopify (sync / outbox)

Runs right after payment; again via `sync`, at API start-up, or when `pay` is repeated. Code: `app/billing.py` `sync`, `adapters/shopify.py` `create_order`.

```
A  Bill is PAID
└─ A→B  sync_attempts + 1 is SAVED before calling Shopify (write-ahead)
    ├─ B→C  This is not the first attempt → FIRST look for an order with this invoice number
    │    ├─ C→D  Found → adopt it (no new order) → SYNCED
    │    └─ C→E  Not found → create
    └─ B→E  First attempt → create order (invoice_no in note_attributes; Rx on the lens line; tax lines; transaction)
         ├─ E→F  201 → line ids, order id, sale transaction id → SYNCED
         ├─ E→G  429 → wait Retry-After, retry (Shopify did nothing)
         ├─ E→H  5xx or response lost (it MAY have been created) → look the invoice number up
         │    ├─ H→D  Found → adopt → SYNCED  (no duplicate)
         │    ├─ H→E  Not found → create again (up to 4 tries)
         │    └─ H→J  The lookup itself fails (Shopify down) → stays PAID, last_error kept
         ├─ E→K  4xx (Shopify rejects the data) → stays PAID with the error → fix the data, or void (§13)
         └─ E→J  Shopify down → stays PAID; day-end shows "payment captured but not in Shopify yet"
                  └─ J→A  Later `sync` → back to A (now "not the first attempt", so it looks first)
```

| Path | Decision and why |
|---|---|
| idempotency key | Shopify order create is **not** idempotent. Our key is the invoice number: allocated locally before the first attempt and unique by construction. |
| B write-ahead | If the process dies mid-call, the counter says "an attempt may have landed", so the next run looks before creating. Tested by dropping the response *and* failing the lookup. |
| H | Only an **ambiguous** failure triggers a lookup; the normal path is one call, which keeps checkout fast. |
| J | The customer leaves with a valid invoice; Shopify catches up. Finance sees the gap in day-end until it does. |

---

## 13. Cancel and void

Code: `app/billing.py` `cancel`, `void`.

```
A  Bill should not go ahead
├─ A→B  DRAFT (nothing charged, no number) → cancel → CANCELLED
├─ A→C  PAYMENT_PENDING (terminal stuck), no charge recorded → cancel ONLY with a store manager who checked the
│        terminal shows no payment (`approver_required` otherwise) → CANCELLED, frame released
│        with a charge recorded → never cancellable: finish with `pay` (then refund or void)
├─ A→D  PAID but cannot reach Shopify (e.g. Shopify keeps rejecting it) → void
│    ├─ D→X1 Reason code not in reason_codes.json "void" → `bad_reason_code`
│    ├─ D→X2 Approver not a store manager → `wrong_role`
│    └─ D→E  Money back: terminal refund (UPI/card) or cash out of the till
│         └─ E→F  VOIDED; invoice number marked CANCELLED in the register (not reused, not deleted)
└─ A→G  SYNCED → not voidable → use a refund (§14), which issues a credit note
```

| Path | Decision and why |
|---|---|
| D→F | Vikram: "a cancelled invoice stays in the series as cancelled". The next bill takes the next number; the series has no gaps (tested). |
| A→G | Once a sale is in Shopify, it's reversed by a credit note, never by deleting history. |

---

## 14. Refund (full and partial)

`refund INVOICE --store --staff --reason [--lines 1F,2L]`, `POST /refunds`. Code: `app/refunds.py`, `domain/refund.py`.

**14a. Checks before anything is recorded**

```
A  Refund request
├─ A→X1 Reason code not in reason_codes.json "refund" → `bad_reason_code`
├─ A→B  Find the bill: POS database → else Shopify (pre-cutover bill, §16) → else `invoice_not_found`
├─ B→X2 Billed at another store → `wrong_store` ("refund it there: the frame restocks there, that till took the money")
├─ B→X3 Zero-value bill (remake) → `zero_refund`
├─ B→X4 Bill not yet in Shopify (PAID) → `bill_not_synced` ("run sync first")
└─ B→C  Which lines?
     ├─ C→D  --lines given → exactly those
     │    ├─ D→X5 Unknown line ref → `unknown_line`
     │    └─ D→X6 Already refunded (or a refund in flight) → `already_refunded`
     └─ C→E  none given → every line not yet refunded (= full refund)
          └─ E→X7 Nothing left → `already_refunded`
```

**14b. Can the money go back?**

```
F  Lines chosen; amounts = exactly what was charged per line (tax as charged, not today's rate)
├─ F→G  Paid in cash
│    ├─ G→X8 No open till → `till_not_open`
│    └─ G→X9 Drawer holds less than the refund → `drawer_negative` (record a paid-in first)
├─ F→H  Paid by UPI/card without a terminal charge on record → `no_charge`
├─ F→I  Credit-note series not ready and Shopify down → `series_not_ready`
└─ F→J  Ask Shopify (refunds/calculate) what is refundable
     ├─ J→X10 Shopify rejects → `refund_rejected`
     ├─ J→X11 Shopify's maximum < this refund → `over_refund`
     └─ J→K  OK (a tax difference between Shopify and us is noted in the audit)
```

**14c. Moving money, credit note, Shopify**

```
K  Refund saved as REQUESTED; audit refund.requested
├─ K→L  UPI / card → terminal refund
│    ├─ L→X12 Terminal fails → FAILED; `refund_failed`; no credit note
│    └─ L→M  Done → terminal refund id SAVED IMMEDIATELY
├─ K→M  Cash → nothing moved yet
└─ M→N  ONE database transaction: next credit-note number (ELU/HSR/FY26/CN/00003) + cash-out movement (cash)
     ├─ N→X13 Fails for cash → FAILED (no cash paid out); for UPI/card → stays REQUESTED (resumable, §15)
     └─ N→O  MONEY_RETURNED → Shopify refund:
          frame lines: restock_type "return" at the BILLING store · lens lines: "no_restock"
          transaction "refund" against the original sale transaction · credit_note_no in note_attributes
          ├─ O→P  201 → SYNCED (order becomes refunded / partially_refunded)
          ├─ O→Q  Response lost / 5xx → look up by credit-note number → adopt if found, else retry
          └─ O→R  Shopify down → stays MONEY_RETURNED; `sync` finishes it; day-end flags it meanwhile
```

| Path | Decision and why |
|---|---|
| F | A credit note must reverse **the tax that was charged**. If the rate changed between sale and refund, recomputing would produce a wrong credit note (tested with the 12% → 5% change). |
| restock | Farhan: the frame goes back to the store that billed it; lenses are made to order and scrapped. Shopify itself refuses to restock a lens, so the mapping never asks. |
| 14b before K | Everything that can refuse does so **before** a record exists. (Integration tests caught a refused cash refund that left a stuck record blocking every retry; fixed by this ordering.) |
| L→M | Saved immediately, so an interruption after the customer has the money can always be finished (§15). |
| partial | `--lines 2F` refunds only the second frame. The line's net paid amount is refunded; reclaiming a bundle discount the customer no longer qualifies for is a finance question (see DECISIONS.md). |

---

## 15. Resuming an interrupted refund

`sync` runs this for every unfinished refund. Code: `app/refunds.py` `resume`; `adapters/terminal.py` `refund`.

```
A  Unfinished refund
├─ A→B  MONEY_RETURNED → push to Shopify (§14 O)
├─ A→C  REQUESTED with a terminal refund id → the customer has the money → issue the credit note + Shopify
├─ A→D  REQUESTED, cash → no cash-out was recorded → FAILED ("start the refund again")
└─ A→E  REQUESTED, UPI/card, no terminal id → state unknown → read the charge's refunded amount; flag for a person
```

Inside the terminal adapter, when a refund call's response is lost:

```
F  Refund response lost
├─ F→G  Charge's refunded amount went up by exactly this refund → it happened → treat as done
├─ F→H  Unchanged → it did not happen → try once more
└─ F→I  Changed by something else → stop and raise; a person checks
```

| Path | Decision and why |
|---|---|
| A→D | For cash, the system only records money leaving the drawer inside the same transaction as the credit note; if that never committed, the cash wasn't handed over on the system's word. |
| F | The terminal's refund call has no idempotency key, so we read the result back instead of guessing. |

---

## 16. Bills from before cutover

Any command given an invoice the POS didn't issue (refund, reprint, remake). Code: `app/refunds.py` `find_bill`, `adapters/shopify.py` `bill_from_order`.

```
A  Invoice number not in the POS database
└─ A→B  Search Shopify orders at that store for note_attributes invoice_no
    ├─ B→X  Not found → `invoice_not_found`
    └─ B→C  Found → convert to a bill (lines, tax, discounts, Rx, payment) → saved locally (source "imported"); audit bill.imported
         ├─ C→D  Refund → normal refund flow (§14); e.g. ELU/JN/FY26/00002, cash, refunded through the JN till
         ├─ C→E  Reprint → invoice text/HTML (reproduces sample_invoice.txt byte for byte for #1001)
         └─ C→F  Remake → §17
```

| Path | Decision and why |
|---|---|
| C | On day one, every return is of a bill made by Shopify POS. Reading it from Shopify on demand avoids a migration, and the conversion is tested on all 12 seed orders. |

---

## 17. Remake

`remake PARENT_INVOICE --reason … --cost-bearer … [--pair 1] [--lens …]`. Code: `app/remakes.py`.

```
A  Remake request
├─ A→X1 Reason not in reason_codes.json "remake" (non-adaptation, external Rx error, in-store Rx error, lab defect) → `bad_reason_code`
├─ A→X2 Cost bearer not eluno / customer / external_optometrist → `bad_reason_code`
├─ A→B  Find the parent bill (POS or Shopify, §16)
│    ├─ B→X3 Parent not in Shopify yet → `bill_not_synced`
│    ├─ B→X4 No lens in that pair → `no_lens_in_pair`
│    └─ B→X5 That lens was refunded → `already_refunded`
├─ B→C  Lens: --lens given → that one; else the same lens (gone from catalog → `lens_not_found`)
├─ C→D  Wearer's NEWEST valid prescription (a re-test may have replaced the wrong one)
│    ├─ D→X6 Clinical block (§4/§5) → `bill_blocked`
│    └─ D→E  OK
└─ E→F  New bill, kind "remake": ONE lens line, 100% off (REMAKE_GOODWILL), tax ₹0
     └─ F→G  pay with method "none": invoice number issued (zero-value), no terminal, no till
          └─ G→H  Shopify order: lens line only, no transaction, inventory "bypass",
                   note_attributes parent_invoice_no + remake_reason + remake_cost_bearer
```

| Path | Decision and why |
|---|---|
| F | Dr. Iyer and Farhan: the frame is not billed again and frame stock is not touched (double reservations locked stock for days). Lens-only order with `bypass`. |
| G | It still takes an invoice number, so the series stays complete; the seed's own remake (#1009) did the same. |
| cost bearer | "Decided case by case" today, so it is captured on every remake for finance to allocate the lab cost. If the customer pays, it's a normal lens-only sale instead. |

---

## 18. Family billing

`--pair …,wearer=PHONE`. Code: `app/billing.py` `quote`, `adapters/shopify.py` `order_payload`.

```
A  One bill, several pairs
├─ A→B  Pair without wearer → the wearer is the payer
└─ A→C  Pair with wearer=phone → that customer is looked up (not found → `customer_not_found`)
     └─ C→D  Prescription fetched for THAT wearer (once per wearer)
          └─ D→E  Clinical checks against the wearer's own Rx
└─ A→F  Shopify order: customer = the payer; each lens line carries rx_id, Rx JSON,
         for_customer_id, wearer name; invoice line shows "(for Shreya Joshi)"
└─ A→G  Offers: customer-tag conditions use the PAYER's tags; two pairs unlock SECONDPAIR50
```

| Path | Decision and why |
|---|---|
| A→F | One Shopify order can only have one customer, and Meena wants one bill. The wearer goes on the line, which keeps Shopify valid and the Rx traceable. |
| revisit | If marketing needs per-wearer purchase history in Shopify, split into one order per wearer. |

---

## 19. Cash till

`till open|paid-in|paid-out|close|status`, `POST /tills/{store}/…`. Code: `app/tills.py`, `domain/till.py`.

```
A  Open
├─ A→X1 Store already has an open till → `till_already_open` (one per store, enforced by the database)
├─ A→X2 Negative float → `bad_amount`
└─ A→B  Session TILL-HSR-2026-10-22 (second the same day: …-2), float recorded

B  During the day
├─ B→C  Cash sale → recorded automatically by pay (§11 H3)
├─ B→D  Cash refund → recorded automatically by refund (§14 N)
├─ B→E  Paid-out (courier, tea) / paid-in (change from HQ)
│    ├─ E→X3 No reason code → `reason_code_required`; code OTHER without a note → `note_required`
│    ├─ E→X4 Paid-out more than the drawer should hold → `drawer_negative`
│    └─ E→F  Recorded with its own reference PO-HSR-00001 / PI-HSR-00001; audited
└─ B→G  Cash payment with no open till → refused at pay (`till_not_open`)

H  Close with the counted amount
└─ H→I  expected = float + sales − refunds + paid-ins − paid-outs;  variance = counted − expected
     ├─ I→J  |variance| ≤ ₹200 → closed
     └─ I→K  |variance| > ₹200 → closed and FLAGGED → audit till.variance_alert (notify finance) + warning log
```

| Path | Decision and why |
|---|---|
| E | Meena: cash taken for courier and tea "has to be recorded somewhere". Every movement has a reason code and a reference. |
| I→K | Vikram: variance over ₹200 flagged the same night. The threshold is a setting. Sending the alert (email/WhatsApp) is not built; it is recorded. |
| seed check | The JN seed session replays to expected ₹18,296 and variance −₹250 (flagged); VC to 0 (tested). |

---

## 20. Day-end report

`day-end --store HSR [--date] [--json]`, `GET /reports/day-end`. Code: `domain/dayend.py`, `app/reports.py`.

```
A  Store + date (IST)
└─ A→B  Shopify orders at that location, created from (date − 90 days) to date
    └─ B→C  Keep only POS orders (they carry an invoice_no)
        ├─ C→D  Order created ON the date → counts as a sale
        │    └─ bills, zero-value bills, gross (MRP), discounts (and by offer), net, taxable, CGST, SGST,
        │       by payment method, by channel, average bill (zero-value excluded)
        └─ C→E  Refund made ON the date (whatever the order date) → counts as a refund
             └─ amount, taxable, tax reversed, credit notes; reduces that payment method's net
└─ A→F  Reconciliation findings (Shopify vs the POS's own records)
     ├─ F→F1 Sale captured ≠ invoice total → ERROR
     ├─ F→F2 Refund in Shopify without a credit note → ERROR
     ├─ F→F3 POS bill PAID but not in Shopify → ERROR ("run sync")
     ├─ F→F4 POS says synced but Shopify doesn't have it / totals differ → ERROR
     ├─ F→F5 Refund money returned but not in Shopify → ERROR
     ├─ F→F6 Invoice or credit-note series has a gap → ERROR
     ├─ F→F7 Voided bill / cancelled number → INFO
     ├─ F→F8 Invoice in Shopify not issued by this POS (pre-cutover, another till) → INFO
     ├─ F→F9 Shopify cash net ≠ till cash sales − cash refunds → ERROR
     ├─ F→F10 Till variance over threshold → WARNING
     ├─ F→F11 Cash bills but no till session that day → WARNING
     └─ F→G  None → "Reconciliation: clean"
```

| Path | Decision and why |
|---|---|
| B | Vikram: day-end must match Shopify to the rupee, so Shopify is the source of the numbers. |
| F | The till also knows what happened; it is used to **check** Shopify, and every disagreement is listed instead of being silently fixed in the numbers. |
| C→E | A refund today of last week's bill reduces today's takings, because that's what the drawer and the bank see. |
| 90 days | How far back an order can be and still be refunded today; a setting. |

---

## 21. Channel report

`channels [--date]`, `GET /reports/channels`. Code: `app/reports.py` `channels`.

```
A  Date
└─ A→B  For each billing store → that day's POS orders grouped by channel (walk_in / whatsapp_booked / online_pickup)
     └─ B→C  bills and net per channel, per store
```

Meena: "where each bill came from… by store, daily". The channel is required on every bill and travels in
the order's `note_attributes` and tags.

---

## 22. Network calls: retry decisions

Code: `adapters/http.py`. Policy: 5 attempts, jittered backoff from 0.15 s up to 2 s.

```
A  Response or failure
├─ A→B  2xx → done (if Shopify's call-limit bucket is nearly full, pause 0.5 s)
├─ A→C  429 → wait Retry-After (capped at 10 s) → retry, ANY call (Shopify rejected it before doing anything)
├─ A→D  Connection refused → retry, any call (nothing left this machine)
├─ A→E  5xx or no response
│    ├─ E→F  Safe to repeat (GET, refunds/calculate, terminal charge with same order_ref) → retry with backoff
│    └─ E→G  Not safe (Shopify order/refund create, terminal refund) → AmbiguousWrite → caller looks before retrying
├─ A→H  4xx → no retry; Shopify's message is shown
└─ A→I  Attempts used up → the caller decides (checkout: bill stays PAID; refund: stays MONEY_RETURNED)
```

| Path | Decision and why |
|---|---|
| E→G | "Creating the same order twice after a timeout must not create two orders": the client never blindly retries a write whose outcome is unknown. |
| policy size | Sized for the 10-second checkout: a dead Shopify falls through to the outbox in about 2 s. |
| 127.0.0.1 | `localhost` on Windows tries IPv6 first and the mock is IPv4-only: +2 s per call (8 s bills). The default URL avoids it. |

---

## 23. Wi-Fi drop: what the till sees

Farhan: "the bill shouldn't vanish, and it definitely shouldn't get created twice". Each moment a connection can drop:

```
A  Connection drops…
├─ A→B  …while building the cart → nothing saved yet; build again
├─ A→C  …after the draft is saved → the draft is in the database; pay it when back
├─ A→D  …during the terminal charge → bill PAYMENT_PENDING; pay again → same order_ref, no double charge
├─ A→E  …after the charge, before the invoice → charge id saved; pay again → skips charging, issues the number
├─ A→F  …during the Shopify create → bill PAID; pay or sync again → looks up the invoice number, adopts or creates once
└─ A→G  …after everything → pay again returns the SYNCED bill unchanged
```

Every one of these is covered by an integration test or the demo (lost response, outage, repeated `pay`).

---

## 24. How errors reach the till

Code: `domain/errors.py`, `cli.py`, `api/__init__.py`.

```
A  Something refuses
├─ A→B  BillBlocked (clinical, stock, Rx…) → CLI exit 2 / HTTP 422; list of issues, each with severity, pair, message, hint
├─ A→C  Bad input (phone, lens text, missing field) → CLI exit 2 / HTTP 400
├─ A→D  Not found (customer, invoice, store) → CLI exit 2 / HTTP 404
├─ A→E  Not allowed (approver role, wrong store) → CLI exit 2 / HTTP 403
├─ A→F  Wrong state (already refunded, till closed, quote changed) → CLI exit 2 / HTTP 409
├─ A→G  Payment failed (declined, terminal unreachable) → CLI exit 2 / HTTP 402
└─ A→H  Upstream down after retries → CLI exit 3 / HTTP 502, with "retryable"
```

Every message says what went wrong **and what to do next** ("pick 1.60, 1.67 or 1.74", "open the till
first", "run sync"), because the reader is a salesperson with a customer waiting.
