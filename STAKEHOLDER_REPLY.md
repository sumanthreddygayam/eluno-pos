# Re: [Retail-Requirement] Replace Shopify POS in stores

**To:** Rohan · **Cc:** Meena, Dr. Iyer, Vikram, Farhan · **From:** Priya (Product-Tech)

All, here is the plan for v1. Below are the decisions I need from you; each has the default we'll ship if I
don't hear back by **Wed 29 Oct**.

## 1. Decisions I need

**Rohan, Meena: offers**
1. **Stacking.** FESTIVE10 is set "not combinable" today; Meena's pitch stacks it with second-pair 50% and
   priority ₹500. If they stack:
   - Does the 50% apply before or after the ₹500?
   - Is there a ceiling per bill? A two-pair priority bill passes ₹6,000 off.
   - *Default: FESTIVE10 alone; the till picks whichever combination saves the customer more.*
2. **Manager's 10% for a walk-out:** on top of the running offer, or instead? "Instead" means a customer
   already on PGX15 gets nothing extra, which defeats the save. *Default: instead.*

**Meena**
3. **Online order picked up in store:** is it already paid on the website, or paid at pickup? If it's paid,
   the till marks it collected instead of billing it again, or sales count twice. *Default: bill
   pay-at-pickup orders only.*

**Dr. Iyer**
4. **Lab limits.** Our data has sphere and cylinder limits separately.
   - Does the lab also reject on **combined power**? For example, −7.50 / −2.00 on a 1.60 is −9.50 in the
     strong meridian. That's the likeliest way a "valid" lens still comes back, and I'll add the check if so.
   - External Rx written at 87°: does the optometrist round it and re-enter it, or do we order it as written?
   - *Default: staff confirm on screen, and we record who did.*
5. **Remakes:** within how many days of delivery, and how many per order? If the remake switches to a cheaper
   lens type (progressive → bifocal), is the difference refunded? *Default: no limit, zero charge, lab-cost owner
   recorded per remake.*

**Vikram**
6. **When the tax invoice is issued.** We issue it at payment, but the lens is made 3–6 days later. Is that
   right, or a receipt now and the tax invoice at delivery? This decides when a number is used, and which GST
   rate applies if a rate changes in between (what forced re-issues last time). *Default: at payment.*
7. **Rounding.** Totals come out in paise (₹23,622.15), and cash can't pay 15 paise. Round with a round-off
   line on every bill, or on cash only? *Default: cash only.*
8. **Where a flat discount sits.** ₹500 split across frame (18% GST) and lens (5%) by price, or all on one
   line? It moves the GST by up to about ₹50 per bill. *Default: by price.*
9. **What day-end must match.** Shopify only, or Razorpay's settlement too (next day)? A UPI payment that
   completes on the customer's phone after our request timed out appears only in settlement. *Default: Shopify,
   plus a next-day settlement check that flags differences.*

**Farhan**
10. **Frame borrowed from HQ or another store for a sale:** is it transferred in Shopify before billing (so it's
    billed and restocked at the selling store), or billed from where it sits? *Default: transfer first.*
11. **Remakes and family bills at the lab:** a remake is a ₹0 lens-only order linked to the old invoice; a
    family bill has two prescriptions on one order. Does the lab need anything else on the job (the original
    job number, the frame coming back)? We'll make sure the OMS picks both up.

## 2. What we're building and what waits

**Building:** all seven Must items, the five Should items, and the billing-time anomaly flags. Also: stock holds
so two tills can't sell the same last frame, and payments that survive Wi-Fi drops.

**Waits:**
- **The 10 am email:** the data exists; it needs recipients. The week after go-live.
- **Split payments:** they complicate refunds; only if stores ask.
- **Return-window rules:** need the policy from Meena and Vikram.
- **Staff sign-in and manager PIN:** come with the store tool's login (section 4); every action is audited until then.
- **Offline billing:** see the Wi-Fi point below.

## 3. Conflicts and risks

**Rohan's 10 seconds.** A bill has two parts: the till's work and the customer paying.

The till's part takes about **2 seconds**:
- checks the frame is still in stock, and reserves it;
- sends the amount to the card machine;
- gives the invoice its number;
- records the order in Shopify, which also takes the frame off Shopify's stock in the same step.

The customer's part depends on how they pay, and no software can shorten it:

| Payment | What the customer does | Customer's part | Till's part | "Yes" to printed invoice |
|---|---|---|---|---|
| UPI | scans the QR, enters PIN | 15–30 s | ~2 s | 17–32 s |
| Card up to ₹5,000 | taps | 3–5 s | ~2 s | 5–7 s |
| Card above ₹5,000 (most of our bills) | taps or inserts, enters PIN | 8–15 s | ~2 s | 10–17 s |
| Cash | hands over cash, gets change | 30–60 s | ~1 s | 30–60 s |

What we promise: the payment screen appears **within 1 second** of pressing Pay, and the invoice prints **within
2 seconds** of the money arriving. Every bill records both times, so when a store says "the system was slow", we
can see whether it was the till or the payment. If UPI shows "pending", the bill simply waits; the customer
can't be charged twice.

**Festive rush, every store busy.** Shopify gives our app **40 requests at once, then 2 more every second**.

Once the customer says yes, a bill uses **3**:
- checks stock when the bill is saved;
- checks stock again just before charging;
- creates the order, which also reduces the frame's stock (no separate stock update).

Before that, while customers choose, every change of frame, lens or coating re-checks stock (1 request). The
till remembers who the customer is and their prescription for the whole visit, so it doesn't ask again.

In the busiest festive hour, across all seven stores, we expect about:
- 150 customers looked up;
- 750 price changes while they choose;
- 50 payments.

That's about **1,050 requests in the hour**, roughly a seventh of what Shopify allows. We tested much harder cases:
- All seven stores pressing Pay in the same second: no slower (about 2.7 s).
- Fourteen at once: about 6 seconds each, because 40 requests run straight away and the rest wait a few
  seconds for Shopify to allow more.
- Twenty-eight at once: about 24 seconds, and a few are told "Shopify busy, try again" *before* any money moves.

A paid bill is never lost or charged twice, and a slow payment at one store never slows another. Reports run
after closing. If the pilot ever shows "busy" messages, Shopify's higher plan allows ten times more: all 28 at
once took under 4 seconds in the same test.

**Wi-Fi drops (Farhan).**
- *During payment:* the bill is saved, and pressing Pay again is safe: no double charge, no double order.
- *After the customer has paid, Shopify unreachable:* the customer still gets the invoice. The order reaches
  Shopify once the connection is back, and the day-end report shows it as pending until then.
- *Store internet down before payment:* no billing, because the till needs Shopify for prices and stock, and the
  card machine needs the internet. The fix is a 4G backup connection per store, Koramangala first. We're not
  building offline billing: without a stock check it could sell a frame that's already gone.

**Manager discount: Meena vs Vikram.** Meena wants no approval chain; Vikram wants an approver and a reason on
every manual discount. Both work: the store manager *is* the approver. One step, their ID and a reason from a
short list; no one else is needed, and auditors see who gave it and why.

**Offers: "yes to all" vs the current setup.** Rohan agreed to Meena's stacking, but FESTIVE10 is set up today
as "not combinable". That needs the decision in Q1 before the festive offers go live.

**Go-live on 15 Nov in all seven stores.** That's our busiest season, and the last day of FESTIVE10, so a problem
would hit the worst possible week. Plan:
- Two pilot stores from **3 Nov**.
- The other five once two stores' day-ends match Shopify for five days running.
- Shopify POS stays installed as a fallback.
- Each store does a stock count and an hour of staff training before switching.

**Not part of this project.** The beverage question in the queue tool goes on the store tool's list; the
Indiranagar footfall camera goes to facilities and the vendor.

## 4. What we need to ship

**From outside:**
- **Razorpay:**
  - a payment-status lookup by our bill reference (without it, a UPI "pending" can't be resolved safely);
  - production keys;
  - the daily settlement file.
- **Shopify:** a custom app with order, refund and inventory write access; confirm our plan's rate limit.

**From the business:**
- **Vikram:** answers to Q6–Q9; opening invoice numbers per store.
- **Meena:** offer rules signed off; two pilot stores; one hour of training per store.
- **Farhan:** a stock count before cutover; receipt printers; 4G backup lines.
- **Dr. Iyer:** answers to Q4–Q5.

**On our side (store tool and OMS):**
- **Store tool:**
  - Pass the booking source with the customer, so WhatsApp-booked bills are tagged automatically.
  - Keep **one** source for priority/campaign tags (Shopify), so the till and the queue always agree.
  - Prescriptions and staff behind an authenticated API; sign-in and a manager PIN for the till.
- **OMS:** accept remake and family orders (Q11).
- **Ops:** on-call cover for the pilot weeks.
