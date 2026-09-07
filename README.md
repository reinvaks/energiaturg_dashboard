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

## V7 gas fix
- TTF: official EEX TTF NGP current + 60-day history.
- Estonia/Latvia gas: official EEX LVA-EST NGP current + 60-day history.
- Legacy GET Baltic BGSI is not fabricated; Baltic-Finnish trading migrated to EEX in September 2025.
- CSV parsing is deterministic/defensive and does not invent fallback prices.
