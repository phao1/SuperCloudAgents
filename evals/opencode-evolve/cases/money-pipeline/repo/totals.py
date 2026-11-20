from validation import validate
def totals(rows):
    return {r['currency']:r['cents'] for r in rows}
