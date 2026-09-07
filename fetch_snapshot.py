import logging
import sys

from umm_client import fetch_umm_messages, save_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

rows, meta = fetch_umm_messages()
if meta.error:
    logging.error("Nord Pool fetch failed: %s", meta.error)
    sys.exit(1)
if not rows:
    logging.error("Nord Pool returned zero messages; refusing to overwrite last successful snapshot")
    sys.exit(2)

save_snapshot("data/umm.json", rows, meta)
logging.info("Saved %s UMM messages to data/umm.json", len(rows))
