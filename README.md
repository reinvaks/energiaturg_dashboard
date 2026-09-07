# Energiaturu ja reservide armatuurlaud — valideeritud versioon

Algse rakenduse vahelehed, järjestus ja põhivälimus on säilitatud. Muudetud on ainult andmehõive ja kohad, kus algne kood kasutas kontrollimata või sünteetilisi väärtusi.

## Streamlit Secrets

```toml
ENTSOE_API_KEY = "..."
GIE_API_KEY = "..."
```

## Valideeritud automaatsed allikad

- EE/LV/LT/FI spot-hinnad: Elering Dashboard API.
- Eesti YTD tarbimine/tootmine: ENTSO-E Transparency actual load + actual generation per production type.
- Elektri lõpphinnad: Eurostat nrg_pc_204 (household DC) ja nrg_pc_205 (non-household IC), viimane avaldatud poolaasta, all taxes included.
- Installeeritud tuule- ja päikesevõimsus: ENTSO-E A68/A33.
- Eesti maagaasi YTD tarbimine: Eurostat nrg_cb_gasm / G3000 / IC_CAL_MG / TJ_GCV; võrdlus eelneva aasta samade kuudega.
- TTF: EEX NGP TTF ametlik avalik fail.
- Brent: U.S. EIA Europe Brent Spot Price FOB.
- EUA: EEX primaaroksjoni clearing price. See EI ole secondary-market closing price.
- Gaasihoidlad: GIE AGSI+ isikliku API võtmega.
- aFRR/mFRR capacity prices: Baltic Transparency Dashboardi andmed Voltoni avaliku peegli kaudu.
- Eesti tootmisstruktuur, koormus ja piiriülesed füüsilised vood: ENTSO-E actual data.

## Teadlikult täitmata väljad

- GET Baltic BGSI: TTF + spread tuletis eemaldatud; valideeritud automaatse avaliku voo puudumisel ei kuvata hinda.
- FCR capacity: varasemad sünteetilised väärtused eemaldatud; valideeritud voogu ei ole sellesse versiooni ühendatud.
- “Põlevkivi ja muud” ning “Maagaas / Koostootmine” 5-aasta capacity tabelis: ENTSO-E kategooriad ei vasta nendele koondnimetustele üks-ühele, seega ei agregeerita neid oletuslikult.
- “Eesti võrku lisandunud uus tootmisvõimsus”: installed capacity aastamuutust ei esitata uue liitumisvõimsusena.
- “Eestisse tehtud energeetika investeeringud”: jäetud tühjaks kuni täpselt sama definitsiooniga Statistikaameti tabel on valideeritud.

## Andmekvaliteedi põhimõte

Kui allikas ei vasta, API võti puudub, skeemi ei saa usaldusväärselt tõlgendada või mõõdiku definitsioon ei sobi allikaga, kuvab rakendus puuduva väärtuse. Mock-, juhuslikke, hard-coded või teistest instrumentidest tuletatud hindu/mahud ei kasutata.

- Inčukalns storage stock: primary source is Conexus Baltic Grid Storage Stocks; GIE AGSI+ is Latvia fallback/cross-check only.
