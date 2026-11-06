"""
Fronius Smart Meter Emulator (Modbus TCP / SunSpec) fuer Fronius GEN24.

Liest die Netzleistung von einem Tasmota-IR-Lesekopf (SML) per HTTP und
stellt sie als SunSpec-Zaehler (Common Model 1 + Meter Model 213, Float)
per Modbus TCP bereit.

Bewusst ohne pymodbus: dessen API aendert sich laufend (BinaryPayloadBuilder
entfernt, ModbusDeviceContext in 3.16 deprecated). Der Wechselrichter liest
nur Holding Register (FC3), das ist mit asyncio in wenigen Zeilen erledigt.

Vorzeichen (Fronius-Konvention am Einspeisepunkt):
    W > 0  -> Bezug aus dem Netz
    W < 0  -> Einspeisung ins Netz

Simulationsmodus (ohne Tasmota), z.B.:
    python fronius_meter_emulator.py --sim 1000
    python fronius_meter_emulator.py --sim 400 500 1000 3000 --sim-interval 30
Im laufenden Betrieb kann ein neuer Wert in der Konsole eingegeben werden.

Mehrere Instanzen auf einem PC: je Instanz eigener --port (oder --host),
eigene --unit-id und eigene --serial verwenden (siehe --help).
"""

import argparse
import asyncio
import json
import struct
import sys
import time
import urllib.request

# ==================== KONFIGURATION ====================
TASMOTA_IP = "10.10.55.105"
POLL_INTERVAL = 1.0          # Sekunden zwischen Tasmota-Abfragen
STALE_TIMEOUT = 10.0         # Nach so vielen Sekunden ohne Daten: Modbus-Fehler

JSON_PREFIX = "SML"
FIELD_POWER = "Power_curr"   # W (positiv = Bezug)
FIELD_IMPORT = "Total_in"    # kWh Bezug   (None, falls nicht vorhanden)
FIELD_EXPORT = "Total_out"   # kWh Einspeisung (None, falls nicht vorhanden)
INVERT_SIGN = False          # True, falls der Zaehler Einspeisung positiv liefert

MODBUS_HOST = "0.0.0.0"
MODBUS_PORT = 502
UNIT_ID = None               # None = auf jede Unit-ID antworten, sonst z.B. 1

NOMINAL_VOLTAGE = 230.0      # Wird gemeldet, da der Lesekopf keine Spannung liefert
NOMINAL_FREQ = 50.0

SERIAL = "00000001"          # Seriennummer; bei mehreren Instanzen unterschiedlich!
NAME = ""                    # Praefix fuer die Konsolenausgabe, z.B. "Netz"
# =======================================================

SUNSPEC_BASE = 40000         # Fronius liest ab Protokolladresse 40000
REG_COUNT = 200

# Offsets relativ zu SUNSPEC_BASE
OFF_COMMON = 2               # Model 1:   ID @40002, L @40003, Daten 40004..40068
OFF_METER = 69               # Model 213: ID @40069, L @40070, Daten 40071..40194
OFF_END = 195                # End-Model: 0xFFFF, 0

# Float-Punkte in Model 213 (Index * 2 + 40071)
P_A, P_APH = 0, 1            # Strom gesamt, Phase A..C (1..3)
P_PHV, P_PHVPH = 4, 5        # Spannung L-N Mittel, Phase A..C (5..7)
P_PPV, P_PPVPH = 8, 9        # Spannung L-L Mittel, AB/BC/CA (9..11)
P_HZ = 12
P_W, P_WPH = 13, 14          # Wirkleistung gesamt, Phase A..C (14..16)
P_VA, P_VAPH = 17, 18
P_VAR, P_VARPH = 21, 22
P_PF, P_PFPH = 25, 26
P_WH_EXP, P_WH_EXP_PH = 29, 30
P_WH_IMP, P_WH_IMP_PH = 33, 34

regs = [0] * REG_COUNT
state = {"power": 0.0, "imp_wh": 0.0, "exp_wh": 0.0, "last_ok": 0.0}


def log(msg):
    print(f"[{NAME}] {msg}" if NAME else msg, flush=True)


def _str_regs(text, n_regs):
    raw = text.encode("ascii")[: n_regs * 2].ljust(n_regs * 2, b"\x00")
    return list(struct.unpack(f">{n_regs}H", raw))


def _set(off, values):
    regs[off : off + len(values)] = values


def _set_float(point, value):
    hi, lo = struct.unpack(">HH", struct.pack(">f", float(value)))
    off = OFF_METER + 2 + point * 2
    regs[off] = hi
    regs[off + 1] = lo


