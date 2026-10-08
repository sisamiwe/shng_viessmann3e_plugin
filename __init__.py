#!/usr/bin/env python3
# vim: set encoding=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab
#########################################################################
#  Copyright 2026-      Michael Wenzel                              
#########################################################################
#  This file is part of SmartHomeNG.
#  https://www.smarthomeNG.de
#  https://knx-user-forum.de/forum/supportforen/smarthome-py
#
#  SmartHomeNG is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  SmartHomeNG is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with SmartHomeNG. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

"""Open3E Plugin für SmartHomeNG.

Ermöglicht die Anbindung von Heizsystemen und Steuerungen via Open3E über den
CAN-Bus (SocketCAN/can0). Das Plugin unterstützt sowohl das zyklische Auslesen
von Datenbezeichnern (DIDs) als auch das Schreiben von Werten auf Befehl.
"""

# TODO: Wenn Sensor nicht verfügbar, dann Item auf None setzen (statt Wert zu behalten) Lese DID 3015: Ergebnis = ({'Actual': 0.0, 'Minimum': 0.0, 'Maximum': 0.0, 'Average': 0.0, 'SensorStatus': {'ID': 4, 'Text': 'not_available'}}, 'HeatSinkTemperatureSensor', 3015)
# TODO: Sensorstatus mit auswerten: DID 1772 (CompressorOilTemperatureSensor) = {'Actual': 20.2, 'Minimum': 0.0, 'Maximum': 0.0, 'Average': 0.0, 'SensorStatus': {'ID': 0, 'Text': 'no_error'}}

from __future__ import annotations

import sys
import os
import time
import datetime
import json
import importlib.util
from typing import Any, Dict, List, Tuple, Callable, Optional, Union
from pathlib import Path
from collections import defaultdict

from .open3e_client import HAS_OPEN3E, Open3EClient
from .open3e_scanner import Open3EScanner

if __name__ == '__main__':
    class SmartPlugin():
        """Mockup-Klasse für SmartPlugin im Standalone-Ausführungsmodus."""
        pass

    class SmartPluginWebIf():
        """Mockup-Klasse für SmartPluginWebIf im Standalone-Ausführungsmodus."""
        pass

    BASE = os.path.sep.join(os.path.realpath(__file__).split(os.path.sep)[:-3])
    sys.path.insert(0, BASE)

else:
    from lib.model.smartplugin import SmartPlugin
    from .webif import WebInterface


"""Standarddateiname der Geräte-Konfigurationsdatei."""
DEFAULT_CONFIG_FILE: str = "devices.json"
DEFAULT_CONFIG_SUB_PATH: str = 'config'


