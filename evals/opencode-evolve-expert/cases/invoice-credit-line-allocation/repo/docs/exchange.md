# Bank remittances and portable payment archive

'payment bank-import PATH' reads a UTF-8 JSON list. A bank row uses
account, day, amount and reference. Reconcile against the named
account currency and apply the whole list in one ledger update.
Return payments in file order. In the original feed there was no
bank posting identity, so repeat deposit text is not unusual.

'archive export PATH [--account ACCOUNT]' writes v1 payment
exchange data in insertion order, rather than copying local IDs.
The legacy format is a JSON object with version and payments.
Each row has account, day, reference and signed integer cents.
It was designed for two-place accounts. 'archive import PATH'
applies the archive to accounts already present in the receiving desk
and assigns new local IDs. Importing a bad row leaves the file intact.

Use the supported local ledger file for persistent state. Archive
exports never change that state. All file writes use one replacement
in the destination directory.

See [bank register exports](bank-register.md) for the reconciliation spreadsheet.
