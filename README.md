# Fronius GEN24 Zähler-Emulator

Emuliert einen Fronius Smart Meter per **Modbus TCP (SunSpec)** für Fronius GEN24
Wechselrichter. Die Netzleistung kommt von einem **Tasmota-IR-Lesekopf (SML)** am
Stromzähler – oder wird zum Testen **simuliert** (z. B. 400 W, 1000 W, 3000 W).

> **Hinweis:** Nicht offiziell von Fronius. Bei aktiver Einspeisebegrenzung regelt der
> Wechselrichter anhand dieser Werte – falsche Werte können zu Überschreitung der
> Einspeisegrenze führen. Nutzung auf eigene Verantwortung. Getestet mit einem
> SunSpec-Client, noch nicht an jedem GEN24-Firmwarestand.

## Funktionsweise

```
Stromzähler ──IR──> Tasmota (SML) ──HTTP──> fronius_meter_emulator.py ──Modbus TCP──> GEN24
```

- Fragt jede Sekunde `http://<tasmota>/cm?cmnd=Status%2010` ab.
- Stellt die Werte als SunSpec-Zähler bereit: Kennung `SunS` ab Register 40000,
  Common Model 1 (Fronius / Smart Meter TS 65A-3), Meter Model 213 (Float), End-Model.
- Wirkleistung gesamt liegt auf Register 40097 (Float32).
- Vorzeichen (Fronius-Konvention am Einspeisepunkt): **positiv = Bezug**, **negativ = Einspeisung**.
- Die Leistung wird gleichmäßig auf 3 Phasen verteilt; Spannung (230 V) und
  Frequenz (50 Hz) sind feste Werte, da der Lesekopf sie nicht liefert.
- Energiezähler Bezug/Einspeisung kommen aus `Total_in` / `Total_out` (kWh).
- **Ausfallschutz:** Liefert Tasmota länger als 10 s keine Daten, antwortet der Emulator
  mit einem Modbus-Fehler statt mit veralteten Werten, damit der Wechselrichter einen
  Zählerfehler erkennt.
- Keine Abhängigkeiten außer Python ≥ 3.9 (Modbus-Server mit `asyncio`, kein pymodbus).

## Start

```
python fronius_meter_emulator.py
```

Port 502 benötigt unter Linux root-Rechte bzw. unter Windows Administratorrechte.

### Optionen

| Option | Bedeutung |
|---|---|
| `--tasmota IP` | IP des Tasmota-Lesekopfs (Standard aus der Konfiguration im Skript) |
| `--port PORT` | Modbus-TCP-Port (Standard 502) |
| `--host IP` | Lokale IP, auf der gelauscht wird (Standard `0.0.0.0` = alle Netzwerkkarten) |
| `--unit-id ID` | Modbus-Adresse 1–247 (1 = Primärzähler); ohne Angabe wird auf jede ID geantwortet |
| `--serial SN` | Gemeldete Seriennummer (Standard `00000001`) |
| `--name NAME` | Name der Instanz, wird jeder Konsolenzeile vorangestellt |
| `--json-prefix`, `--field-power`, `--field-import`, `--field-export` | Tasmota-Feldnamen (überschreiben die Werte im Skript) |
| `--invert` | Vorzeichen der Leistung umkehren |
| `--sim W [W ...]` | Simulationsmodus: Leistung(en) in W statt Tasmota |
| `--sim-interval S` | Sekunden pro Wert bei mehreren `--sim`-Werten (Standard 30) |

`0.0.0.0` ist keine Adresse zum Eintragen: Am GEN24 wird immer die echte LAN-IP des
Rechners angegeben (`ipconfig` bzw. `hostname -I`).

### Simulationsmodus

```
python fronius_meter_emulator.py --sim 1000                     # konstant 1000 W Bezug
python fronius_meter_emulator.py --sim -1500                    # konstant 1500 W Einspeisung
python fronius_meter_emulator.py --sim 400 500 1000 3000        # nacheinander je 30 s, endlos
python fronius_meter_emulator.py --sim 400 3000 --sim-interval 10
```

Im laufenden Betrieb kann in der Konsole ein neuer Wert eingegeben werden:

