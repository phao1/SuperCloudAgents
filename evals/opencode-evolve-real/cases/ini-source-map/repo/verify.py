from app import parse

assert parse('location = "前;后"\r\n [ops]\r\nname=one\\\r\n two') == {
    "": {"location": ("前;后", 1, 1)}, "ops": {"name": ("one\ntwo", 3, 1)},
}
