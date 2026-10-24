# Starter kit

Everything here is synthetic but shaped like the real thing. Use it as-is, extend it, or replace it; say what you changed.

```
data/                        seed JSON in the shapes below (regenerate with generate_seed.py, check with validate_seed.py)
mock-apis/mock_server.py     one local server, no dependencies, three surfaces: Shopify Admin, store tool, payment terminal
examples.sh                  a curl walkthrough of a full bill and refund against the mock
sample_invoice.txt           what a finished invoice looks like, for order #1001
schema.md                    what lives where, and the objects your POS owns
```

## Run

Requires Python 3.8+. Nothing to install.

```
python3 mock-apis/mock_server.py --no-flake  # http://localhost:8787   (drop --no-flake to get the 429/503 behaviour)
python3 validate_seed.py                     # prints OK
bash examples.sh                             # walks one bill through lookup, charge, Shopify create, refund, day-end
```

The mock is in memory; restart resets it to the seed. `examples.sh` is a walkthrough of the calls, not a reference implementation.

## Three systems, one port

| Surface | Base | What it is |
|---|---|---|
| **Shopify Admin** | `/admin/api/2026-10/` | The system of record. Customers, locations, products/variants, inventory levels, orders, transactions, refunds. Shapes follow Shopify's REST Admin API. |
| **Store tool (SMT)** | `/smt/` | Prescriptions by phone, staff by store. Shopify has no place for a prescription; this is where it lives. |
| **Payment terminal** | `/terminal/` | Razorpay-like. Charge, check status, refund. After a charge succeeds you still record the transaction in Shopify. |

Routes and payloads are documented at the top of `mock_server.py`.

Behaviour that matches production and that your code has to live with:

- Money is a **decimal string** (`"104.98"`). Numbers are rejected.
- `order_number` is **one global series** across all stores and the website. Your invoice number goes in `note_attributes`.
- `taxes_included` must be `true`. You supply `tax_lines` per line item; Shopify stores what you send.
- Stock is decremented on order create only when `inventory_behaviour` is `decrement_obeying_policy` or `decrement_ignoring_policy`. Default is `bypass`.
- Refunds: call `refunds/calculate.json` first if you like; `refunds.json` rejects a refund larger than captured, rejects restocking a lens (not inventory-tracked), and needs a `location_id` when `restock_type` is `return`.
- About **10% of writes return `429`** with a `Retry-After` header. Every response carries `X-Shopify-Shop-Api-Call-Limit`.
- The terminal returns **`503` once** for a new `order_ref` about 30% of the time. Retrying with the same `order_ref` succeeds and is idempotent.
- The store tool returns prescriptions newest first. `valid_until` is twelve months after the test date.

## Data

| File | Shape | What to know |
|---|---|---|
| `locations.json` | Shopify location | 7 stores + HQ. Each carries an `eluno` block: `store_code`, `invoice_prefix`, `state_code`, `kind`. |
| `customers.json` | Shopify customer | `tags` carry cohorts (`priority`, `campaign:progressive_oct26`, `family`). `total_spent` is lifetime value. Phones are in a non-dialable synthetic range. |
| `products.json` | Shopify product/variant | `product_type` Frame or Lens. Frames: one variant each, inventory-tracked, `eluno.hsn`, `eluno.category` (optical/sunglasses), `eluno.rx_able`. Lenses: one product per type × index, one variant per coating, not inventory-tracked, `eluno.rx_limits` (the lab's constraints), `eluno.lab_tat_days`. One frame product is `archived` but appears in history. |
| `inventory_levels.json` | Shopify inventory level | `available` per `inventory_item_id` per `location_id`. Frames only. Some stores have 0 of some SKUs. |
| `prescriptions.json` | Store tool | OD = right eye, OS = left. `sph`/`cyl`/`add` on a 0.25 D grid, `axis` 0–180. In-store prescriptions use a 5° axis grid; one external one (`RX5999`) does not. Customers have 0–3; use the latest valid one. |
| `staff.json` | Store tool | Roles sales, optometrist, store_manager. `can_override_discount`, `progressive_expert` badge. |
| `tax_rates.json` | POS-owned | HSN → rate in basis points with effective dates. The rates carry dates for a reason. |
| `offers.json` | POS-owned | 8 offers: percent and flat, frame/lens/pair/cart scope, conditions on tags, lens type, index, pairs in cart; `stackable`, `priority`, dates; one expired; one that needs a store manager and a reason code. The rule schema is loose on purpose. |
| `orders_sample.json` | Shopify order | 12 bills as the old system wrote them, with `tax_lines`, `discount_allocations`, the prescription in line `properties`, invoice number / store / channel / staff / payment method in `note_attributes`, `transactions`, and `refunds`. Includes a two-pair bill, sunglasses only, lens only, a full refund with restock, a zero-charge remake referencing its parent invoice, and a bill where a better offer combination existed. Tax math is correct to the paisa; use them as fixtures. |
| `till_sessions_sample.json` | POS-owned | Two store-days: opening float, cash sales, paid-outs, a paid-in, counted close, variance. One is short by ₹250. |

## Money and tax conventions

- Compute in integer paise. Serialise to decimal strings at the Shopify boundary.
- Prices are MRP inclusive of GST. Taxable value = gross × 10000 ÷ (10000 + rate_bps), rounded half-up to the paisa per line; tax = gross − taxable; CGST = half the tax rounded half-up, SGST = the remainder. Every line reconciles to the paisa. `validate_seed.py` is the reference implementation.
- Intra-state supply throughout (CGST + SGST).
- Discounts apply to the inclusive price before tax is backed out.

## Not covered by the mocks, on purpose

Catalog search UI, customer creation, prescription creation, eye-test booking, lab order creation, printer hardware, e-invoicing/IRN, Shopify webhooks. Say what you'd need from them; don't build them.
