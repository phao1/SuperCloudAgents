# Quayside Ledger

Quayside tracks invoices and bank deposits at a group of market
stalls where the desks reconcile transactions offline. Each account
has a native currency. The operator can post invoices, import daily
remittances, review an account statement or transfer archived
payments between desk files.

    python -B -m quayside --ledger market.json account put blue "Blue Stall" USD
    python -B -m quayside --ledger market.json payment add blue 2024-07-12 4.50 "Deposit #15"
    python -B -m quayside --ledger market.json statement open 2024-07-01 2024-07-31

Use Python 3.12. The ledger file is plain UTF-8 JSON and is created
on first write. Use [posting](docs/posting.md), [statement](docs/statement.md)
and [bank exchange](docs/exchange.md) for the desk conventions.
Current changes are recorded under docs/adr/. Run the older desk
regressions with python -B -m unittest discover -s tests.
