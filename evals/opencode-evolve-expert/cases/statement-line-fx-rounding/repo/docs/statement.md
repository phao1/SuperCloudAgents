# Statement and rate desk

'statement open FIRST LAST [--currency CURRENCY] [--account ACCOUNT]
[--format json|csv]' reads a closed date window. Invoices add to
receivables; payments subtract. Positive balances mean money owed
to the stall. Without an FX target, keep each row and total in its
native currency. JSON wraps 'entries' and 'totals'; each total is
keyed by currency. CSV emits the entries with a header and CRLF.
Maintain day order, invoices before payments on the same day, and
original numeric ID order within a kind.

Rates are direct currency pairs applied on the latest dated quote
no later than an entry's posting day. No inverse or derived cross
rate is implied. A same-currency rate is always one. If a required
rate is absent, the entire statement fails. The standalone 'rate
quote' command is the desk's single-amount indicative quote.
Its tie rule rounds half away from zero to the target currency's
number of decimal places. That quote is independent of the
carrier's statement reconciliation agreement.

Supported fixed places: USD/EUR/CHF 2, JPY 0, KWD 3. Native
amounts are strict decimal strings with sufficient scale (redundant
zeroes are allowed). A report always prints exactly the currency's
fixed number of places. No locale-dependent separators.

See [aged debt](aging.md) for the native account workflow.
