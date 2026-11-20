from service import quote

assert quote("a-a1", [{"grams": 501}]) == {
    "zone": "metro", "lines": [{"shipping_cents": 150, "handling_cents": 0}],
    "discount_cents": 0, "total_cents": 150,
}
