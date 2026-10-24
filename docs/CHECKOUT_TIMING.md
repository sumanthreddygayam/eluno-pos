# Checkout timing, more tills, and Wi-Fi

Rohan's rule: *a bill should be done in under 10 seconds from the moment the customer says yes.* This page
shows where those seconds go, what happens when many tills pay at once, and what a Wi-Fi drop costs.
Reproduce every number with `python run.py bench`.

**Assumptions.** The local mock answers in about 1 ms, so the bench adds a delay to every call, as seen from
a store in Bengaluru: **Shopify 0.4 s, store tool 0.15 s, terminal 0.8 s** (change them with
`--shopify-ms`, `--smt-ms` and `--terminal-ms`). It also enforces Shopify's REST rate limit the way Shopify
describes it: a bucket of **40 requests per shop that drains at 2 per second** on standard plans (Shopify Plus:
400, at 20 per second). The real numbers come from the pilot: every bill already records its own checkout
time in the audit trail (`bill.timing`).

## 1. One bill, step by step

From "customer says yes" (the quote is on screen) to "invoice ready". The customer and their prescription were
read when they were looked up at the start of the visit and are remembered for it (`visit_cache_seconds`, 15
minutes). Stock is never remembered.

| Step | Network calls | Time |
|---|---|---|
| A. Save the bill: re-price (stock is the only thing re-read) and save the draft | 1 Shopify | 0.4 s |
| B. Re-check price and stock just before charging; reserve the frame | 1 Shopify | 0.4 s |
| C. Terminal: send the charge, get the answer | 1 terminal | 0.8 s |
| D. Invoice number + mark paid (one local database transaction) | 0 | 0.02 s |
| E. Create the order in Shopify; this same request takes the frame off Shopify's stock | 1 Shopify | 0.4 s |
| F. Release the reservation, audit, return | 0 | < 0.01 s |
| **Total system time** | **4 (3 Shopify)** | **2.1 s** |
| *Plus:* rendering the invoice for the printer | – | < 0.1 s |

Before remembering the customer and prescription for the visit, it was 8 calls (5 Shopify) and 3.2 s.

Plus the customer paying, which depends on the method (estimates, to be measured in the pilot):

| Method | What the customer does | Customer time | Our system time | Yes → invoice |
|---|---|---|---|---|
| UPI (dynamic QR on the terminal) | scan, open the app, enter PIN, bank confirms | 15–30 s; sometimes "pending" for 1–2 min | ~2 s | 17–32 s |
| Card ≤ ₹5,000 | tap, no PIN (the contactless limit) | 3–5 s | ~2 s | 5–7 s |
| Card > ₹5,000 (most bills; average ₹5,400) | tap or insert, then PIN | 8–15 s | ~2 s | 10–17 s |
| Cash | count, give change | 30–60 s | ~1 s (no terminal call) | 30–60 s |

Notes:
* The checks Finance asked for (GST per line, invoice series, reason codes, the audit trail) are local and
  together take **milliseconds**. They don't threaten the 10 seconds.
* Stock is read twice on purpose: when the bill is saved, and again just before charging, because another till
  may have sold the frame in between. The second read is reused to reserve the frame (no third read).
* The customer's payment time is outside our control. We commit to the system part (the payment prompt on
  screen within 1 s of Pay, the invoice within 2 s of the money confirming) and log system time and payment time
  separately per bill.
* A UPI payment that sits at "pending" keeps the bill waiting and the frame reserved; it can't be charged twice.
  Resolving it safely needs Razorpay's payment-status lookup by our bill reference.

## 2. With the mock's real failures switched on

The mock returns 429 ("too busy") on about 10% of Shopify writes, and 503 once on about 30% of new terminal
charges. 20 bills in a row:

| Median | p95 | Worst | Bills that needed a retry |
|---|---|---|---|
| 3.2 s | 4.6 s | 5.1 s | 10 of 20 (each 429 waits about 1 s, as Shopify asks) |

## 3. More tills paying at the same moment

Every till presses Pay at the same instant, the worst case. All tills share one Shopify bucket. In this test each
bill also looks its customer up once (4 Shopify requests per bill), because the tills start cold.

