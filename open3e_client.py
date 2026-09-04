from __future__ import annotations

import time
import threading
from typing import Any, Callable, Dict, List, Optional
import logging

# WORKAROUND: Blockade von SmartHomeNG durch udsoncan.setup_logging verhindern
try:
    import udsoncan
    udsoncan.setup_logging = lambda *args, **kwargs: None
except ImportError:
    pass

try:
    from open3e.Open3Eclass import O3Eclass
    HAS_OPEN3E = True
except Exception:
    HAS_OPEN3E = False

CallbackType = Callable[[int, str, Any], None]
"""Signatur: ``callback(did_id: int, did_name: str, value: Any) -> None``."""


class Open3EClient:
    """
    Kompakter, MQTT-freier Wrapper um :class:`open3e.Open3Eclass.O3Eclass`.
    """

    def __init__(
        self,
        bus: str = "can0",
        devtype: Optional[str] = None,
        e3_address: int = 0x680,
        logger: Optional[logging.Logger] = None
    ) -> None:
        
        self.bus_name: str = bus
        self.devtype: Optional[str] = devtype
        self.e3_address: int = e3_address
        self.logger: logging.Logger = logger or logging.getLogger("open3e_client")

        self.logger.debug(f"[Open3Eclient] OpenE3-Integration verfügbar: {HAS_OPEN3E}")
        self.logger.info(
            f"[Open3Eclient] Initialisiere für Bus: '{self.bus_name}', "
            f"devtype: '{self.devtype}', E3-Adresse: '{hex(self.e3_address)}'"
        )

        self.o3e: Optional[O3Eclass] = None
        self.data_callbacks: List[CallbackType] = []
        self._cb_lock: threading.Lock = threading.Lock()
        
        # Performance-Cache für name -> did Lookups O(1)
        self._name_to_did_map: Dict[str, int] = {}

        # local data storage
        self.data = {}

    def add_data_callback(self, callback_func: CallbackType) -> None:
        """Registriert einen Callback thread-sicher."""
        with self._cb_lock:
            if callback_func not in self.data_callbacks:
                self.data_callbacks.append(callback_func)

    def remove_data_callback(self, callback_func: CallbackType) -> None:
        """Entfernt einen registrierten Callback thread-sicher."""
        with self._cb_lock:
            if callback_func in self.data_callbacks:
                self.data_callbacks.remove(callback_func)

    def _emit(self, did_id: int, did_name: str, value: Any) -> None:
        """Ruft alle registrierten Callbacks isoliert und thread-sicher auf."""
        with self._cb_lock:
            callbacks = list(self.data_callbacks)

        for cb in callbacks:
            try:
                cb(did_id, did_name, value)
            except Exception as exc:
                self.logger.error(f"[Open3Eclient Error] Fehler im Callback für DID {did_id}: {exc}")

    def _build_name_cache(self) -> None:
        """Baut das Name-to-DID Mapping für schnellen O(1) Zugriff auf."""
        self._name_to_did_map.clear()
        if not self.o3e or not hasattr(self.o3e, "dataIdentifiers"):
            return

        for did, codec in self.o3e.dataIdentifiers.items():
            codec_id = getattr(codec, "id", None)
            if codec_id:
                self._name_to_did_map[codec_id.lower()] = did

    def connect(self, timeout: float = 15.0) -> None:
        """Baut die UDS/CAN-Verbindung auf und initialisiert O3Eclass."""
        if not HAS_OPEN3E:
            raise RuntimeError("open3e-Bibliothek ist nicht installiert oder konnte nicht geladen werden.")

        self.logger.info(
            f"[Open3Eclient] Starte O3Eclass für Bus='{self.bus_name}', "
            f"devtype='{self.devtype}', E3-Adresse='{hex(self.e3_address)}'..."
        )
        try:
            self.o3e = O3Eclass(can=self.bus_name, dev=self.devtype, ecutx=self.e3_address)
            self._build_name_cache()
            self.logger.info(f"[Open3Eclient] Verbunden mit Bus '{self.bus_name}' ({self.devtype})")
        except Exception as exc:
            self.logger.error(f"[Open3Eclient] Fehler beim Verbinden: {exc}")
            self.o3e = None
            raise

    def _resolve_name(self, did_id: int) -> str:
        """Liefert den Klartext-Namen eines DID, falls bekannt."""
        if self.o3e and hasattr(self.o3e, "dataIdentifiers") and did_id in self.o3e.dataIdentifiers:
            return getattr(self.o3e.dataIdentifiers[did_id], "id", f"DID_{did_id}")
        return f"DID_{did_id}"

    def read_dids(self, dids: List[int]) -> None:
        """Liest eine Liste von DIDs per UDS und emittiert die Werte."""

        if not self.o3e:
            raise RuntimeError("Client ist nicht verbunden. Bitte erst connect() aufrufen.")
            return

        for did in dids:
            try:
                result = self.o3e.readByDid(did, raw=False)
                self.logger.debug(f"[Open3Eclient] Lese DID {did}: Ergebnis = {result}")

                if isinstance(result, tuple) and len(result) == 3:
                    val, idstr, idid = result

                    # Fehlererkennung anhand des 'ERR/'-Präfixes
                    if isinstance(idstr, str) and idstr.startswith('ERR/'):
                        self.logger.error(f"[Open3Eclient] Fehler bei DID {idid} ({did}): {val}")
                    else:
                        # Erfolgreich gelesen -> val enthält Dict oder Einzelwert
                        self._emit(idid, idstr, val)

                else:
                    self.logger.error(f"[Open3Eclient] Unerwartetes Rückgabeformat für DID {did}: {result}")

            except Exception as exc:
                self.logger.error(f"[Open3Eclient] Unerwarteter Fehler beim Lesen von DID {did}: {exc}", exc_info=True)

    def read_all_dids(self) -> None:
        """Liest alle verfügabren Liste von DIDs per UDS und emittiert die Werte."""
        if not self.o3e:
            raise RuntimeError("Client ist nicht verbunden. Bitte erst connect() aufrufen.")

        try:
            data_list = self.o3e.readAll(raw=False)
            self.logger.debug(f"[Open3Eclient] Lese alle DIDs: Ergebnis = {data_list}")
            
        except Exception as exc:
            self.logger.error(f"[Open3Eclient] Unerwarteter Fehler beim Lesen: {exc}")

        else:
            # store data in local dict
            self.data = {
                item[0]: {
                    'value': item[1],
                    'idstr': item[2]
                }
                for item in data_list
            }

            # emit for callback
            for did, value, idstr in data_list:
                self.logger.debug(f"[Open3Eclient] DID: {did}, ID: {idstr}, Wert: {value}")
                self._emit(did, idstr, value)

    def write_did(self, did: int, value: Any) -> bool:
        """Schreibt einen Wert auf einen DID per UDS."""
        if not self.o3e:
            raise RuntimeError("Client ist nicht verbunden. Bitte erst connect() aufrufen.")
        
        try:
            success, _code = self.o3e.writeByDid(did, value, raw=False)
            return bool(success)
        except Exception as exc:
            self.logger.error(f"[Open3Eclient] Schreibfehler auf DID {did}: {exc}")
            return False

    def spin_once(self, timeout: float = 0.05) -> None:
        """Kompatibilitäts-Hook für polling-basierte Hauptschleifen."""
        time.sleep(timeout)

    def describe_did(self, did: int) -> Optional[Dict[str, Any]]:
        """Liefert die Struktur eines DID (Sub-Felder, Typen, Einheiten)."""
        if not self.o3e or not hasattr(self.o3e, "dataIdentifiers"):
            return None
        codec = self.o3e.dataIdentifiers.get(did)
        if codec is None or not hasattr(codec, "getCodecInfo"):
            return None
        return codec.getCodecInfo()

    def describe_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Wie describe_did, erwartet aber einen Klartext-Namen."""
        did = self.lookup_did(name)
        return self.describe_did(did) if did is not None else None

    def list_dids(self) -> List[Dict[str, Any]]:
        """Listet alle DIDs des aktuellen DpSets auf."""
        if not self.o3e or not hasattr(self.o3e, "dataIdentifiers"):
            return []
        
        descriptions = []
        for did in sorted(self.o3e.dataIdentifiers.keys()):
            desc = self.describe_did(did)
            if desc:
                descriptions.append(desc)
        return descriptions

    def lookup_did(self, name: str) -> Optional[int]:
        """Liefert die DID-Nummer zu einem Klartext-Namen in O(1)."""
        if not self.o3e:
            return None
        return self._name_to_did_map.get(name.lower())

    def read_dids_by_name(self, names: List[str]) -> Dict[str, Any]:
        """Liest mehrere Datenpunkte anhand ihrer Klartext-Namen."""
        if not self.o3e:
            raise RuntimeError("Client ist nicht verbunden. Bitte erst connect() aufrufen.")

        name_to_did: Dict[str, Optional[int]] = {n: self.lookup_did(n) for n in names}
        dids: List[int] = [d for d in name_to_did.values() if d is not None]

        captured: Dict[int, Any] = {}

        def _capture(did_id: int, _name: str, value: Any) -> None:
            captured[did_id] = value

        self.add_data_callback(_capture)
        try:
            if dids:
                self.read_dids(dids)
        finally:
            self.remove_data_callback(_capture)

        return {
            name: (captured.get(did) if did is not None else None)
            for name, did in name_to_did.items()
        }

    def read_by_name(self, name: str) -> Optional[Any]:
        """Liest einen einzelnen Datenpunkt anhand seines Klartext-Namens."""
        return self.read_dids_by_name([name]).get(name)

    def disconnect(self) -> None:
        """Trennt die Verbindung und gibt die O3Eclass-Instanz frei."""
        if self.o3e:
            if hasattr(self.o3e, "close"):
                try:
                    self.o3e.close()
                except Exception as exc:
                    self.logger.warning(f"[Open3Eclient] Fehler beim Schließen der Verbindung: {exc}")
            self.logger.info("[Open3Eclient] Verbindung getrennt.")
            self.o3e = None
            self._name_to_did_map.clear()