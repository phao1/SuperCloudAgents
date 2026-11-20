# Aged debt preview

'statement aging DAY [--account ACCOUNT]' allocates posted payments to
native-currency invoice totals, oldest open item first (date then ID).
The preview uses all records posted through DAY including that date.
Failed or future payments do not reduce the debt. Signed refunds reduce
the cash available for allocation. Only positive available cash can
cover an invoice; unallocated cash can be negative if refunds exceed
posted deposits. Invoices already covered in full are omitted.

Each row contains the reference, outstanding amount and days since issue.
JSON also includes the unallocated native amount for every selected
account. The view is read-only and independent of converted statements.
