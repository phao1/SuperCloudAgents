def unique_records(records):
    return list({record["id"]: record for record in records}.values())


def summarize(records):
    return len(records)
