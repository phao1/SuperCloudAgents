# Bank register exports

'payment register PATH [--account ACCOUNT] [--since DAY]
[--through DAY]' writes a CSV register with day, id, account,
currency, reference and amount columns. Dates and filters are
inclusive and independent. Rows sort by posting date then local ID.
An empty range still writes the header. Amounts retain the native
payment sign. Register files are intended for operator spreadsheets,
and each row can be looked up in 'payment list'. This register has
no effect on the archive or remittance interfaces.

The exporter uses UTF-8 and CRLF, including a final CRLF. It escapes
commas, quotes and embedded line breaks without modifying the
source reference. Once the rows are validated, the destination file
is replaced as one write. An invalid range leaves it unchanged.
