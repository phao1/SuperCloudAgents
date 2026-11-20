# Posting and account operations

--ledger PATH goes before the command. Errors return code 2, report
'error: MESSAGE' once on stderr, and leave stdout empty. Ordinary
output is one UTF-8 JSON value and a newline.

    account put CODE NAME CURRENCY
    account list
    invoice issue ACCOUNT REFERENCE DAY PATH
    invoice show ID
    invoice list [--account ACCOUNT]
    invoice find ACCOUNT REFERENCE
    payment add ACCOUNT DAY AMOUNT REFERENCE
    payment list [--account ACCOUNT]
    payment bank-import PATH
    rate set SOURCE TARGET DAY RATE
    rate quote SOURCE TARGET DAY AMOUNT

Account codes are ASCII, normalized to uppercase. Code and currency
are the account's stable identity. An existing account can change its
display name but not its currency. Dates use strict YYYY-MM-DD.
Invoice line input is a nonempty JSON list with description, units
and unit_price. Units are positive integers, unit_price is a
nonnegative native decimal string. Line order remains part of the
issued invoice. Invoice IDs and payment IDs are separate insertion
sequences; a failed file operation does not allocate an ID.

Human references are nonblank Unicode strings. Keep the original
spelling in stored and returned records. Invoices can share a text
reference across accounts, never within a single account under the
desk's comparison rules. The initial desk release grouped references
with lowercase and outer whitespace removed. Invoices are immutable
after issue. Payments can be negative to reverse bank activity.
