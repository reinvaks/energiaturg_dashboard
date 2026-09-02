import requests
from datetime import datetime

def fetch_nordpool_umms():
    # Nord Pool UMM avalik API lõpp-punkt (filtreeritud Balti / Põhjamaade piirkonnale)
    api_url = "https://umm.nordpoolgroup.com/api/messages"
    
    params = {
        "limit": 10,              # Viimased 10 teadet
        "areas": "EE",            # Saab lisada või eemaldada (nt "EE,LV,LT,FI")
        "messageType": "Production"  # Saab filtreerida: Production, Transmission, Consumption
    }
    
    headers = {
        "Accept": "application/json",
        "User-Agent": "EnergyMonitor/1.0"
    }

    try:
        response = requests.get(api_url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"Viga teadete pärimisel: {e}")
        return []

    results = []
    
    # Nord Pool tagastab sõnumite nimekirja objektidena
    messages = data if isinstance(data, list) else data.get("items", [])

    for msg in messages:
        msg_id = msg.get("messageId") or msg.get("id")
        subject = msg.get("subject", "N/A")
        
        # Objekti ja võimsuste ekstraheerimine
        # Sõltuvalt teate tüübist on andmed kas põhiosas või mõjutatud ressursside massiivis
        affected_units = msg.get("affectedUnits", [])
        if affected_units:
            unit = affected_units[0]
            unit_name = unit.get("unitName") or unit.get("resourceName") or subject
            installed_mw = unit.get("installedCapacity", "N/A")
            available_mw = unit.get("availableCapacity", "N/A")
            unavailable_mw = unit.get("unavailableCapacity", "N/A")
        else:
            unit_name = subject
            installed_mw = msg.get("installedCapacity", "N/A")
            available_mw = msg.get("availableCapacity", "N/A")
            unavailable_mw = msg.get("unavailableCapacity", "N/A")

        # Otselink teatele Nord Pooli portaalis
        direct_link = f"https://umm.nordpoolgroup.com/#/messages/{msg_id}"

        results.append({
            "objekt": unit_name,
            "paigaldatud_mw": installed_mw,
            "saadaval_mw": available_mw,
            "mittesaadaval_mw": unavailable_mw,
            "link": direct_link,
            "uuendatud": msg.get("messagePublishTime", "")
        })

    return results

# Tulemuste kuvamine
if __name__ == "__main__":
    umms = fetch_nordpool_umms()
    print(f"{'Objekt':<35} | {'Paigaldatud':<12} | {'Saadaval':<10} | {'Link teatele'}")
    print("-" * 110)
    for u in umms:
        print(f"{u['objekt'][:35]:<35} | {str(u['paigaldatud_mw']) + ' MW':<12} | {str(u['saadaval_mw']) + ' MW':<10} | {u['link']}")