| Situation | Bills | Median | p95 | Worst | Throttled (429) | Outcome |
|---|---|---|---|---|---|---|
| One till, 3 bills | 3 | 2.7 s | 2.7 s | 2.7 s | 0 | all fine |
| **One till per store (7) in the same second** | 7 | **2.7 s** | 2.8 s | 2.8 s | 0 | all fine |
| Two per store (14) in the same second | 14 | 5.8 s | 8.4 s | 9.3 s | 14 | all fine |
| Four per store (28) in the same second | 28 | 23.7 s | 34.1 s | 34.8 s | 81 | 3 refused *before* charging ("busy, try again") |
| Four per store (28), **Shopify Plus** limit | 28 | 3.8 s | 4.8 s | 4.8 s | 0 | all fine |
| 7 tills billing back to back (4 bills each) | 28 | 5.9 s | 17.7 s | 19.5 s | 68 | 3 slowed past retries; paid ones keep their invoice and sync later |

The bucket in plain terms: Shopify lets our app make **40 requests at once, then 2 more every second**. A bill after
"yes" needs 3. Seven bills together need 21 and fit; fourteen need 42 plus their lookups, so a few wait seconds
for Shopify to allow more; twenty-eight need about 110, so some wait long enough to be told "try again".

What this means for Eluno:
* **Shopify's rate limit is the only real bottleneck**, not our code or our database. At 3 requests per bill the
  shop sustains about **40 bills a minute** across all stores.
* Paying isn't the main load. **Price checks while the customer chooses** are: every change of frame, lens or
  coating re-checks stock (1 Shopify request). The busiest hour of a festive day, estimated from the brief's
  350–400 walk-ins and ~120 bills a day, ×2.5 for the festive day and 15% of the day in its busiest hour:

  | Busiest hour, all stores | Count | Shopify requests |
  |---|---|---|
  | Walk-ins looked up | ~150 | ~150 |
  | Price changes while choosing (~5 per walk-in) | ~750 | ~750 |
  | Bills paid (3 requests each) | ~50 | ~150 |
  | **Total** | | **~1,050 ≈ 0.3 per second: about a seventh of the standard limit** |

  What hurts is a **burst**: more than about 13 bills pressing Pay within the same few seconds. The bucket of 40
  absorbs anything smaller.
* Even under overload, **nothing is lost or doubled**. Reads that run out of retries are refused *before* the
  customer is charged ("try again"). A sale that has been charged keeps its invoice and syncs to Shopify later.
* If the pilot shows bursts, the next levers are a short queue that spreads Shopify requests, or Shopify Plus
  (10x the limit: 28 simultaneous bills at under 4 s).
* Other reads also use the bucket. The day-end for 7 stores reads about 90 days of orders per store, a few
  dozen calls in all, so it should run after closing, not during the evening rush.
* **Two tills, one last frame:** the second till is told *at once* that the frame is reserved by another bill.
  It doesn't wait or retry (`python run.py race`).
* **One server, many stores:** the HTTP API serves requests in parallel. A customer taking 15 seconds to approve
  UPI at HSR doesn't delay Jayanagar (tested); a double-clicked Pay on one bill still charges once (tested).

## 4. Wi-Fi and Shopify trouble during a bill

| What goes wrong | Pay takes | Result |
|---|---|---|
| Wi-Fi blip: Shopify created the order but the answer was lost | 2.1 s | the order is found by invoice number and adopted; **no duplicate** |
| Shopify unreachable right after payment | 2.7 s | **invoice issued**, bill marked PAID; the order goes to Shopify on the next sync; day-end flags it until then |
| Terminal slow (3 s) | 4.7 s | completes normally |
| Terminal unreachable | 1.8 s | bill waits (PAYMENT_PENDING), frame reserved; Pay again is safe (same reference, never charged twice); only a manager can cancel it |
| **Store internet fully down before Pay** | – | **can't bill**: pricing needs Shopify (stock) and payment needs the terminal's network |

The last row is a store-network question, not a software one. A 4G backup router per store (Koramangala first)
keeps the till, Shopify and the terminal online when the broadband drops. A true offline mode (cash only,
invoices queued) is possible later, but it risks selling stock Shopify has already sold elsewhere, so we'd
only build it if the backup link isn't enough.
