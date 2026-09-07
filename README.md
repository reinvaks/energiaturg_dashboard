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

## V8.1 hotfix
- Fixed a packaging regression where `fetch_getbaltic_history()` referenced
  `EEX_LVAEST_CURRENT_URL` / `EEX_LVAEST_HISTORY_URL` without defining them.
- Both constants are now defined in all three entrypoints.
- The function also contains fixed public-URL fallbacks, so this exact NameError
  cannot take down the app again.

## V8.2 ENTSO-E fix
- Core ENTSO-E access no longer depends on `entsoe-py`.
- Actual generation: official web-api `documentType=A75`, `processType=A16`.
- Actual total load: official web-api `documentType=A65`, `processType=A16`.
- Errors are shown in the UI instead of being swallowed.
- YTD requests are split into monthly chunks to avoid oversized responses.

## V8.3 explicit ENTSO-E diagnostics
A connection smoke-test now runs on every app load and is always rendered near
the top of the page. It reports:
- missing Streamlit secret
- network exception
- HTTP status/body
- ENTSO-E acknowledgement reason
- XML parsing failure
- number of returned data points

The app can no longer silently hide the ENTSO-E failure.
