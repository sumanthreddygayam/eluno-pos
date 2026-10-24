# Decisions

Each entry: what was decided, why, and what would make us revisit it. The first block answers the
questions `starter-kit/schema.md` leaves open.

## The questions the seed leaves open

**1. Order vs invoice vs credit note.** The invoice number is allocated by the POS, per store prefix
(`ELU/HSR/FY26/00007`), gapless, in a SQLite transaction *after the payment is captured and before
Shopify is called*. It goes into `note_attributes.invoice_no`; Shopify's `order_number` stays the global
series. A failed or declined payment never burns a number. A captured payment always gets one, even if
Shopify is down; the order is pushed later. If an order can never reach Shopify, a manager **voids** the
bill: money is returned and the number stays in the register as *cancelled*. Credit notes are a separate
gapless series per store (`ELU/HSR/FY26/CN/00003`), allocated when the refund money moves. At cutover,
each series continues after the highest number Shopify POS already issued (`cutover` command).
*Revisit:* if finance wants numbers allocated at quote time, or one series across invoice and credit note.

**2. What is a pair.** A cart is a list of pairs; a pair is a frame and/or a lens for one wearer. Lines are
`1F`, `1L`, `2F`... and carry `pair_ref` in Shopify. Frame only = sunglasses or a plano frame; lens only =
customer's own frame. A remake is a lens-only order with `parent_invoice_no`.

**3. Family billing.** One bill, one Shopify customer (the payer). Each pair has its own wearer and
prescription; the lens line carries `rx_id`, the Rx JSON, `for_customer_id` and the wearer's name.
Prescriptions are fetched per wearer. Customer-tag offer conditions use the payer's tags.
*Revisit:* if marketing needs per-wearer purchase history in Shopify (then split orders, one invoice each).

**4. Idempotency key.** Terminal: `order_ref = bill_id` (the terminal is idempotent on it). Shopify order:
the invoice number, allocated before the first attempt and stored locally, so a retry after a timeout first
looks the number up and adopts the order it finds. Shopify refund: the credit-note number, same way. The
bill records `sync_attempts` *before* each call, so after a crash the next attempt always looks first.

**5. Offer evaluation.** Non-stackable offers stand alone; stackable offers combine. Candidates are each
non-stackable offer alone and all stackable offers together. Policy `best_for_customer` (default, in
`config/pos_settings.json`) picks the biggest saving; ties go to the lower `priority` number. Within a stack
offers apply in priority order on what is left of each line. Every offer, applied or not, gets a reason the
salesperson can read out ("does not combine with other offers; X saves the customer more"). On the seed
data this picks Rs 1,800 for order #1001, where the old system applied Rs 1,424.85.
*Revisit:* `priority_first` is one setting away if the business prefers predictability over maximum saving.

**6. Expired prescription.** The till uses the newest prescription with `issued_on <= today <= valid_until`.
Expired ones are skipped, and so are prescriptions dated in the future (they exist in the store-tool data and
can only be entry errors). No valid Rx blocks lens lines with the reason and next step; frames and
sunglasses can still be sold.

**7. Day-end source of truth.** Shopify. The report is computed from Shopify orders and refunds; the POS
ledger and till are used to *check* it, and every mismatch is listed (paid but not in Shopify, totals
differ, credit note not synced, cash in Shopify vs till, series gaps, variance over Rs 200). Sales count on
the invoice date, refunds on the refund date.

## Money, tax and discounts

* **Integer paise** everywhere; `Decimal`-free integer rounding (`div_round_half_up`); decimal strings only
  in `adapters/shopify.py`. The back-out formula matches `validate_seed.py` and every seed line to the paisa.
* **Rates by date** from `config/tax_rates.json` (the lens rate change of 2025-09-22 is honoured). A missing
  rate blocks billing that item; overlapping rates fail at load.
* **Refunds reverse the tax charged**, not the tax at today's rate.
* **Flat cart and pair discounts are split pro rata** across lines (largest remainder, exact). GST is per
  line at different rates, so the split changes the tax. The seed orders #1005 and #1010 put cart and pair
  discounts on the frame line only, which lowers the 18% line and therefore the tax. *Question for finance.*
* **Manual discounts** need a store manager of the same store with `can_override_discount`, a reason code
  from `config/reason_codes.json`, and stay within the configured cap. Approver and reason go to the audit
  trail and to Shopify's `note_attributes`. An open-ended percent offer without `requires.role` is rejected
  at load: "no manual discount without an approver" is enforced on the data too.