def init_static_registers():
    _set(0, [0x5375, 0x6E53])                     # 'SunS'
    _set(OFF_COMMON, [1, 65])                     # Common Model, Laenge 65
    o = OFF_COMMON + 2
    _set(o, _str_regs("Fronius", 16))             # Mn
    _set(o + 16, _str_regs("Smart Meter TS 65A-3", 16))  # Md
    _set(o + 32, _str_regs("", 8))                # Opt
    _set(o + 40, _str_regs("1.0", 8))             # Vr
    _set(o + 48, _str_regs(SERIAL, 16))           # SN
    regs[o + 64] = UNIT_ID if UNIT_ID is not None else 1  # DA
    _set(OFF_METER, [213, 124])                   # Meter Model 213, Laenge 124
    _set(OFF_END, [0xFFFF, 0])


def update_meter_registers():
    w = state["power"]
    w_ph = w / 3.0
    a_ph = abs(w_ph) / NOMINAL_VOLTAGE
    v_ll = NOMINAL_VOLTAGE * 3 ** 0.5

    _set_float(P_A, a_ph * 3)
    _set_float(P_PHV, NOMINAL_VOLTAGE)
    _set_float(P_PPV, v_ll)
    for i in range(3):
        _set_float(P_APH + i, a_ph)
        _set_float(P_PHVPH + i, NOMINAL_VOLTAGE)
        _set_float(P_PPVPH + i, v_ll)
        _set_float(P_WPH + i, w_ph)
        _set_float(P_VAPH + i, abs(w_ph))
        _set_float(P_VARPH + i, 0.0)
        _set_float(P_PFPH + i, 1.0)
        _set_float(P_WH_EXP_PH + i, state["exp_wh"] / 3.0)
        _set_float(P_WH_IMP_PH + i, state["imp_wh"] / 3.0)
    _set_float(P_HZ, NOMINAL_FREQ)
    _set_float(P_W, w)
    _set_float(P_VA, abs(w))
    _set_float(P_VAR, 0.0)
    _set_float(P_PF, 1.0)
    _set_float(P_WH_EXP, state["exp_wh"])
    _set_float(P_WH_IMP, state["imp_wh"])


# ---------------- Tasmota ----------------

def _read_tasmota():
    url = f"http://{TASMOTA_IP}/cm?cmnd=Status%2010"
    with urllib.request.urlopen(url, timeout=2) as resp:
        return json.load(resp)


async def poll_tasmota():
    while True:
        try:
            data = await asyncio.to_thread(_read_tasmota)
            meter = data.get("StatusSNS", {}).get(JSON_PREFIX, {})
            power = meter.get(FIELD_POWER)
            if power is None:
                log(f"[Tasmota] Feld '{FIELD_POWER}' in '{JSON_PREFIX}' fehlt: {meter}")
            else:
                power = float(power)
                state["power"] = -power if INVERT_SIGN else power
                if FIELD_IMPORT and meter.get(FIELD_IMPORT) is not None:
                    state["imp_wh"] = float(meter[FIELD_IMPORT]) * 1000.0
                if FIELD_EXPORT and meter.get(FIELD_EXPORT) is not None:
                    state["exp_wh"] = float(meter[FIELD_EXPORT]) * 1000.0
                state["last_ok"] = time.monotonic()
                update_meter_registers()
                log(f"[Tasmota] {state['power']:.0f} W  "
                      f"Bezug {state['imp_wh'] / 1000:.3f} kWh  "
                      f"Einsp. {state['exp_wh'] / 1000:.3f} kWh")
        except Exception as exc:  # noqa: BLE001
            log(f"[Tasmota] nicht erreichbar: {exc}")
        await asyncio.sleep(POLL_INTERVAL)


# ---------------- Simulation ----------------

sim = {"values": [], "index": 0, "next_switch": 0.0, "manual": False}


def _set_sim_power(watt):
    state["power"] = float(watt)
    log(f"[Simulation] Leistung jetzt {watt:.0f} W "
          f"({'Bezug' if watt >= 0 else 'Einspeisung'})")


async def simulate(interval):
    """Liefert simulierte Werte statt Tasmota-Daten.

    Ein Wert: konstant. Mehrere Werte: werden alle `interval` Sekunden
    nacheinander durchlaufen (Endlosschleife), bis ein Wert manuell
    eingegeben wird. Die Energiezaehler werden aus der Leistung integriert.
    """
    _set_sim_power(sim["values"][0])
    sim["next_switch"] = time.monotonic() + interval
    last = time.monotonic()
    while True:
        now = time.monotonic()
        dt_h = (now - last) / 3600.0
        last = now
        if state["power"] >= 0:
            state["imp_wh"] += state["power"] * dt_h
        else:
            state["exp_wh"] += -state["power"] * dt_h

        if (not sim["manual"] and len(sim["values"]) > 1
                and now >= sim["next_switch"]):
            sim["index"] = (sim["index"] + 1) % len(sim["values"])
            sim["next_switch"] = now + interval
            _set_sim_power(sim["values"][sim["index"]])

        state["last_ok"] = now
        update_meter_registers()
        await asyncio.sleep(POLL_INTERVAL)


