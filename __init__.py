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

from __future__ import annotations

import sys
import os
import time
import json
import importlib.util
from typing import Any, Dict, List, Tuple, Callable, Optional
from pathlib import Path
from collections import defaultdict

from .open3e_client import Open3EClient

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
    from lib.item import Items
    from lib.model.smartplugin import SmartPlugin
    from .webif import WebInterface


"""Standarddateiname der Geräte-Konfigurationsdatei."""
DEFAULT_CONFIG_FILE: str = "devices.json"
DEFAULT_CONFIG_SUB_PATH: str = 'config'

"""Demo Data Identifier (DIDs) für Standalone-Tests."""
DEMO_DIDS: Tuple[int, ...] = (256, 268)

"""Namen der Demo Data Identifier."""
DEMO_DIDS_NAME: Tuple[str, ...] = ("FlowTemperatureSensor", "BusIdentification")


class Open3E(SmartPlugin):
    """Hauptklasse des Open3E SmartHomeNG Plugins.

    Handhabt die Verbindung zum CAN-Bus, verwaltet die Endgeräte und verbindet
    SmartHomeNG-Items mit den Datenbezeichnern (DIDs) der Open3E-Schnittstelle.

    Attributes:
        PLUGIN_VERSION (str): Version des Plugins.
        ALLOW_MULTIINSTANCE (bool): Flag, ob Mehrfachinstanzen erlaubt sind.
    """

    PLUGIN_VERSION = '0.0.1'
    ALLOW_MULTIINSTANCE = False

    def __init__(self, sh=None, *args, standalone: str = '', logger=None, **kwargs) -> None:
        """Initialisiert die Open3E-Plugin-Instanz.

        Args:
            sh: Instanz des SmartHomeNG-Kernobjekts (nur im SHNG-Betrieb).
            *args: Variable Positionsargumente.
            standalone (str): CAN-Port für den Standalone-Modus (z. B. 'can0').
            logger: Logger-Instanz für Standalone-Modus.
            **kwargs: Variable Schlüsselwortargumente.
        """
        super().__init__()

        self._pause_item = None
        self.devices: Dict[str, Dict[str, Any]] = {}

        if standalone:
            self.canport = standalone
            self.logger = logger
            self._standalone = True
            self.default_read_cycle = 60
            self._pause_item_path = ''
        else:
            self._pause_item_path = self.get_parameter_value('pause_item')
            self.canport = self.get_parameter_value('can_port')
            self.default_read_cycle = self.get_parameter_value('read_cycle')
            self._standalone = False
            self.init_webinterface(WebInterface)

        self._update_active: bool = False
        self._init_done = False
        self.clients: Dict[int, Dict[str, Any]] = {}

    def run(self) -> None:
        """Startet das Plugin, lädt die Konfiguration und baut Verbindungen auf.

        Liest die Konfigurationsdatei ein, verbindet mit den CAN-Geräten,
        initialisiert den zyklischen Abfrage-Scheduler und setzt das Alive-Flag.
        """
        self.logger.dbghigh(self.translate("Methode '{method}' aufgerufen", {'method': 'run()'}))

        self.devices = self.load_devices_config(DEFAULT_CONFIG_FILE)
        if not self.devices:
            self.logger.warning("Keine Geräte konfiguriert.")

        # Verbindung aufbauen
        self.connect_to_devices()

        # Scheduler für zyklische Abfragen erstellen
        self.create_cyclic_scheduler()

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

        Liest die Item-Attribute `open3e_read_cycle`, `open3e_read_init`,
        `open3e_write`, `open3e_ecu` etc. aus und registriert das Item.

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

        # Sonstige Attribute
        open3e_update_all = self.get_iattr_value(item.conf, 'open3e_update_all')
        if open3e_update_all:
            self.logger.debug(f"parse_item: open3e_update_all auf {item.property.path}")
            self.add_item(item, updating=True)
            return self.update_item
        
        # Direct Types
        open3e_read_init = self.get_iattr_value(item.conf, 'open3e_read_init')
        open3e_read_cycle = self.get_iattr_value(item.conf, 'open3e_read_cycle') or 0
        open3e_write = self.get_iattr_value(item.conf, 'open3e_write')

        # Abbruch, wenn weder Lese- noch Schreibregel aktiv sind
        if not (open3e_read_init or open3e_read_cycle > 0 or open3e_write):
            return None

        # 2. Gemeinsame Attribute auflösen & konvertieren
        raw_ecu = self.get_iattr_value(item.conf, 'open3e_ecu')
        try:
            ecu = int(raw_ecu, 0) if raw_ecu is not None else None
        except (ValueError, TypeError):
            ecu = raw_ecu

        raw_did = self.get_iattr_value(item.conf, 'open3e_did')

        did = None
        sub_path = None

        if raw_did is not None:
            item_did_str = str(raw_did).strip()

            # Trennung bei Punkt (z.B. "318.Actual")
            if '.' in item_did_str:
                did_part, sub_path = item_did_str.split('.', 1)
            else:
                did_part = item_did_str

            # DID in Ganzzahl umwandeln, falls möglich
            try:
                did = int(did_part)
            except (ValueError, TypeError):
                did = did_part
                self.logger.warning(f"DID '{did_part}' definied in item {item.property.path} invalid. Item will be skipped")
                return

        needs_update_handler = False

        # 3. Lese-Konfiguration (Read)
        if open3e_read_init or open3e_read_cycle > 0:
            nexttime = 0 if open3e_read_init else time.time() + open3e_read_cycle

            read_config = {
                'ecu': ecu,
                'did': did,
                'sub_path': sub_path,
                'read': True,
                'read_cycle': open3e_read_cycle,
                'read_init': open3e_read_init,
                'nexttime': nexttime
            }
            self.logger.debug(f"parse_item [READ]: {item.property.path} -> {read_config}")
            self.add_item(item, mapping=did, config_data_dict=read_config, updating=False)

        # 4. Schreib-Konfiguration (Write)
        if open3e_write:
            read_after_write = bool(self.get_iattr_value(item.conf, 'open3e_read_after_write'))

            write_config = {
                'ecu': ecu,
                'did': did,
                'sub_path': sub_path,
                'write': True,
                'read_after_write': read_after_write
            }
            self.logger.debug(f"parse_item [WRITE]: {item.property.path} -> {write_config}")
            self.add_item(item, mapping=did, config_data_dict=write_config, updating=True)
            needs_update_handler = True

        return self.update_item if needs_update_handler else None

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
            if caller != self.get_shortname():
                self.logger.debug(f'pause item changed to {item()}')
                if item() and self.alive:
                    self.stop()
                elif not item() and not self.alive:
                    self.run()
            return

        if self.alive and caller != self.get_fullname():
            self.logger.info(
                f"update_item: '{item.property.path}' has been changed outside this plugin "
                f"by caller '{self.callerinfo(caller, source)}'"
            )

            open3e_update_all = self.get_iattr_value(item.conf, 'open3e_update_all')
            if open3e_update_all:
                self.logger.info(f"Update all Items called")
                self.poll_all_data()
                item(False, caller=self.get_fullname())

    def on_item_change(self, item, caller=None, source=None, dest=None) -> None:
        """Verarbeitet Änderungen an Schreib-Items und überträgt Werte via CAN-Bus.

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
        read_after_write = item_config.get("read_after_write", 5)
        new_val = item()

        self.logger.info(f"on_item_change: Item {item.property.path} -> Schreibe DID {write_did} an Geraet {ecu}: Wert = {new_val}")

        # Direkt-Lookup im Clients-Dict über Adresse/Name
        target_client = None
        if isinstance(ecu, int) and ecu in self.clients:
            target_client = self.clients[ecu]["client"]
        else:
            # Fallback falls ecu als String/Name im Item konfiguriert wurde
            for client_entry in self.clients.values():
                if client_entry["name"] == ecu:
                    target_client = client_entry["client"]
                    break

        if target_client is None:
            self.logger.error(f"on_item_change: Kein aktiver Client fuer Geraet '{ecu}' gefunden.")
            return

        try:
            target_client.write_did(write_did, new_val)
            self.logger.info(f"on_item_change: DID {write_did} erfolgreich geschrieben.")
            if read_after_write:
                self.logger.info(f"on_item_change: Lese DID {write_did} nach dem Schreiben erneut ein.")
                target_client.read_dids([write_did])
        except Exception as exc:
            self.logger.error(f"on_item_change: Fehler beim Schreiben von DID {write_did}: {exc}")

    def poll_data(self) -> None:
        """Zyklische Task zur Abfrage fälliger DIDs von den CAN-Geräten.

        Iteriert durch das todo-Dict und fragt die DIDs bei den registrierten
        Clients ab.
        """

        todo = self.create_to_reads()
        read_items = sum(len(dids) for dids in todo.values())

        if read_items == 0:
            self.logger.info("Keine Items zum Lesen fällig.")
            return

        if self._update_active:
            self.logger.warning('Triggered cyclic command read, but previous cyclic run is still active.')
            return

        self.logger.info(f"Poll data for {read_items} DIDs from {len(todo)} devices")
        self._update_active = True

        try:
            for dev_addr, to_read in todo.items():
                if not to_read:
                    continue

                client_entry = self.clients.get(dev_addr)
                if client_entry:
                    dev_name = client_entry["name"]
                    client = client_entry["client"]
                    dev_info = client_entry["info"]

                    self.logger.debug(f"Checking device {dev_addr=}, {dev_name=}, {dev_info=}")
                    self.logger.info(f"Lese fuer {dev_name} ({len(to_read)} DIDs)...")
                    client.read_dids(to_read)
                else:
                    self.logger.warning(f"Kein aktiver Client fuer Adresse {dev_addr} (Hex: {hex(dev_addr) if isinstance(dev_addr, int) else dev_addr}) gefunden!")
        finally:
            self._update_active = False

    def create_cyclic_scheduler(self) -> None:
        """Erstellt oder aktualisiert den SHNG-Scheduler für die Abfrageintervalle.

        Berechnet das kürzeste Lese-Intervall über alle konfigurierten Items und
        richtet einen passenden Scheduler-Task ein (Intervall = kürzester Zyklus / 2).
        """
        shortestcycle = -1

        for item in self._get_all_items_cyclic():
            item_config = self.get_item_config(item)
            read_cycle = item_config.get('read_cycle', 0)

            if read_cycle > 0 and (shortestcycle == -1 or read_cycle < shortestcycle):
                shortestcycle = read_cycle

        if shortestcycle != -1:
            workercycle = max(1, int(shortestcycle / 2))
            if self.scheduler_get('cyclic'):
                self.scheduler_remove('cyclic')
            self.scheduler_add('cyclic', self.poll_data, cycle=workercycle, prio=5, offset=0)
            self.logger.info(f'Added cyclic worker thread ({workercycle} sec cycle). Shortest item cycle: {shortestcycle} sec')

    def _get_items_by_config_key(self, filter_key: str) -> list:
        """Filtert registrierte Items anhand eines Schlüssels in den Konfigurationsdaten.

        Args:
            config_key (str): Der gesuchte Konfigurationsschlüssel (z. B. 'read_cycle').

        Returns:
            list: Liste der passenden SmartHomeNG Items.
        """
        return [entry["item"] for entry in list(self._plg_item_dict.values()) if filter_key in entry.get("config_data", {})]

    def _get_items_by_config_key_and_value_old(self, filter_key: str, min_value: int = 0) -> list:
        """Filtert registrierte Items anhand eines Konfigurationsschlüssels und prüft,

        ob der zugehörige Wert ein Integer größer als ein definierter Mindestwert ist (Standard > 0).

        Args:
            filter_key (str): Der gesuchte Konfigurationsschlüssel (z. B. 'read_cycle').
            min_value (int): Der minimale Wert (exklusiv), den der Integer haben muss (default: 0).

        Returns:
            list: Liste der passenden SmartHomeNG Items.
        """
        matching_items = []

        for entry in list(self._plg_item_dict.values()):
            config_data = entry.get("config_data", {})

            if filter_key in config_data:
                value = config_data[filter_key]

                # Prüfen, ob der Wert ein Integer (oder als String konvertierbarer Int) und > min_value ist
                if isinstance(value, int) and not isinstance(value, bool) and value > min_value:
                    matching_items.append(entry["item"])
                elif isinstance(value, str) and value.isdigit() and int(value) > min_value:
                    matching_items.append(entry["item"])

        return matching_items

    def _get_items_by_config_key_and_value(self, filter_key: str = '', filter_value: Any = None, op: str = '==') -> List[Any]:
        """
        Gibt eine Liste registrierter SmartHomeNG Items zurück, gefiltert nach Config-Key und Wert.

        :param filter_key: Schlüssel im 'config_data' Dict (z. B. 'read_cycle', 'open3e_ecu')
        :param filter_value: Zielwert für den Vergleich (int, str, bool, etc.)
        :param op: Vergleichsoperator oder -modus:
                - '=='     : Exakte Übereinstimmung (Default)
                - '>'      : Numerisch größer als filter_value
                - '>='     : Numerisch größer/gleich filter_value
                - '<'      : Numerisch kleiner als filter_value
                - '<='     : Numerisch kleiner/gleich filter_value
                - 'start'  : String beginnt mit filter_value
                - 'end'    : String endet mit filter_value
                - 'in'     : filter_value ist als Substring enthalten
        
        :return: Liste der passenden Item-Objekte
        """
        # Wenn kein Key angegeben ist oder filter_value None ist -> alle Items zurückgeben
        if not filter_key or filter_value is None:
            return [entry['item'] for entry in self._plg_item_dict.values()]

        matching_items = []

        for entry in list(self._plg_item_dict.values()):
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

        return matching_items

    def _get_all_items_cyclic(self) -> list:
        """Gibt alle Items zurück, die für die zyklische Abfrage konfiguriert sind.

        Returns:
            list: Liste aller zyklischen Lese-Items.
        """

        return self._get_items_by_config_key_and_value(filter_key="read_cycle", filter_value=0, op='>')

    def _get_all_items_init(self) -> list:
        """Gibt alle Items zurück, die bei Start initiert werden sollen.

        Returns:
            list: Liste aller initiellen Lese-Items.
        """
        return self.get_item_list(filter_key="read_init", filter_value=True)

    def load_dp_file(self, file_name: str, base_dir: Path) -> Dict[str, Any] | None:
        """Lädt eine Python-Datenpunktliste (DP-Liste) dynamisch als Modul ein.

        Args:
            file_name (str): Name der Py-Datei (z. B. 'E3_2050.py').
            base_dir (Path): Basispfad, in dem sich die Datei befindet.

        Returns:
            Dict[str, Any] | None: Das `dataIdentifiers`-Dictionary der Datei oder `None` bei Fehlern.
        """
        file_path = base_dir / file_name
        if not file_path.exists():
            self.logger.warning(f"[Warnung] Datei '{file_path}' wurde nicht gefunden.")
            return None

        try:
            module_name = file_path.stem
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            self.logger.error(f"[Fehler] Laden von '{file_name}' fehlgeschlagen: {exc}")
            return None

        if not hasattr(module, "dataIdentifiers"):
            self.logger.warning(f"[Warnung] 'dataIdentifiers' in '{file_name}' nicht gefunden.")
            return None
        return module.dataIdentifiers

    def load_devices_config(self, json_file: str = DEFAULT_CONFIG_FILE) -> Dict[str, Dict[str, Any]]:
        """Liest die zentrale Gerätekonfiguration (JSON) und verknüpfte DP-Dateien ein.

        Args:
            json_file (str): Dateiname der Geräte-JSON (Standard: 'devices.json').

        Returns:
            Dict[str, Dict[str, Any]]: Aufbereitetes Wörterbuch mit Geräte-Informationen und DIDs.
        """
        ordnerpfad = Path(__file__).resolve().parent
        ordnerpfad = ordnerpfad / DEFAULT_CONFIG_SUB_PATH
        json_path = ordnerpfad / json_file

        if not json_path.exists():
            self.logger.error(f"[Fehler] Datei '{json_file}' nicht gefunden.")
            return {}

        with open(json_path, "r", encoding="utf-8") as fh:
            devices_data = json.load(fh)

        devices: Dict[str, Dict[str, Any]] = {}
        self.logger.info("--- Lade Gerätekonfiguration ---")
        for dev_id, dev_info in devices_data.items():
            dp_file = dev_info.get("dpList")
            prop = dev_info.get("prop")
            tx = dev_info.get("tx")

            dp_data = self.load_dp_file(dp_file, ordnerpfad)
            if not dp_data:
                continue

            dids = dp_data.get("dids", {})
            devices[dev_id] = {
                "tx": tx,
                "prop": prop,
                "name": dp_data.get("name", prop),
                "dp_file": dp_file,
                "dp_path": str(ordnerpfad / dp_file),
                "dids": dids,
            }
            self.logger.info(f"  + {dev_id} ({prop}): {len(dids)} DIDs aus {dp_file}")
        self.logger.info("--------------------------------")
        return devices
   
    def data_handler(self, dev_name: str, dev_addr: int) -> Callable[[int, str, Any], None]:
        """Erstellt eine Callback-Funktion für Open3E-Updates."""
        
        def handler(did_id: int, did_name: str, value: Any) -> None:
            self.logger.info(f"[Update] [{dev_name}] DID {did_id} ({did_name}) -> {value!r}")

            # Alle SmartHomeNG-Items suchen, die diese DID und Adresse abonniert haben
            items_with_did = self._get_items_by_config_key_and_value(filter_key='did', filter_value=did_id, op='==')
            items_with_ecu = self._get_items_by_config_key_and_value(filter_key='ecu', filter_value=dev_addr, op='==')

            for item in set(items_with_did).intersection(items_with_ecu):
                config = self.get_item_config(item)
                
                # Stimmen ECU/Adresse und DID überein?
                if config.get('ecu') == dev_addr and config.get('did') == did_id:
                    sub_path = config.get('sub_path')

                    self.logger.debug(f"data_handler: Item {item.property.path} matched for DID {did_id} with sub_path={sub_path}")
                    self.logger.debug(f"data_handler: {did_id=}, {did_name=}, value={value}")
                    
                    if sub_path and isinstance(value, dict):
                        # Einzelwert aus dem Pfad extrahieren
                        item_val = self.get_dict_value_by_path(value, sub_path)
                    else:
                        item_val = value

                    # Wert im SmartHomeNG Item setzen (nur senden, wenn er existiert)
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
                client.add_data_callback(self.data_handler(dev_name, tx_addr))

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

        for dev_addr, client_entry in list(self.clients.items()):
            dev_name = client_entry["name"]
            client = client_entry["client"]
            try:
                client.disconnect()
                self.logger.info(f"Verbindung zu Gerät '{dev_name}' getrennt.")
            except Exception as e:
                self.logger.error(f"Fehler beim Trennen von '{dev_name}': {e}")
        
        self.clients.clear()

    def create_to_reads(self) -> Dict[int, List[int]]:
        """Erstellt eine Liste von DIDs, die für jedes Gerät gelesen werden sollen.

        Returns:
            Dict[int, List[int]]: Dictionary mit Geräteadressen als Schlüssel und
            Listen von eindeutigen DIDs als Werte.
        """
        to_reads = defaultdict(set)
        item_list = []
        
        currenttime = time.time()
        get_config = self.get_item_config

        is_init = not self._init_done
        items = self._get_all_items_init() if is_init else self._get_all_items_cyclic()

        self.logger.debug(f"create_to_reads: is_init={is_init}, items={[item.property.path for item in items]}")

        for item in items:
            config = get_config(item)
            if not config:
                continue

            # In der Init-Phase wird nexttime ignoriert
            if not is_init and config['nexttime'] > currenttime:
                continue

            ecu = config['ecu']
            did = config['did']
            read_cycle = config['read_cycle']
            
            # Deaktivierte Zyklustypen (0) überspringen
            if not is_init and read_cycle == 0:
                continue

            if ecu is not None and did is not None:
                item_list.append(item)
                config['nexttime'] = currenttime + read_cycle
                to_reads[ecu].add(did)

        if is_init:
            self._init_done = True

        self.logger.debug(f"Following items are scheduled for reading: {[item.property.path for item in item_list]}")

        return {ecu: list(dids) for ecu, dids in to_reads.items()}

    def get_dict_value_by_path(self, data: Any, path: str) -> Any:
        """Greift auf verschachtelte Keys oder Attribute wie 'BusType.Text' oder 'Actual' zu."""
        if not path or data is None:
            return data

        keys = path.split('.')
        for key in keys:
            if isinstance(data, dict):
                data = data.get(key)
            elif hasattr(data, key):
                data = getattr(data, key)
            else:
                return None

            # Falls ein Zwischenwert None ist, direkt abbrechen
            if data is None:
                return None

        return data

    def poll_all_data(self, ecu: str | int | None = None):
        # Falls eine spezielle ECU übergeben wurde, nur diese in eine Liste packen
        if ecu is not None:
            target_ecus = [self.clients[ecu]] if ecu in self.clients else []
        else:
            target_ecus = self.clients.values()

        for ecu_data in target_ecus:
            client = ecu_data.get('client')
            if client:
                try:
                    client.read_all_dids()
                except Exception as e:
                    self.logger.error(f"Fehler beim Pollen von ECU {ecu_data}: {e}")


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

                logger.info(f"\n--- Abfrage von DEMO DIDs für {dev_name} (tx={hex(dev_addr)}) ---")

                dids_to_read = list(DEMO_DIDS)
                available_dids = dev_info.get("dids", {})
                valid_dids = [d for d in dids_to_read if d in available_dids]

                if valid_dids:
                    logger.info(f"Lese bekannte DIDs: {valid_dids}")
                    client.read_dids(valid_dids)
                else:
                    first_dids = list(available_dids.keys())[:3]
                    logger.info(f"Demo-DIDs nicht in Konfiguration. Lese erste verfügbare DIDs: {first_dids}")
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