* **Offers are validated** at load against a strict schema; a malformed offer is rejected and reported by
  `check-config`, never half-applied.

## Clinical checks

* Sphere and cylinder within the lens's `rx_limits` for both eyes; ADD present and in range for progressive
  and bifocal. A blocked lens names the eye, value and limit, and lists the indices of the same type and
  coating that fit.
* **Axis off the 5 degree grid** (external RX5999) is a CONFIRM: the bill proceeds only if staff
  acknowledge it, which is audited. Blocking would stop valid external prescriptions; ignoring it repeats
  Dr. Iyer's transcription errors.
* Stretch (anomaly flags): sphere change over 1.00 D since the previous prescription (CONFIRM), first-time
  progressive (INFO, names whether the salesperson holds the `progressive_expert` badge), external Rx (INFO).

## Operations

* **Stock**: checked before charging (blocking, with where else it is). The order is created with
  `decrement_ignoring_policy`: once the money is taken and the frame is in the customer's hands, a stale
  count must not make Shopify reject the order. Refund restocks frames at the billing store; lenses never.
* **Stock holds**: Shopify only deducts a frame when the order is created, so between "payment started"
  and "order in Shopify" (or for the whole of an outage) it still shows the frame as available. When a bill
  starts payment, the POS holds its frames in its own database, in the same locked transaction that moves
  the bill to PAYMENT_PENDING; every quote subtracts other bills' holds. Two tills can't both take the last
  unit. A declined or stuck payment keeps the hold (the customer is still at the counter); it is released when
  the bill is cancelled (a stuck terminal payment only by a store manager), voided, or when Shopify's count
  includes the sale (synced). Day-end lists bills still holding frames. *Limit:* channels that read Shopify directly (the website) see the sale only once the
  order lands; across machines the holds need the central database.
* **Re-check at payment**: the draft is re-priced when paying. If stock, an offer (midnight expiry) or a
  tax rate changed since the customer agreed, payment is refused and nothing is charged.
* **Cash**: no cash sale or refund without an open till; a cash refund larger than the drawer is refused
  before anything is recorded; paid-ins and paid-outs need reason codes; variance over Rs 200 is flagged.
* **Refund at the billing store only**: the frame restocks there and that drawer took the money.
* **Remake**: lens-only order at zero (offer `REMAKE_GOODWILL`), no frame line, `inventory_behaviour:
  bypass`, references the parent invoice, records the reason and who bears the lab cost, and is checked
  against the newest valid prescription (a re-test may have replaced the wrong one). It gets an invoice
  number (zero-value tax invoice) so the series stays complete.
* **Bills from before cutover** are read from Shopify on demand (refund, reprint, remake) and kept locally.

## Things found in the starter kit

* The mock terminal is idempotent on `order_ref` only for the first 503; a second POST after a
  *successful* charge creates a second charge. The POS persists the charge id at once and never re-charges
  a bill that has one.
* `localhost` on Windows resolves to IPv6 first; the mock listens on IPv4 only, which costs ~2 s per
  request. The default URL is `http://127.0.0.1:8787`.
* The mock stamps `created_at` with the machine's local offset and filters dates as strings; the adapter
  widens the window by a day and filters precisely in IST.
* Change made to the vendored mock: an optional `--today YYYY-MM-DD` flag that pins the calendar date for
  reproducible demos and tests. Default behaviour is unchanged.

## Open questions for the people on the thread (input for STAKEHOLDER_REPLY.md)

* Finance: flat discounts pro rata across lines, or on a specific line? Who approves partial refunds that
  break an offer's condition (a frame-only refund on a BUNDLE300 bill keeps the bundle discount on the lens)?
  Is a zero-value remake invoice acceptable, or should remakes be delivery challans?
* Finance: credit notes as a separate series per store, confirmed? FY rollover: the prefix comes from
  location data; who changes it on 1 April?
* Retail: Meena wants FESTIVE10 + SECONDPAIR50 + PRIORITY500 together, but FESTIVE10 is non-stackable in
  the data. Which wins: the data or the floor pitch? (One flag in `offers.json` either way.)
* Retail vs Finance: "manager discount, no approval chain" vs "no manual discount without reason code and
  approver". Built: the manager *is* the approver (one tap, their id and a reason code), no chain.
* Optometry: off-grid axis on external prescriptions: confirm (built) or block? Who bears remake lab cost:
  recorded per remake, decided by whom?
* Ops: should refunds be allowed at a store other than the billing store (restock where)?
* Founder: the 10 am email: day-end JSON per store exists (`day-end --json`, `/reports/day-end`); who
  receives it and from which mailbox?
