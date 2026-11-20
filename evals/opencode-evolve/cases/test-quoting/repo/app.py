import csv
import io
def row(fields):
    output = io.StringIO(newline='')
    csv.writer(output).writerow(fields)
    return output.getvalue()
