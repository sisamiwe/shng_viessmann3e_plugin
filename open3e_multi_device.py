"""
open3e_multi_device
===================

Beispielskript für den parallelen Betrieb mehrerer E3-Geräte über
einen gemeinsamen :class:`open3e_client.Open3EClient`.

Zweck
-----
Während ``open3e_client`` den Single-Device-Fall abdeckt, benötigen
reale Anlagen oft mehrere ECUs (z. B. ``Vcal`` für die Regelung
und ``Vdens`` für den hydraulischen Abgleich) auf demselben CAN-Bus.
Dieses Skript zeigt, wie mehrere Clients parallel initialisiert,
ihre Datenpunktlisten individuell geladen und die Ergebnisse
zusammengeführt werden.

Gerätekonfiguration
-------------------
Die angebundenen Geräte werden aus der Datei ``devices.json``
geladen, die pro Eintrag mindestens folgende Felder erwartet:

* ``prop``:    logischer Gerätename (z. B. ``"Vdens"``).
* ``tx``:      CAN-Transmitadresse als Hex-String (z. B. ``"0x680"``).
* ``dpList``:  Dateiname der gerätespezifischen
  ``Open3Edatapoints*.py``-Datei im Projektverzeichnis.

Beispiel ``devices.json``
-------------------------
.. code-block:: json

   {
       "device_0": {
           "prop": "Vcal",
           "tx":   "0x680",
           "dpList": "Open3Edatapoints_680.py"
       },
       "device_1": {
           "prop": "Vdens",
           "tx":   "0x684",
           "dpList": "Open3Edatapoints_684.py"
       }
   }

Ablauf
------
1. ``devices.json`` einlesen.
2. Pro Gerät einen :class:`open3e_client.Open3EClient` instanziieren
   und verbinden.
3. Beispiel-DIDs (``256``, ``268``) aus den jeweiligen DpLists lesen.
4. Gemeinsame Event-Loop starten.
5. Bei ``Ctrl+C`` alle Verbindungen trennen.

Aufruf
------
.. code-block:: bash

   $ python open3e_multi_device.py

Voraussetzungen
---------------
* Linux mit SocketCAN-Schnittstelle ``can0``.
* Geladenes ``can-isotp``-Kernelmodul.
* Installierte Abhängigkeiten aus ``requirements.txt``.

Siehe auch
----------
* :mod:`open3e_client` — Wrapper-Klasse.
* :mod:`open3e.Open3Eclass` — zugrundeliegende UDS-Implementierung.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from open3e_client import Open3EClient


PROJECT_ROOT: Path = Path(__file__).resolve().parent
"""Projektverzeichnis (Speicherort dieses Skripts)."""

DEFAULT_CONFIG: str = "devices.json"
"""Voreingestellter Name der Geräte-Konfigurationsdatei."""

DEMO_DIDS: Tuple[int, ...] = (256, 268)

DEMO_DIDS_NAME: Tuple[str, ...] = ("FlowTemperatureSensor", "BusIdentification")
"""
Demo-DIDs, die pro Gerät gelesen werden.

* ``256`` — Außentemperatur.
* ``268`` — Vorlauftemperatur.

