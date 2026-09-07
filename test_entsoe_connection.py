
import os, requests, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

token = os.getenv("ENTSOE_API_KEY", "")
assert token, "ENTSOE_API_KEY missing from environment"

now = datetime.now(timezone.utc)
params = {
    "securityToken": token,
    "documentType": "A65",
    "processType": "A16",
    "outBiddingZone_Domain": "10Y1001A1001A39I",
    "periodStart": (now - timedelta(hours=6)).strftime("%Y%m%d%H%M"),
    "periodEnd": now.strftime("%Y%m%d%H%M"),
}
r = requests.get("https://web-api.tp.entsoe.eu/api", params=params, timeout=30)
print("HTTP", r.status_code)
print(r.text[:1000])