class Open3E(SmartPlugin):
    """Hauptklasse des Open3E SmartHomeNG Plugins.

    Handhabt die Verbindung zum CAN-Bus, verwaltet die Endgeräte und verbindet
    SmartHomeNG-Items mit den Datenbezeichnern (DIDs) der Open3E-Schnittstelle.

    Attributes:
        PLUGIN_VERSION (str): Version des Plugins.
        ALLOW_MULTIINSTANCE (bool): Flag, ob Mehrfachinstanzen erlaubt sind.
    """

    PLUGIN_VERSION = '0.0.5'
    ALLOW_MULTIINSTANCE = False
    
    DEFAULT_SCAN_START_COB: int = 0x680
    DEFAULT_SCAN_LAST_COB: int = 0x6EF
    DEFAULT_SCAN_START_DID: int = 256
    DEFAULT_SCAN_LAST_DID: int = 4000

    def __init__(self, sh=None, *args, standalone: str = '', logger=None, **kwargs) -> None:
        """Initialisiert die Open3E-Plugin-Instanz.

        Args:
            sh: Instanz des SmartHomeNG-Kernobjekts (nur im SHNG-Betrieb).
            *args: Variable Positionsargumente.
            standalone (str): CAN-Port für den Standalone-Modus (z. B. 'can0').
            logger: Logger-Instanz für Standalone-Modus.
            **kwargs: Variable Schlüsselwortargumente.
        """
        super().__init__(*args, **kwargs)

        # Mode configuration & logging initialization
        self._standalone = bool(standalone)
        if self._standalone:
            self.canport = standalone
            self.logger = logger
            self.default_read_cycle = 60
            self._pause_item_path = ''
        else:
            self.canport = self.get_parameter_value('can_port')
            self.default_read_cycle = self.get_parameter_value('read_cycle')
            self._pause_item_path = self.get_parameter_value('pause_item')
            self.read_all_at_init = bool(self.get_parameter_value('read_all_at_init'))
            self.init_webinterface(WebInterface)

        # Dependency & configuration validation
        if not HAS_OPEN3E:
            self.logger.error("open3e-Bibliothek nicht verfuegbar. Plugin wird nicht gestartet.")
            self._init_complete = False

        self.devices: Dict[str, Dict[str, Any]] = self.load_devices(DEFAULT_CONFIG_FILE)
        if not self.devices:
            self.logger.warning("Keine Geräte konfiguriert.")
            self._init_complete = False

        # State initialization
        self._pause_item = None
        self.clients: Dict[int, Dict[str, Any]] = {}
        self.ecu_dids: Dict = {}
        self._update_active: bool = False
        self._initial_item_read_done: bool = False
        self._filter_cache: Optional[List] = None
        self._filter_cache_key: str = ''
        self._filter_cache_value: Any = None
        self._filter_cache_op: str = ''

    # =========================================================================
    # PLUGIN CORE
    # =========================================================================

    def run(self) -> None:
        """Startet das Plugin, lädt die Konfiguration und baut Verbindungen auf.

        Liest die Konfigurationsdatei ein, verbindet mit den CAN-Geräten,
        initialisiert den zyklischen Abfrage-Scheduler und setzt das Alive-Flag.
        """
        self.logger.dbghigh(self.translate("Methode '{method}' aufgerufen", {'method': 'run()'}))

        # Verbindung aufbauen
        self.connect_to_devices()

        # Scheduler für zyklische Abfragen erstellen
        self.setup_scheduler()

        # Plugin alive setzen
        self.alive = True

        if self._pause_item:
            self._pause_item(False, self.get_fullname())

        self.poll_data()  # Initiale Abfrage der konfigurierten DIDs

    def stop(self) -> None:
        """Stoppt das Plugin geordnet.

        Trennt alle CAN-Verbindungen, stoppt den Scheduler und beendet den Laufzeit-Status.
        """
        self.logger.dbghigh(self.translate("Methode '{method}' aufgerufen", {'method': 'stop()'}))
        self.alive = False

        if self._pause_item:
            self._pause_item(True, self.get_fullname())

        self.disconnect_from_devices()
        self.scheduler_remove_all()

    def parse_item(self, item) -> Optional[Callable]:
        """Analysiert Item-Attribute beim Start von SmartHomeNG.

        Liest die Item-Attribute `open3e_read_cycle`, `open3e_read_at_init`,
        `open3e_write`, `open3e_read_afterwrite`, `open3e_ecu` etc. aus
        und registriert das Item (unterstützt kombiniertes Lesen und Schreiben).

        Args:
            item: Das zu analysierende SmartHomeNG Item-Objekt.

        Returns:
            Optional[Callable]: Die Update-Methode des Plugins (`self.update_item`), falls
            Schreibfunktionen oder das Pause-Item aktiv sind, andernfalls `None`.
        """
        # 1. Sonderfall: Pause-Item
        if item.property.path == self._pause_item_path:
            self.logger.debug(f"Pause item '{item.property.path}' registriert")
            self._pause_item = item
            self.add_item(item, updating=True)
            return self.update_item

        # Sonderfall: Update All Trigger Item
        open3e_update_all = self.get_iattr_value(item.conf, 'open3e_update_all')
        if open3e_update_all:
            self.logger.debug(f"parse_item: open3e_update_all auf {item.property.path}")
            self.add_item(item, updating=True)
            return self.update_item
        
        # 2. Attribute auslesen
        open3e_read_at_init = bool(self.get_iattr_value(item.conf, 'open3e_read_at_init'))
        open3e_read_cycle = int(self.get_iattr_value(item.conf, 'open3e_read_cycle') or 0)
        open3e_write = bool(self.get_iattr_value(item.conf, 'open3e_write'))
        open3e_read_after_write = int(self.get_iattr_value(item.conf, 'open3e_read_after_write') or 0)

        # Abbruch, wenn weder Lese- noch Schreibregel aktiv sind
        if not (open3e_read_at_init or open3e_read_cycle > 0 or open3e_write):
            return None

        open3e_read_at_init = open3e_read_at_init or self.read_all_at_init

        # 3. ECU & DID verarbeiten
        raw_ecu = self.get_iattr_value(item.conf, 'open3e_ecu')
        item_path = item.property.path 

        # ECU als Integer parsen (oder Fallback verwenden)
        try:
            ecu = int(raw_ecu, 0) if raw_ecu is not None else self.DEFAULT_SCAN_START_COB
        except (ValueError, TypeError):
            self.logger.warning(
                f"ECU '{raw_ecu}' definiert in {item_path} hat ein ungültiges Format. Item wird übersprungen."
            )
            return None

        # Prüfe, ob ECU unterstützt wird
        if ecu not in self.devices:
            self.logger.warning(
                f"ECU {ecu} definiert in {item_path} ist nicht verfügbar. Item wird übersprungen."
            )
            return None

        # DID auflösen
        raw_did = self.get_iattr_value(item.conf, 'open3e_did')
        did, sub_path = self._resolve_did(raw_did)

        if did is None:
            if raw_did is not None:
                self.logger.warning(
                    f"DID '{raw_did}' definiert in {item_path} ist ungültig. Item wird übersprungen."
                )
            return None

        # Prüfe, ob DID von der ECU unterstützt wird (Safeguard via .get())
        ecu_dids = self.devices[ecu].get('dids', {})
        if did not in ecu_dids:
            self.logger.warning(
                f"DID {did} definiert in {item_path} wird von ECU {ecu} nicht unterstützt. Item wird übersprungen."
            )
            return None

        # 4. Kombinierte Konfiguration erstellen
        is_read_active = open3e_read_at_init or open3e_read_cycle > 0
        nexttime = 0.0 if open3e_read_at_init else (time.time() + open3e_read_cycle if open3e_read_cycle > 0 else 0.0)

        item_config = {
            'ecu': ecu,
            'did': did,
            'sub_path': sub_path,
            'read': is_read_active,
            'read_cycle': open3e_read_cycle,
            'read_init': open3e_read_at_init,
            'nexttime': nexttime,
            'write': open3e_write,
            'read_after_write': open3e_read_after_write
        }

        self.logger.debug(f"parse_item [READ={is_read_active}, WRITE={open3e_write}]: {item.property.path} -> {item_config}")

        # 5. Item EINMALIG registrieren
        # updating=True registriert den Change-Listener in SmartHomeNG, falls geschrieben werden kann
        # Use tuple mapping (ecu, did) for O(1) lookup in update handler
        self.add_item(item, mapping=(ecu, did), config_data_dict=item_config, updating=open3e_write)

        # Wenn open3e_write aktiv ist, muss update_item als Listener zurückgegeben werden
        return self.update_item if open3e_write else None

    def parse_logic(self, logic) -> None:
        """Verarbeitet Logiken (vom Plugin-Framework vorgegeben).

        Args:
            logic: Das SmartHomeNG Logic-Objekt.
        """
        pass

    def update_item(self, item, caller=None, source=None, dest=None) -> None:
        """Callback wenn ein Item extern verändert wurde (z. B. durch KNX oder Logik).

        Args:
            item: Das geänderte SmartHomeNG Item.
            caller (str, optional): Aufrufer der Änderung.
            source (str, optional): Quelle der Änderung.
            dest (str, optional): Ziel der Änderung.
        """
        if item is self._pause_item:
            if caller != self.get_fullname():
                self.logger.debug(f'pause item changed to {item()}')
                if item() and self.alive:
                    self.stop()
                elif not item() and not self.alive:
                    self.run()
            return

        if self.alive and caller != self.get_fullname():
            self.logger.info(f"update_item: '{item.property.path}' has been changed outside this plugin by caller '{self.callerinfo(caller, source)}'")

            # read all dids
            open3e_update_all = self.get_iattr_value(item.conf, 'open3e_update_all')
            if open3e_update_all:
                self.logger.info(f"Update all Items called")
                self.poll_all_data()
                item(False, caller=self.get_fullname())

            # write to did
            open3e_write = self.get_iattr_value(item.conf, 'open3e_write')
            if open3e_write:
                self.on_item_written(item, caller=caller, source=source, dest=dest)

    # =========================================================================
    # ITEM VERARBEITUNG & CALLBACKS
    # =========================================================================

    def on_item_written(self, item, caller=None, source=None, dest=None) -> None:
        """Verarbeitet Änderungen an Schreib-Items und überträgt Werte via CAN-Bus.

        Schreibt den Wert auf die entsprechende ECU und plant bei Erfolg
        optional ein asynchrones Nachlesen (read_after_write) im Scheduler ein.

        Args:
            item: Das geänderte Item-Objekt.
            caller (str, optional): Aufrufer der Änderung.
            source (str, optional): Quelle der Änderung.
            dest (str, optional): Ziel der Änderung.
        """
        if caller == self.get_fullname():
            return

        item_config = self.get_item_config(item)
        if not item_config.get("write", False):
            return

        ecu = item_config.get("ecu")
        write_did = item_config.get("did")
        sub_path = item_config.get("sub_path")
        read_after_write = item_config.get("read_after_write", 0)
        new_val = item()

        self.logger.info(f"on_item_written: Item {item.property.path} -> Schreibe DID {write_did} an Geraet {ecu}: Wert = {new_val}")

        target_client = self._get_client(ecu)
        if target_client is None:
            self.logger.error(f"on_item_written: Kein aktiver Client fuer Geraet '{ecu}' gefunden.")
            return

        # 2. Schreiben & asynchrones Nachlesen
        try:
            target_client.write_did(did=write_did, value=new_val, sub=sub_path)
            self.logger.info(f"on_item_written: DID {write_did} erfolgreich geschrieben.")

            if read_after_write > 0:
                self.logger.info(f"on_item_written: Lese DID {write_did} in {read_after_write}s nach (Scheduler).")
                job_name = f"{self.get_fullname()}: read_after_write_{ecu}_{write_did}"
                if self.scheduler_get(job_name):
                    self.scheduler_remove(job_name)
                
                # Exakte Ausführungszeit als datetime berechnen
                next_time = self.shtime.now() + datetime.timedelta(seconds=read_after_write)

                self.scheduler_add(
                    name=job_name,
                    obj=self._schedule_readback,
                    value={'client': target_client, 'did': write_did},
                    next=next_time
                )

        except Exception as exc:
            self.logger.error(f"on_item_written: Fehler beim Schreiben von DID {write_did}: {exc}")

    def _schedule_readback(self, client, did: int) -> None:
        """Callback fuer den Scheduler zum verzögerten Nachlesen einer DID."""
        try:
            self.logger.info(f"Führe verzögertes Nachlesen für DID {did} aus...")
            client.read_did(did)
        except Exception as exc:
            self.logger.error(f"Fehler beim verzögerten Nachlesen von DID {did}: {exc}")

    # =========================================================================
    # DATENABFRAGE & SCHEDULER
    # =========================================================================

    def poll_data(self) -> None:
        """Zyklische Task zur Abfrage fälliger DIDs von den CAN-Geräten.

        Iteriert durch das todo-Dict und fragt die DIDs bei den registrierten
        Clients ab.
        """

        todo = self.build_read_plan()
        read_dids = sum(len(dids) for dids in todo.values())

        if read_dids == 0:
            self.logger.info("Keine Items zum Lesen fällig.")
            return

        if self._update_active:
            self.logger.warning('Triggered cyclic command read, but previous cyclic run is still active.')
            return

        self.logger.info(f"Poll data for {read_dids} DIDs from {len(todo)} devices")
        self._update_active = True

        try:
            for dev_addr, did in todo.items():
                if not did:
                    continue

                client_entry = self.clients.get(dev_addr)
                if client_entry is None:
                    addr_str = hex(dev_addr) if isinstance(dev_addr, int) else dev_addr
                    self.logger.warning(f"Kein aktiver Client fuer Adresse {addr_str} gefunden!")
                    continue

                self.logger.info(f"Lese fuer {client_entry['name']} ({len(did)} DIDs)...")
                client_entry["client"].read_dids(did)
        finally:
            self._update_active = False

    def setup_scheduler(self) -> None:
        """Erstellt oder aktualisiert den SHNG-Scheduler für die Abfrageintervalle.

        Berechnet das kürzeste Lese-Intervall über alle konfigurierten Items und
        richtet einen passenden Scheduler-Task ein (Intervall = kürzester Zyklus / 2).
        """
        cycles = [self.get_item_config(item).get('read_cycle', 0)
                   for item in self._cyclic_items()]
        shortestcycle = min((c for c in cycles if c > 0), default=None)

        if shortestcycle is not None:
            workercycle = max(1, int(shortestcycle / 2))
            if self.scheduler_get('cyclic'):
                self.scheduler_remove('cyclic')
            self.scheduler_add('cyclic', self.poll_data, cycle=workercycle, prio=5, offset=0)
            self.logger.info(f'Added cyclic worker thread ({workercycle} sec cycle). Shortest item cycle: {shortestcycle} sec')

    def build_read_plan(self) -> Dict[int, List[int]]:
        """Erstellt eine Liste von DIDs, die fuer jedes Gerat gelesen werden sollen.

        Returns:
            Dict[int, List[int]]: Dictionary mit Geräteadressen als Schlüssel und
            Listen von eindeutigen DIDs als Werte.
        """
        to_reads = defaultdict(set)

        currenttime = time.time()
        get_config = self.get_item_config

        is_init = not self._initial_item_read_done
        items = self._init_items() if is_init else self._cyclic_items()

        if self.logger.isEnabledFor(10):
            self.logger.debug("build_read_plan: is_init=%s, items=%s", is_init, [item.property.path for item in items])

        for item in items:
            config = get_config(item)
            if not config:
                continue

            ecu = config.get('ecu')
            did = config.get('did')
            read_cycle = config.get('read_cycle', 0)

            # Überprüfung für nicht-initialen Lauf
            if not is_init:
                nexttime = config.get('nexttime', 0)
                if nexttime > currenttime or read_cycle == 0:
                    continue

            if ecu is not None and did is not None:
                config['nexttime'] = currenttime + read_cycle
                to_reads[ecu].add(did)

        if is_init:
            self._initial_item_read_done = True

        if self.logger.isEnabledFor(10):
            self.logger.debug("Following DIDs scheduled for reading: %s", {ecu: list(dids) for ecu, dids in to_reads.items()})

        return {ecu: list(dids) for ecu, dids in to_reads.items()}

    def poll_all_data(self, ecu: Optional[int] = None) -> None:
        """Liest alle DIDs von allen oder einer spezifischen ECU."""
        if ecu is not None:
            target_ecus = [self.clients[ecu]] if ecu in self.clients else []
        else:
            target_ecus = list(self.clients.values())

        for ecu_data in target_ecus:
            client = ecu_data.get('client')
            if client:
                try:
                    client.read_all_dids()
                except Exception as e:
                    self.logger.error(f"Fehler beim Pollen von ECU {ecu_data}: {e}")

    # =========================================================================
    # ITEM FILTER & ABFRAGE-HELPERS
    # =========================================================================

    def _filter_items(self, filter_key: str = '', filter_value: Any = None, op: str = '==') -> List[Any]:
        """Gibt eine Liste registrierter SmartHomeNG Items zurueck, gefiltert nach Config-Key und Wert.

        Args:
            filter_key: Schluessel im 'config_data' Dict (z.B. 'read_cycle').
            filter_value: Zielwert fuer den Vergleich.
            op: Vergleichsoperator: '==', '>', '>=', '<', '<=', 'start', 'end', 'in'.

        Returns:
            Liste der passenden Item-Objekte.
        """
        if not filter_key or filter_value is None:
            return [entry['item'] for entry in self._plg_item_dict.values()]

        cache_key = (filter_key, filter_value, op)
        if cache_key == (self._filter_cache_key, self._filter_cache_value, self._filter_cache_op):
            return self._filter_cache or []

        matching_items = []

        for entry in self._plg_item_dict.values():
            config_data = entry.get('config_data', {})
            if filter_key not in config_data:
                continue

            raw_val = config_data[filter_key]

            # ----------------------------------------------------------------------
            # 1. Numerische Vergleiche (z.B. read_cycle > 0)
            # ----------------------------------------------------------------------
            if op in ('>', '>=', '<', '<='):
                # Versuchen, den Wert sicher in int/float zu wandeln (unter Ausschluss von Bools)
                num_val = None
                if isinstance(raw_val, (int, float)) and not isinstance(raw_val, bool):
                    num_val = raw_val
                elif isinstance(raw_val, str):
                    try:
                        num_val = float(raw_val) if '.' in raw_val else int(raw_val)
                    except ValueError:
                        pass

                if num_val is not None and isinstance(filter_value, (int, float)):
                    if op == '>' and num_val > filter_value:
                        matching_items.append(entry['item'])
                    elif op == '>=' and num_val >= filter_value:
                        matching_items.append(entry['item'])
                    elif op == '<' and num_val < filter_value:
                        matching_items.append(entry['item'])
                    elif op == '<=' and num_val <= filter_value:
                        matching_items.append(entry['item'])

            # ----------------------------------------------------------------------
            # 2. String-Muster-Vergleiche ('start', 'end', 'in')
            # ----------------------------------------------------------------------
            elif op in ('start', 'end', 'in'):
                val_str = str(raw_val)
                search_str = str(filter_value)
                
                # Falls deine Plugin-Klasse self._string_compare nutzt:
                if hasattr(self, '_string_compare') and op in ('start', 'end'):
                    if self._string_compare(val_str, search_str, op):
                        matching_items.append(entry['item'])
                else:
                    if op == 'start' and val_str.startswith(search_str):
                        matching_items.append(entry['item'])
                    elif op == 'end' and val_str.endswith(search_str):
                        matching_items.append(entry['item'])
                    elif op == 'in' and search_str in val_str:
                        matching_items.append(entry['item'])

            # ----------------------------------------------------------------------
            # 3. Exakter Vergleich ('==')
            # ----------------------------------------------------------------------
            else:
                if raw_val == filter_value:
                    matching_items.append(entry['item'])

        self._filter_cache = matching_items
        self._filter_cache_key = filter_key
        self._filter_cache_value = filter_value
        self._filter_cache_op = op
        return matching_items

    def _cyclic_items(self) -> list:
        """Gibt alle Items zurueck, die fuer die zyklische Abfrage konfiguriert sind."""
        return self._filter_items(filter_key="read_cycle", filter_value=0, op='>')

    def _init_items(self) -> list:
        """Gibt alle Items zurueck, die bei Start initiert werden sollen."""
        return self._filter_items(filter_key="read_init", filter_value=True)

    # =========================================================================
    # KONFIGURATION & DATEIEN
    # =========================================================================

    def load_datapoints(self, file_name: str, base_dir: Path) -> Dict[str, Any] | None:
        """Laedt eine Python-Datenpunktliste (DP-Liste) dynamisch als Modul ein.

        Args:
            file_name: Name der Py-Datei (z.B. 'E3_2050.py').
            base_dir: Basispfad, in dem sich die Datei befindet.

        Returns:
            Dict[str, Any] | None: Das dataIdentifiers-Dict oder None bei Fehlern.
        """
        file_path = base_dir / file_name
        if not file_path.exists():
            self.logger.warning(f"Datei '{file_path}' nicht gefunden")
            return None

        try:
            module_name = file_path.stem
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            self.logger.error(f"Laden von '{file_name}' fehlgeschlagen: {exc}")
            return None

        if not hasattr(module, "dataIdentifiers"):
            self.logger.warning(f"'dataIdentifiers' in '{file_name}' nicht gefunden")
            return None
        return module.dataIdentifiers

    def load_devices(self, json_file: str = DEFAULT_CONFIG_FILE) -> Dict[str, Dict[str, Any]]:
        """Liest die zentrale Geraetekonfiguration (JSON) und verknüpfte DP-Dateien.

        Args:
            json_file: Dateiname der Geraete-JSON (Standard: 'devices.json').

        Returns:
            Dict[str, Dict[str, Any]]: Aufbereitetes Woerterbuch mit Geraete-Informationen und DIDs.
        """
        config_dir = Path(__file__).resolve().parent / DEFAULT_CONFIG_SUB_PATH
        json_path = config_dir / json_file

        if not json_path.exists():
            self.logger.error(f"Konfigurationsdatei '{json_file}' nicht gefunden")
            return {}

        with open(json_path, "r", encoding="utf-8") as fh:
            devices_data = json.load(fh)

        devices: Dict[str, Dict[str, Any]] = {}
        self.logger.info("--- Lade Gerätekonfiguration ---")
        for dev_id, dev_info in devices_data.items():
            dp_file = dev_info.get("dpList")
            prop = dev_info.get("prop")
            tx = dev_info.get("tx")

            dp_data = self.load_datapoints(dp_file, config_dir)
            if not dp_data:
                continue

            # TODO: dev_id in int wandeln
            dids = dp_data.get("dids", {})
            devices[int(dev_id, 16)] = {
                "tx": tx,
                "prop": prop,
                "name": dp_data.get("name", prop),
                "dp_file": dp_file,
                "dp_path": str(config_dir / dp_file),
                "dids": dids,
            }
            self.logger.info(f"  + {dev_id} ({prop}): {len(dids)} DIDs aus {dp_file}")
        self.logger.info("--------------------------------")
        return devices
   
    # =========================================================================
    # GERÄTEVERBINDUNG & DATENHANDLER
    # =========================================================================

    def make_update_handler(self, dev_name: str, dev_addr: int) -> Callable[[int, str, Any], None]:
        """Erstellt eine Callback-Funktion fuer Open3E-Updates.
        
        Uses tuple mapping (dev_addr, did_id) for item lookup.
        """
        
        def handler(did_id: int, did_name: str, value: Any) -> None:
            self.logger.debug(f"[{dev_name}] DID {did_id} ({did_name}) = {value!r}")

            # O(1) lookup - returns only items for this specific (ECU, DID) combo
            did_items = self.get_items_for_mapping(mapping=(dev_addr, did_id))
            
            for item in did_items:
                config = self.get_item_config(item)
                sub_path = config.get("sub_path")

                self.logger.debug(f"update_handler: Item {item.property.path} matched for DID {did_id}")

                if sub_path is not None and value is not None:
                    item_val = self._resolve_path(value, sub_path)
                else:
                    item_val = value

                if item_val is not None:
                    item(item_val, caller=self.get_fullname())

        return handler

    def connect_to_devices(self) -> None:
        """Instanziiert für jedes konfigurierte Gerät einen `Open3EClient` und verbindet ihn.

        Fügt die erfolgreich verbundenen Clients zum Dictionary `self.clients` hinzu.
        """
        for dev_key, dev_info in self.devices.items():
            dev_name = dev_info.get("name", dev_key)
            try:
                tx_addr = int(dev_info["tx"], 16)
                dp_file = dev_info["dp_file"]
                dp_full_path = dev_info["dp_path"]

                self.logger.info(f"Erstelle Client für Gerät '{dev_name}' (tx={hex(tx_addr)}, dp_file={dp_file})...")
                client = Open3EClient(bus=self.canport, devtype=dp_full_path, e3_address=tx_addr, logger=self.logger)

                self.logger.debug(f"Erstelle Callback für '{dev_name}'...")
                client.add_callback(self.make_update_handler(dev_name, tx_addr))

                self.logger.debug("Verbinde...")
                client.connect()

                self.clients[tx_addr] = {
                    "name": dev_name,
                    "client": client,
                    "info": dev_info
                }
                self.logger.info(f"Erfolgreich mit '{dev_name}' (Adresse {hex(tx_addr)}) verbunden.")

            except Exception as e:
                self.logger.error(f"Fehler beim Verbinden mit Gerät '{dev_name}': {e}")

    def disconnect_from_devices(self) -> None:
        """Trennt geordnet alle aktiven Open3E-Client-Verbindungen."""
        if not self.clients:
            self.logger.warning("Keine aktiven Verbindungen zum Trennen gefunden.")
            return

        for dev_addr, client_entry in self.clients.items():
            dev_name = client_entry["name"]
            client = client_entry["client"]
            try:
                client.disconnect()
                self.logger.info(f"Verbindung zu Gerät '{dev_name}' getrennt.")
            except Exception as e:
                self.logger.error(f"Fehler beim Trennen von '{dev_name}': {e}")
        
        self.clients.clear()

    # =========================================================================
    # HILFSMETHODEN
    # =========================================================================

    def _get_client(self, ecu: Any) -> Optional[Open3EClient]:
        """Gibt den Open3E-Client fuer eine ECU-Adresse oder einen Geratenamen zurueck."""
        if isinstance(ecu, int) and ecu in self.clients:
            return self.clients[ecu]["client"]

        for entry in self.clients.values():
            if entry["name"] == ecu:
                return entry["client"]

        return None

    def _resolve_did(self, raw_did) -> Tuple[Optional[int], Optional[Union[str, int]]]:
        """Löst einen rohen DID-Wert in (did_int, sub_path) auf.

        Sub-Path kann str für den Key innerhalb des Dict sein oder int für die Position im Dict.

        Args:
            raw_did: Rohwert aus Item-Config (z.B. "318", "318.Actual" oder "318.0").

        Returns:
            Tuple aus DID-Integer und optionalem Sub-Pfad (str oder int). Bei Fehler (None, None).
        """
        if raw_did is None:
            return None, None

        did_str = str(raw_did).strip()
        did_part, raw_sub_path = did_str.split('.', 1) if '.' in did_str else (did_str, None)

        try:
            did_int = int(did_part)
        except (ValueError, TypeError):
            return None, None

        sub_path = None
        if raw_sub_path is not None:
            try:
                sub_path = int(raw_sub_path)
            except ValueError:
                sub_path = raw_sub_path

        return did_int, sub_path

    def _resolve_path(self, data: Any, path: Union[str, int]) -> Any:
        """Greift auf Daten via Schlüssel/Eigenschaft, verschachtelten Pfad ('BusType.Text')
        oder numerischen Index (bei dict, list, tuple) zu.
        """
        if path is None or data is None:
            return data

        # Fall 1: Zugriff über Integer-Index (Positionsbasiert)
        if isinstance(path, int):
            if isinstance(data, dict):
                # Iterate to index without creating full list
                for i, key in enumerate(data.keys()):
                    if i == path:
                        return data[key]
                return None
            elif isinstance(data, (list, tuple)):
                try:
                    return data[path]
                except IndexError:
                    return None
            return None

        # Fall 2: Zugriff über String-Pfad (Schlüssel / Attribute)
        keys = str(path).split('.')
        for key in keys:
            if isinstance(data, dict):
                # Prüfen, ob der Key evtl. eine Zahl als String ist (z.B. "0")
                if key.isdigit() and key not in data:
                    try:
                        idx = int(key)
                        # Iterate to index without creating full list
                        for i, dict_key in enumerate(data.keys()):
                            if i == idx:
                                data = data[dict_key]
                                break
                        else:
                            return None
                        continue
                    except (ValueError, IndexError):
                        return None
                data = data.get(key)
            elif hasattr(data, key):
                data = getattr(data, key)
            elif isinstance(data, (list, tuple)) and key.isdigit():
                try:
                    data = data[int(key)]
                except IndexError:
                    return None
            else:
                return None

            if data is None:
                return None

        return data

    # =========================================================================
    # SCANNER / ENTDECKUNG
    # =========================================================================

    def _get_scanner(self) -> Optional[Open3EScanner]:
        """Hilfsmethode zur Wiederverwendung / Lazy-Instanziierung des Scanners."""
        if getattr(self, "scanner", None) is None:
            try:
                self.scanner = Open3EScanner(bus=self.canport, logger=self.logger)
            except Exception as ex:
                self.logger.error(f"Initialisierung des Open3EScanner fehlgeschlagen: {ex}", exc_info=True)
                return None
        return self.scanner

    def scan_ecus(self, start_cob: int = DEFAULT_SCAN_START_COB, last_cob: int = DEFAULT_SCAN_LAST_COB, write_json: bool = True) -> List[Tuple[int, int, str]]:
        """Scannt nach allen aktiven ECUs auf dem Bus.

        Args:
            start_cob: Start-COB-ID für den Scan (Standard: 0x680).
            last_cob: End-COB-ID für den Scan (Standard: 0x6EF).
            write_json: Wenn True, wird eine devices.json geschrieben.

        Returns:
            Liste von Tupeln (cob_id, devprop_byte, devprop_name).
        """
        scanner = self._get_scanner()
        if not scanner:
            return []

        try:
            # Nutzt den übergebenen Adressbereich
            self.ecus = scanner.scan_ecus(start_cob=start_cob, last_cob=last_cob)
            if write_json and self.ecus:
                config_dir = Path(__file__).resolve().parent / DEFAULT_CONFIG_SUB_PATH
                scanner.write_devices_json(self.ecus, config_dir)
            return self.ecus
        except Exception as ex:
            self.logger.error(f"Fehler beim ECU-Scan: {ex}", exc_info=True)
            return []

    def scan_ecu_dids(
        self,
        ecu: int = DEFAULT_SCAN_START_COB,
        start_did: int = DEFAULT_SCAN_START_DID,
        last_did: int = DEFAULT_SCAN_LAST_DID,
        write_file: bool = True
    ) -> Dict[int, Any]:
        """Fragt alle unterstützten DIDs für eine spezifische ECU ab.

        Args:
            ecu: ECU-Adresse (COB-ID).
            start_did: Start-DID für den Scan (Standard: 256).
            last_did: End-DID für den Scan (Standard: 4000).
            write_file: Wenn True, wird eine Datapoints-Python-Datei geschrieben.

        Returns:
            Dictionary mit DID-IDs als Schlüssel und gelesenen Werten.
        """
        scanner = self._get_scanner()
        if not scanner:
            return {}

        try:
            results = scanner.scan_ecu_dids(cob_id=ecu, start_did=start_did, last_did=last_did)
            self.ecu_dids[ecu] = results
            if write_file and results:
                devprop = self._get_device_property_for_ecu(ecu)
                config_dir = Path(__file__).resolve().parent / DEFAULT_CONFIG_SUB_PATH
                scanner.write_datapoints_file(results, ecu, devprop, config_dir)
            return {did: raw for did, dlen, raw in results}
        except Exception as ex:
            self.logger.error(f"Fehler beim DID-Scan für ECU {hex(ecu)}: {ex}", exc_info=True)
            return {}

    def _get_device_property_for_ecu(self, cob_id: int) -> str:
        """Gibt die Geräte-Property für eine ECU zurück (aus gecachtem Scan)."""
        for cob, devprop_byte, devprop_name in getattr(self, 'ecus', []):
            if cob == cob_id:
                return devprop_name
        return 'unknown'