Die Liste wird gegen ``dev_info["dids"]`` gefiltert, sodass nur
im jeweiligen Gerät vorhandene DIDs tatsächlich angefragt werden.
"""

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_dp_file(file_name: str, base_dir: Path) -> Dict[str, Any] | None:
    """
    Lädt eine ``Open3Edatapoints*.py``-Datei dynamisch.

    Die Datei muss ein Dictionary ``dataIdentifiers`` exportieren
    (siehe open3e-Quellen).

    Parameters
    ----------
    file_name:
        Dateiname relativ zu ``base_dir``.
    base_dir:
        Basisverzeichnis für die Suche.

    Returns
    -------
    dict | None
        Inhalt von ``dataIdentifiers`` oder ``None`` bei Fehlern.
    """
    file_path = base_dir / file_name
    if not file_path.exists():
        print(f"[Warnung] Datei '{file_path}' wurde nicht gefunden.")
        return None

    try:
        module_name = file_path.stem
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        print(f"[Fehler] Laden von '{file_name}' fehlgeschlagen: {exc}")
        return None

    if not hasattr(module, "dataIdentifiers"):
        print(f"[Warnung] 'dataIdentifiers' in '{file_name}' nicht gefunden.")
        return None
    return module.dataIdentifiers


def load_devices_config(json_filename: str = DEFAULT_CONFIG) -> Dict[str, Dict[str, Any]]:
    """
    Liest ``devices.json`` und lädt alle referenzierten DpList-Dateien.

    Parameters
    ----------
    json_filename:
        Name der JSON-Konfigurationsdatei im Projektverzeichnis.

    Returns
    -------
    dict
        Mapping ``dev_id -> {tx, prop, name, dp_file, dids}``.
    """
    json_path = PROJECT_ROOT / json_filename
    if not json_path.exists():
        print(f"[Fehler] Datei '{json_path}' nicht gefunden.")
        return {}

    with open(json_path, "r", encoding="utf-8") as fh:
        devices_data = json.load(fh)

    devices: Dict[str, Dict[str, Any]] = {}
    print("--- Lade Gerätekonfiguration ---")
    for dev_id, dev_info in devices_data.items():
        dp_file = dev_info.get("dpList")
        prop = dev_info.get("prop")
        tx = dev_info.get("tx")

        dp_data = load_dp_file(dp_file, PROJECT_ROOT)
        if not dp_data:
            continue

        dids = dp_data.get("dids", {})
        devices[dev_id] = {
            "tx": tx,
            "prop": prop,
            "name": dp_data.get("name", prop),
            "dp_file": dp_file,
            "dids": dids,
        }
        print(f"  + {dev_id} ({prop}): {len(dids)} DIDs aus {dp_file}")
    print("--------------------------------\n")
    return devices


def make_data_handler(dev_name: str):
    """
    Erzeugt einen Callback, der DID-Werte gerätespezifisch ausgibt.

    Parameters
    ----------
    dev_name:
        Anzeigename des Geräts (wird dem Präfix vorangestellt).

    Returns
    -------
    Callable
        Callback-Funktion passend zu
        :class:`open3e_client.Open3EClient`.
    """
    prefix = f"[{dev_name}] " if dev_name else ""

    def handler(did_id: int, did_name: str, value: Any) -> None:
        print(f"[Update] {prefix}DID {did_id} ({did_name}) -> Wert: {value!r}")
        # print(f"[Update]   Typ: {type(value).__name__}")
        print()

    return handler


def main() -> None:
    """Startet die Multi-Device-Demo (siehe Modul-Docstring).

    Die Funktion kehrt erst nach einem ``KeyboardInterrupt``
    (``Ctrl+C``) zurück. In diesem Fall werden alle offenen
    :class:`open3e_client.Open3EClient`-Instanzen sauber getrennt.
    """
    devices = load_devices_config(DEFAULT_CONFIG)
    if not devices:
        print("Keine Geräte konfiguriert. Abbruch.")
        return

    clients: List[Tuple[str, Open3EClient, Dict[str, Any]]] = []

    try:
        for dev_key, dev_info in devices.items():
            tx_addr = int(dev_info["tx"], 16)
            dev_name = dev_info["name"]
            dp_file = dev_info["dp_file"]

            client = Open3EClient(bus="can0", devtype=dp_file, e3_address=tx_addr)
            client.add_data_callback(make_data_handler(dev_name))
            client.connect()
            clients.append((dev_name, client, dev_info))

        print("Sende Leseanforderungen...")
        for dev_name, client, dev_info in clients:

            to_read = DEMO_DIDS
            print(f"Lese fuer {dev_name} ({len(to_read)} DIDs)...")
            client.read_dids(list(to_read))
            print()

            to_read_by_name = DEMO_DIDS_NAME
            print(f"Lese fuer {dev_name} ({len(to_read_by_name)} DID-Names)...")
            client.read_dids_by_name(list(to_read_by_name))

        print("\nListening... (Abbrechen mit Strg+C)")
        while True:
            for _dev_name, client, _info in clients:
                client.spin_once()
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nBeende...")
    finally:
        for _dev_name, client, _info in clients:
            try:
                client.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    main()