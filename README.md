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