# =============================================================================
# STANDALONE MODE
# =============================================================================

if __name__ == '__main__':
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("Open3ETest")

    print("=== Open3E Plugin Standalone Test ===")

    plugin = Open3E(standalone="can0", logger=logger)
    plugin.run()

    try:
        if plugin.clients:
            logger.info("Warte 3 Sekunden vor der ersten Abfrage...")
            time.sleep(3)

            for dev_addr, client_entry in plugin.clients.items():
                dev_name = client_entry["name"]
                client = client_entry["client"]
                dev_info = client_entry["info"]

                logger.info(f"\n--- Abfrage von DID {DEFAULT_SCAN_START_DID} für {dev_name} (tx={hex(dev_addr)}) ---")

                dids_to_read = [DEFAULT_SCAN_START_DID]
                available_dids = dev_info.get("dids", {})
                valid_dids = [d for d in dids_to_read if d in available_dids]

                if valid_dids:
                    logger.info(f"Lese bekannte DIDs: {valid_dids}")
                    client.read_dids(valid_dids)
                else:
                    first_dids = list(available_dids.keys())[:3]
                    logger.info(f"DID nicht in Konfiguration. Lese erste verfügbare DIDs: {first_dids}")
                    client.read_dids(first_dids)

                logger.info("Warte auf Antworte-Callbacks (2 Sekunden)...")
                time.sleep(2)
        else:
            logger.warning("Keine aktiven Clients vorhanden.")

    except KeyboardInterrupt:
        logger.info("\nAbbruch durch Benutzer.")

    finally:
        logger.info("Trenne Verbindungen...")
        plugin.stop()
        print("=== Test beendet ===")