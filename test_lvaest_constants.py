from pathlib import Path

for name in ["app.py", "energy.app.py", "streamlit_app.py"]:
    text = Path(name).read_text(encoding="utf-8")
    assert "EEX_LVAEST_CURRENT_URL =" in text
    assert "EEX_LVAEST_HISTORY_URL =" in text
    assert text.index("EEX_LVAEST_CURRENT_URL =") < text.index("def fetch_getbaltic_history")
    assert text.index("EEX_LVAEST_HISTORY_URL =") < text.index("def fetch_getbaltic_history")
print("LVA-EST constant regression test: OK")
