# Energiaturgude äpp — Build 6.0

Deploy-safe versioon. Sama rakendus on kolmes entrypoint-failis:

- `energy.app.py`
- `app.py`
- `streamlit_app.py`

Päises peab olema **Build 6.0 • UMM deploy-safe** ja selle all kuvatakse tegelik käivitusfail.
UMM on kolmas vaheleht: **📣 Nord Pool UMM**.

Vajalikud secrets:

```toml
ENTSOE_API_KEY = "..."
GIE_API_KEY = "..."
```

Nord Pool UMM otsepäringu fallback on `data/umm.json`, mida uuendab `.github/workflows/update-umm.yml`.

## V8 — validated EEX gas
- TTF: EEX TTF NGP current + 60-day history.
- Estonia/Latvia: EEX LVA-EST NGP current + 60-day history.
- Exact CSV schema: `Gasday;IndexValue;IndexVolume;Status;Timestamp`.
- Windows-1252 decoding; price is always column 2.
- Zero/no-index values are not displayed.
- No TTF+spread, no guessed numeric column, no synthetic fallback.
