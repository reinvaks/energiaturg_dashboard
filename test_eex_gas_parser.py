
import csv, io
from datetime import date
import pandas as pd

def parse(text, targets=None):
    rows = []
    reader = csv.reader(io.StringIO(text), delimiter=";")
    header = next(reader)
    assert header[0] == "Gasday"
    assert "IndexValue" in header[1]
    for row in reader:
        dt = pd.to_datetime(row[0], format="%d/%m/%Y").date()
        if targets and dt not in targets:
            continue
        px = float(row[1].replace(",", "."))
        if px:
            rows.append((dt, px))
    return rows

sample = (
    "Gasday;IndexValue (€/MWh);IndexVolume;Status;Timestamp\n"
    "07/09/2026;75,738;1000;Preliminary;07/09/2026 12:00\n"
    "08/09/2026;76,125;500;Preliminary;07/09/2026 12:00\n"
    "09/09/2026;0;0;NoIndex;07/09/2026 12:00\n"
)
got = parse(sample, {date(2026,9,7), date(2026,9,8), date(2026,9,9)})
assert got == [(date(2026,9,7), 75.738), (date(2026,9,8), 76.125)]
print("EEX exact-format parser test: OK")