async def read_console():
    """Neuen Simulationswert per Tastatur setzen (Zahl + Enter, 'q' = Ende)."""
    if not sys.stdin or not sys.stdin.isatty():
        return
    log("[Simulation] Neuen Wert in W eingeben + Enter "
          "(negativ = Einspeisung, 'auto' = Liste fortsetzen, 'q' = Ende)")
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            return
        text = line.strip().lower().rstrip("w").strip()
        if not text:
            continue
        if text in ("q", "quit", "exit"):
            raise KeyboardInterrupt
        if text == "auto":
            sim["manual"] = False
            sim["next_switch"] = 0.0
            log("[Simulation] Automatischer Durchlauf fortgesetzt")
            continue
        try:
            watt = float(text.replace(",", "."))
        except ValueError:
            log(f"[Simulation] Ungueltige Eingabe: {line.strip()!r}")
            continue
        sim["manual"] = True
        _set_sim_power(watt)


# ---------------- Modbus TCP ----------------

def _exception(fc, code):
    return struct.pack(">BB", fc | 0x80, code)


def handle_pdu(pdu):
    fc = pdu[0]
    if fc not in (3, 4):
        return _exception(fc, 0x01)               # Illegal Function
    if len(pdu) < 5:
        return _exception(fc, 0x03)
    start, count = struct.unpack(">HH", pdu[1:5])
    off = start - SUNSPEC_BASE
    if not 1 <= count <= 125 or off < 0 or off + count > REG_COUNT:
        return _exception(fc, 0x02)               # Illegal Data Address
    # Keine aktuellen Messwerte -> Fehler melden statt alte Werte liefern,
    # damit der Wechselrichter in seinen Zaehler-Fehlermodus geht.
    meter_data_start = OFF_METER + 2
    if off + count > meter_data_start and off < OFF_END:
        if time.monotonic() - state["last_ok"] > STALE_TIMEOUT:
            return _exception(fc, 0x04)           # Server Device Failure
    values = regs[off : off + count]
    return struct.pack(f">BB{count}H", fc, count * 2, *values)


