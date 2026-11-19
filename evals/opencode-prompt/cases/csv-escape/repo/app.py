def render_row(fields):
    return ",".join(fields) + "\r\n"


def export_name(stem):
    return f"{stem}.csv"
