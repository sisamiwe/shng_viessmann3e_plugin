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
import time
import datetime
import json
import importlib.util
from typing import Any, Dict, List, Tuple, Callable, Optional, Union
from pathlib import Path
from collections import defaultdict

from .open3e_client import HAS_OPEN3E, Open3EClient
from .open3e_scanner import Open3EScanner

from lib.model.smartplugin import SmartPlugin
from .webif import WebInterface


"""Standarddateiname der Geräte-Konfigurationsdatei."""
DEFAULT_CONFIG_FILE: str = "devices.json"
DEFAULT_CONFIG_VAR_SUB_PATH: str = 'plugin_data'


class Open3E(SmartPlugin):
    """Hauptklasse des Open3E SmartHomeNG Plugins.

    Handhabt die Verbindung zum CAN-Bus, verwaltet die Endgeräte und verbindet
    SmartHomeNG-Items mit den Datenbezeichnern (DIDs) der Open3E-Schnittstelle.

    Attributes:
        PLUGIN_VERSION (str): Version des Plugins.
        ALLOW_MULTIINSTANCE (bool): Flag, ob Mehrfachinstanzen erlaubt sind.
    """

    PLUGIN_VERSION = '0.1.0'
    ALLOW_MULTIINSTANCE = False
    
    DEFAULT_COB: int = 0x680
    DEFAULT_DID: int = 256
    SCAN_SCHEDULER_DELAY: int = 20


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

        self.canport = self.get_parameter_value('can_port')
        self._pause_item_path = self.get_parameter_value('pause_item')
        self.read_all_at_init = bool(self.get_parameter_value('read_all_at_init'))
 
        # Dependency & configuration validation
        if not HAS_OPEN3E:
            self.logger.error("open3e-Bibliothek nicht verfuegbar. Plugin wird nicht gestartet.")
            self._init_complete = False

        # Config Data Path
        self.config_data_path = Path(self.get_sh().get_vardir()) / DEFAULT_CONFIG_VAR_SUB_PATH / self.get_fullname()
        self.config_data_path.mkdir(parents=True, exist_ok=True)

        # State initialization
        self._pause_item = None
        self.clients: Dict[int, Dict[str, Any]] = {}

        self._update_active: bool = False
        self._initial_item_read_done: bool = False
        self._filter_cache: Optional[List] = None
        self._filter_cache_key: str = ''
        self._filter_cache_value: Any = None
        self._filter_cache_op: str = ''

        # Load Devices
        self.load_config()

        # init WebIF
        self.init_webinterface(WebInterface)

    # =========================================================================
    # PLUGIN CORE
    # =========================================================================

    def run(self) -> None:
        """Startet das Plugin, lädt die Konfiguration und baut Verbindungen auf.

        Liest die Konfigurationsdatei ein, verbindet mit den CAN-Geräten,
        initialisiert den zyklischen Abfrage-Scheduler und setzt das Alive-Flag.
        """
        self.logger.dbghigh(self.translate("Methode '{method}' aufgerufen", {'method': 'run()'}))

        # Plugin alive setzen
        self.alive = True

        if self.devices:
            # Verbindung aufbauen
            self.connect_to_devices()

            # Scheduler für zyklische Abfragen erstellen
            self.setup_scheduler()

            # Initiale Abfrage der konfigurierten DIDs
            self.poll_data()

        else:
            # setup scheduler for scan
            next_time = self.shtime.now() + datetime.timedelta(seconds=self.SCAN_SCHEDULER_DELAY)
            job_name = f"{self.get_fullname()}: depict_system"

            self.scheduler_add(
                name=job_name,
                obj=self.depict_system,
                next=next_time
            )

        if self._pause_item:
            self._pause_item(False, self.get_fullname())
        
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
        `open3e_write`, `open3e_read_after_write`, `open3e_ecu` etc. aus
        und registriert das Item (unterstützt kombiniertes Lesen und Schreiben).

        Args:
            item: Das zu analysierende SmartHomeNG Item-Objekt.

        Returns:
            Optional[Callable]: Die Update-Methode des Plugins (`self.update_item`), falls
            Schreibfunktionen oder das Pause-Item aktiv sind, andernfalls `None`.
        """
        # Sonderfall: Pause-Item
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
        
        # Attribute auslesen
        open3e_read_at_init = bool(self.get_iattr_value(item.conf, 'open3e_read_at_init'))
        open3e_read_cycle = int(self.get_iattr_value(item.conf, 'open3e_read_cycle') or 0)
        open3e_write = bool(self.get_iattr_value(item.conf, 'open3e_write'))
        open3e_read_after_write = int(self.get_iattr_value(item.conf, 'open3e_read_after_write') or 0)
        item_active: bool = True

        # Abbruch, wenn weder Lese- noch Schreibregel aktiv sind
        if not (open3e_read_at_init or open3e_read_cycle > 0 or open3e_write):
            return None

        # Setze read_at_init
        open3e_read_at_init = open3e_read_at_init or self.read_all_at_init

        # ECU & DID verarbeiten
        raw_ecu = self.get_iattr_value(item.conf, 'open3e_ecu')
        item_path = item.property.path 

        # ECU als Integer parsen (oder Fallback verwenden)
        try:
            ecu = int(str(raw_ecu), 0) if raw_ecu is not None else self.DEFAULT_COB
        except (ValueError, TypeError):
            self.logger.warning(f"ECU '{raw_ecu}' definiert in {item_path} hat ein ungültiges Format. Item wird übersprungen.")
            return None

        # DID auflösen
        raw_did = self.get_iattr_value(item.conf, 'open3e_did')
        did, sub_path = self._resolve_did(raw_did)

        if did is None:
            self.logger.warning(f"DID '{raw_did}' definiert in {item_path} ist ungültig. Item wird übersprungen.")
            return None

        # Prüfe, ob ECU unterstützt wird
        if ecu not in self.devices:
            self.logger.warning(f"ECU {ecu} definiert in {item_path} ist nicht verfügbar. Item wird passiv gesetzt und nicht bedient.")
            item_active = False
        else:
            # Prüfe, ob DID von der ECU unterstützt wird (Safeguard via .get())
            ecu_dids = self.devices[ecu].get('dids', {})
            if did not in ecu_dids:
                self.logger.warning(f"DID {did} definiert in {item_path} wird von ECU {ecu} nicht unterstützt. Item wird passiv gesetzt und nicht bedient.")
                item_active = False

        # Kombinierte Konfiguration erstellen
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
            'read_after_write': open3e_read_after_write,
            'active': item_active
        }

        self.logger.debug(f"parse_item [READ={is_read_active}, WRITE={open3e_write}]: {item.property.path} -> {item_config}")

        # Item EINMALIG registrieren
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

    def check_non_active_items(self) -> None:
        """Check non-active items against discovered ECUs and their supported DIDs."""

        self.logger.info("Check check_non_active_items")

        non_active_items = self.get_non_active_items()
        self.logger.info(f"{non_active_items=}")

        for item in non_active_items:
            config = self.get_item_config(item)
            ecu, did = config['ecu'], config['did']

            if ecu not in self.devices:
                self.logger.info(f"ECU {ecu} definiert in {item.property.path} ist nicht verfügbar. Item bleibt auf passiv gesetzt und wird nicht bedient.")
                continue

            if did not in self.devices[ecu].get('dids', {}):
                self.logger.info(f"DID {did} definiert in {item.property.path} wird von ECU {ecu} nicht unterstützt. Item bleibt auf passiv gesetzt und wird nicht bedient.")
            else:
                self.logger.info(f"DID {did} definiert in {item.property.path} wird von ECU {ecu} unterstützt. Item bleibt auf aktiv gesetzt und bedient.")
                config['active'] = True
    
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
                   for item in self.get_active_cyclic_items()]
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
        items = self.get_active_init_items() if is_init else self.get_active_cyclic_items()

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
            target_ecus = [self.clients.get(ecu)] if ecu in self.clients else []
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

    def get_non_active_items(self) -> list:
        return self._filter_items(filter_key="active", filter_value=False)

    def get_active_items(self) -> list:
        return self._filter_items(filter_key="active", filter_value=True)

    def get_active_cyclic_items(self) -> list:
        """Gibt alle Items zurueck, die fuer die zyklische Abfrage konfiguriert sind."""
        _read_cycle_items = self._filter_items(filter_key="read_cycle", filter_value=0, op='>')
        return list(set(_read_cycle_items) & set(self.get_active_items()))

    def get_active_init_items(self) -> list:
        """Gibt alle Items zurueck, die bei Start initiert werden sollen."""
        _init_items = self._filter_items(filter_key="read_init", filter_value=True)
        return list(set(_init_items) & set(self.get_active_items()))
    
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
        
        json_path = self.config_data_path / json_file

        if not json_path.exists():
            self.logger.debug(f"Konfigurationsdatei '{json_file}' nicht gefunden.")
            return {}

        with open(json_path, "r", encoding="utf-8") as fh:
            devices_data = json.load(fh)

        devices: Dict[str, Dict[str, Any]] = {}

        self.logger.info("--- Lade Gerätekonfiguration ---")
        for dev_id, dev_info in devices_data.items():
            dp_file = dev_info.get("dpList")
            prop = dev_info.get("prop")
            tx = dev_info.get("tx")

            dp_data = self.load_datapoints(dp_file, self.config_data_path)
            if not dp_data:
                continue

            dids = dp_data.get("dids", {})
            try:
                dev_key = int(dev_id, 16)
            except ValueError:
                self.logger.warning(f"Ungültige Geräte-ID '{dev_id}' in Konfiguration. Überspringe.")
                continue
            devices[dev_key] = {
                "tx": tx,
                "prop": prop,
                "name": dp_data.get("name", prop),
                "dp_file": dp_file,
                "dp_path": str(self.config_data_path / dp_file),
                "dids": dids,
            }
            self.logger.info(f"  + {dev_id} ({prop}): {len(dids)} DIDs aus {dp_file}")
        self.logger.info("--------------------------------")
        return devices

    def load_config(self) -> None:
        self.devices: Dict[str, Dict[str, Any]] = self.load_devices(DEFAULT_CONFIG_FILE)
    
        if not self.devices:
            self.logger.warning("Es sind keine E3 Geräte konfiguriert. Nach dem Start von shNG wird automatisch ein Scan durchgeführt.")
        else:
            self.logger.info(f"Es sind {len(self.devices)} E3 Geräte konfiguriert.")
   
    # =========================================================================
    # GERÄTEVERBINDUNG & DATENHANDLER
    # =========================================================================

    def make_update_handler(self, dev_name: str, dev_addr: int) -> Callable[[int, str, Any], None]:
        """Erstellt eine Callback-Funktion fuer Open3E-Updates.
        
        Uses tuple mapping (dev_addr, did_id) for item lookup.
        """
        
        def handler(did_id: int, did_name: str, value: Any) -> None:
            # self.logger.debug(f"[{dev_name}] DID {did_id} ({did_name}) = {value!r}")

            # O(1) lookup - returns only items for this specific (ECU, DID) combo
            did_items = self.get_items_for_mapping(mapping=(dev_addr, did_id))
            
            for item in did_items:
                config = self.get_item_config(item)
                sub_path = config.get("sub_path")

                # self.logger.debug(f"update_handler: Item {item.property.path} matched for DID {did_id}")

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
                try:
                    tx_addr = int(dev_info["tx"], 16)
                except (ValueError, KeyError):
                    self.logger.error(f"Ungültige TX-Adresse '{dev_info.get('tx')}' für Gerät '{dev_name}'. Überspringe.")
                    continue
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

    def interview_all_ecus(self):
        for ecu in self.devices:
            target_client = self._get_client(ecu)
            if target_client is None:
                self.logger.error(f"interview_all_ecus: Kein aktiver Client fuer Geraet '{ecu}' gefunden.")
                continue

            try:
                self.devices_info[ecu] = target_client.read_did(self.DEFAULT_DID)
            except Exception as e:
                self.logger.error(f"Fehler beim Auslesen von ECU '{ecu}' (DID: {self.DEFAULT_DID}): {e}")

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

    def depict_system(self, config_dir: Path | None = None) -> None:

        if not config_dir:
            config_dir = self.config_data_path

        self.logger.info(f"Scan von angeschlossenen ECUs und deren DIDs gestartet.")
        
        scanner = self._get_scanner()
        if not scanner:
            return []

        self.logger.warning(f"Plugin {self.get_fullname()} will be stopped for Scan and resumed afterwards.")
        self.stop()
        
        try:
            scanner.depict_system(config_dir)
        except Exception as ex:
            self.logger.error(f"Fehler beim Scan: {ex}", exc_info=True)
        else:
            # lade die config files neu
            self.load_config()

            # prüfe die non-active Items, ob diese nun bedient werden
            self.check_non_active_items()
        
        finally:
            self.logger.warning(f"Plugin {self.get_fullname()} will be resumed.")
            self.logger.info("Scan abgeschlossen.")
            self.run()
