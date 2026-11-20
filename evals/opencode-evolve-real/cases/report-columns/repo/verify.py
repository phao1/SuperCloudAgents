from exports import csv_text

assert csv_text([{"note": "新,件"}], ["note"], "zh") == '备注\n"新,件"\n'