async def handle_client(reader, writer):
    peer = writer.get_extra_info("peername")
    log(f"[Modbus] Verbindung von {peer}")
    try:
        while True:
            header = await reader.readexactly(7)
            tid, pid, length, unit = struct.unpack(">HHHB", header)
            pdu = await reader.readexactly(length - 1)
            if pid != 0:
                continue
            if UNIT_ID is not None and unit != UNIT_ID:
                resp = _exception(pdu[0], 0x0B)   # Gateway Target Failed
            else:
                resp = handle_pdu(pdu)
            writer.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        log(f"[Modbus] Verbindung getrennt {peer}")
        writer.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Fronius GEN24 Smart Meter Emulator (Modbus TCP / SunSpec)",
        epilog="Beispiele:\n"
               "  %(prog)s                          Tasmota-Werte weiterreichen\n"
               "  %(prog)s --sim 1000               konstant 1000 W Bezug\n"
               "  %(prog)s --sim -1500              konstant 1500 W Einspeisung\n"
               "  %(prog)s --sim 400 500 1000 3000  Werte nacheinander, je 30 s\n"
               "  %(prog)s --sim 400 3000 --sim-interval 10\n"
               "\n"
               "Zwei Instanzen auf einem PC (eigener Port, Unit-ID und Seriennummer):\n"
               "  %(prog)s --name Netz --port 502 --unit-id 1 --serial 10000001\n"
               "  %(prog)s --name WP   --port 503 --unit-id 2 --serial 10000002 "
               "--tasmota 10.10.55.106",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", default=NAME,
                   help="Name der Instanz, wird jeder Konsolenzeile vorangestellt")
    p.add_argument("--sim", type=float, nargs="+", metavar="W",
                   help="Simulationsmodus: Leistung(en) in W statt Tasmota "
                        "(positiv = Bezug, negativ = Einspeisung)")
    p.add_argument("--sim-interval", type=float, default=30.0, metavar="S",
                   help="Sekunden pro Wert bei mehreren --sim-Werten (Standard 30)")
    p.add_argument("--port", type=int, default=MODBUS_PORT,
                   help=f"Modbus-TCP-Port (Standard {MODBUS_PORT})")
    p.add_argument("--host", default=MODBUS_HOST, metavar="IP",
                   help=f"Lokale IP, auf der gelauscht wird (Standard {MODBUS_HOST} = alle)")
    p.add_argument("--unit-id", type=int, default=UNIT_ID, metavar="ID",
                   help="Modbus-Adresse/Unit-ID (1 = Primaerzaehler). "
                        "Ohne Angabe wird auf jede Unit-ID geantwortet")
    p.add_argument("--serial", default=SERIAL,
                   help=f"Gemeldete Seriennummer (Standard {SERIAL}); "
                        "bei mehreren Instanzen unterschiedlich waehlen")
    p.add_argument("--tasmota", default=TASMOTA_IP, metavar="IP",
                   help=f"IP des Tasmota-Lesekopfs (Standard {TASMOTA_IP})")
    p.add_argument("--json-prefix", default=JSON_PREFIX,
                   help=f"SML-Block in der Tasmota-Antwort (Standard {JSON_PREFIX})")
    p.add_argument("--field-power", default=FIELD_POWER,
                   help=f"Feld fuer die Leistung in W (Standard {FIELD_POWER})")
    p.add_argument("--field-import", default=FIELD_IMPORT,
                   help=f"Feld Zaehlerstand Bezug in kWh (Standard {FIELD_IMPORT}, "
                        "'' = keins)")
    p.add_argument("--field-export", default=FIELD_EXPORT,
                   help=f"Feld Zaehlerstand Einspeisung in kWh (Standard {FIELD_EXPORT}, "
                        "'' = keins)")
    p.add_argument("--invert", action="store_true", default=INVERT_SIGN,
                   help="Vorzeichen der Leistung umkehren")
    args = p.parse_args(argv)
    if args.sim_interval <= 0:
        p.error("--sim-interval muss > 0 sein")
    if not 1 <= args.port <= 65535:
        p.error("--port muss zwischen 1 und 65535 liegen")
    if args.unit_id is not None and not 1 <= args.unit_id <= 247:
        p.error("--unit-id muss zwischen 1 und 247 liegen")
    if len(args.serial) > 32 or not args.serial.isascii():
        p.error("--serial: hoechstens 32 Zeichen, nur ASCII")
    return args


async def main(argv=None):
    global MODBUS_PORT, MODBUS_HOST, UNIT_ID, SERIAL, NAME, TASMOTA_IP
    global JSON_PREFIX, FIELD_POWER, FIELD_IMPORT, FIELD_EXPORT, INVERT_SIGN
    args = parse_args(argv)
    MODBUS_PORT = args.port
    MODBUS_HOST = args.host
    UNIT_ID = args.unit_id
    SERIAL = args.serial
    NAME = args.name
    TASMOTA_IP = args.tasmota
    JSON_PREFIX = args.json_prefix
    FIELD_POWER = args.field_power
    FIELD_IMPORT = args.field_import or None
    FIELD_EXPORT = args.field_export or None
    INVERT_SIGN = args.invert

    log("=== Fronius GEN24 Smart Meter Emulator ===")
    if args.sim:
        sim["values"] = args.sim
        werte = ", ".join(f"{w:.0f} W" for w in args.sim)
        log(f"SIMULATIONSMODUS: {werte}"
              + (f" (Wechsel alle {args.sim_interval:g} s)" if len(args.sim) > 1 else ""))
    else:
        log(f"Tasmota: {TASMOTA_IP}")
    init_static_registers()
    update_meter_registers()
    try:
        server = await asyncio.start_server(handle_client, MODBUS_HOST, MODBUS_PORT)
    except PermissionError:
        log(f"[Fehler] Port {MODBUS_PORT} nicht erlaubt (Admin/root noetig).")
        return 1
    except OSError as exc:
        log(f"[Fehler] {MODBUS_HOST}:{MODBUS_PORT} nicht verfuegbar "
            f"(laeuft dort schon eine Instanz?): {exc}")
        return 1
    unit = "beliebig" if UNIT_ID is None else UNIT_ID
    log(f"Modbus TCP auf {MODBUS_HOST}:{MODBUS_PORT}, Unit-ID {unit}, SN {SERIAL}")
    tasks = [asyncio.create_task(server.serve_forever())]
    if args.sim:
        tasks.append(asyncio.create_task(simulate(args.sim_interval)))
        tasks.append(asyncio.create_task(read_console()))
    else:
        tasks.append(asyncio.create_task(poll_tasmota()))
    async with server:
        # Endet bei Fehler oder 'q' in der Konsole (KeyboardInterrupt)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            if t.exception():
                raise t.exception()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        log("Beendet.")
