# ADR 009 — Carrier settlement statement

        For a statement with a target currency, extend each invoice line in
        native currency (units times price) before conversion, convert on the
        invoice posting day, then round each converted line to target minor
        units using half-to-even. Sum those rounded amounts for the existing
        per-invoice entry. Do not round the converted unit price and do not
        round the combined invoice total. Convert a signed payment entry
        as one amount using the same half-to-even rule. Totals sum the
        already-rounded entries. Apply scope and date filters before rates.

        Keep native statements unchanged. Same-currency entries do not need
        a rate and target scale still determines their string. Normalize zero
        strings so there is no minus sign on a zero result. Retain the
        ordinary entry order and CSV format. Keep the quote endpoint's legacy
        rounding from the rate desk guide. Conversion must use exact decimal
        arithmetic on declared currency scales, including zero or three
        decimal places.