| Eingabe | Wirkung |
|---|---|
| `2500`, `-800`, `1000w` | Wert sofort setzen, automatischer Durchlauf stoppt |
| `auto` | Durchlauf der `--sim`-Liste fortsetzen |
| `q` | Beenden |

Die Energiezähler werden im Simulationsmodus aus der Leistung hochgezählt.

### Mehrere Instanzen auf einem PC

Jede Instanz ist ein eigener Prozess. Jede Instanz bekommt eine eigene **IP-Adresse**,
eine eigene **Unit-ID** (Modbus-Adresse) und eine eigene **Seriennummer**, damit der
Wechselrichter die Zähler auseinanderhalten kann.

**Empfohlen – eigene IP pro Instanz, alle auf Port 502:** Dem PC zusätzliche feste
IP-Adressen geben (Windows: Netzwerkadapter → IPv4 → Erweitert → IP-Adressen hinzufügen)
und jede Instanz mit `--host` nur auf ihrer IP lauschen lassen:

```
python fronius_meter_emulator.py --name Netz --host 10.10.55.20 --unit-id 1 --serial 10000001
python fronius_meter_emulator.py --name WP   --host 10.10.55.21 --unit-id 2 --serial 10000002 --tasmota 10.10.55.106
```

Am GEN24 dann jeweils die passende IP und Modbus-Adresse eintragen. `--host` muss eine
IP sein, die dem PC tatsächlich zugewiesen ist – sonst bricht das Skript mit einer
Meldung samt Liste der eigenen IPs ab. Pro IP und Port kann nur eine Instanz laufen;
eine Instanz mit `0.0.0.0` belegt den Port auf allen IPs. Bei Bind-Fehlern beendet sich
das Skript mit Exit-Code 1.

**Nicht empfohlen – gleiche IP, verschiedene Ports** (`--port 503`): Technisch läuft das,
laut Berichten im Photovoltaikforum akzeptiert der GEN24 aber keine mehreren Zähler auf
derselben IP mit unterschiedlichen Ports. Ungetestet.

**MQTT:** Der GEN24-Komponententyp „Fronius Smart Meter (MQTT)“ ist nicht nutzbar – die
Verbindung ist TLS-verschlüsselt mit Fronius-eigenen Zertifikaten und laut Fronius nur
für Fronius-Produkte gedacht. Der Emulator wird daher immer als „Fronius Smart Meter (TCP)“
eingebunden.

## Konfiguration

Oben im Skript:

| Variable | Bedeutung |
|---|---|
| `TASMOTA_IP` | IP des Lesekopfs |
| `JSON_PREFIX` | Name des SML-Blocks in Tasmota (meist `SML`) |
| `FIELD_POWER` | Feld der aktuellen Leistung in W (z. B. `Power_curr`) |
| `FIELD_IMPORT` / `FIELD_EXPORT` | Zählerstände in kWh (z. B. `Total_in` / `Total_out`), `None` falls nicht vorhanden |
| `INVERT_SIGN` | `True`, falls der Zähler Einspeisung positiv liefert |
| `UNIT_ID` | Modbus Unit-ID; `None` = auf jede ID antworten |
| `STALE_TIMEOUT` | Sekunden ohne Daten bis zur Fehlermeldung |
| `NOMINAL_VOLTAGE` / `NOMINAL_FREQ` | gemeldete Spannung / Frequenz |

Die Feldnamen lassen sich im Browser prüfen: `http://<tasmota>/cm?cmnd=Status%2010`.

## Einrichtung am GEN24

Im Webinterface des Wechselrichters unter **Gerätekonfiguration → Komponenten →
Primärzähler** einen Fronius Smart Meter IP hinzufügen, IP-Adresse des Rechners mit dem
Emulator eintragen und als Position **Einspeisepunkt** wählen. Die Modbus-Adresse muss
zur `--unit-id` passen (Primärzähler = 1); ohne `--unit-id` antwortet der Emulator auf jede
Adresse. Weitere Instanzen als Sekundärzähler (Verbraucher/Erzeuger) mit eigener
Modbus-Adresse hinzufügen.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
