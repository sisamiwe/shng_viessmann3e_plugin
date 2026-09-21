from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

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
    """Kompakter Wrapper um :class:`open3e.Open3Eclass.O3Eclass`."""

    # =========================================================================
    # 1. LEBENSZYKLUS & VERBINDUNG (Lifecycle & Connection)
    # =========================================================================

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

        # Lokaler Datenspeicher: did -> {'value': ..., 'idstr': ...}
        self.data: Dict[int, Dict[str, Any]] = {}

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

    # =========================================================================
    # 2. CALLBACK-VERWALTUNG & EVENT-DISPATCHING
    # =========================================================================

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
                self.logger.error(
                    f"[Open3Eclient] Fehler im Callback für DID {did_id} ({did_name}): {exc}", 
                    exc_info=True
                )

    # =========================================================================
    # 3. DATEN ZUGRIFF PER ID (Read & Write by DID-ID)
    # =========================================================================

    def read_did(self, did: int) -> Optional[Any]:
        """Liest ein einzelnes DID per UDS, emittiert den Wert und gibt ihn zurück."""
        o3e = self.o3e
        if not o3e:
            raise RuntimeError("Client ist nicht verbunden. Bitte erst connect() aufrufen.")

        try:
            result = o3e.readByDid(did, raw=False)
            self.logger.debug(f"[Open3Eclient] Lese DID {did}: Ergebnis = {result}")

            if isinstance(result, tuple) and len(result) == 3:
                val, idstr, idid = result

                # Fehlererkennung anhand des 'ERR/'-Präfix
                if isinstance(idstr, str) and idstr.startswith("ERR/"):
                    self.logger.error(f"[Open3Eclient] Fehler bei DID {idid} ({did}): {val}")
                    return None

                # Lokalen Speicher & Callbacks aktualisieren
                self.data[idid] = {"value": val, "idstr": idstr}
                self._emit(idid, idstr, val)
                return val
            else:
                self.logger.error(f"[Open3Eclient] Unerwartetes Rückgabeformat für DID {did}: {result}")
                return None

        except Exception as exc:
            self.logger.error(f"[Open3Eclient] Unerwarteter Fehler beim Lesen von DID {did}: {exc}", exc_info=True)
            return None

    def read_dids(self, dids: List[int]) -> None:
        """Liest eine Liste von DIDs per UDS und emittiert deren Werte."""
        for did in dids:
            self.read_did(did)

    def read_all_dids(self) -> None:
        """Liest alle verfügbaren DIDs per UDS und emittiert die Werte."""
        o3e = self.o3e
        if not o3e:
            raise RuntimeError("Client ist nicht verbunden. Bitte erst connect() aufrufen.")

        try:
            data_list = o3e.readAll(raw=False)
            self.logger.debug(f"[Open3Eclient] Lese alle DIDs: {len(data_list) if data_list else 0} Einträge")
            
            if data_list:
                for did, value, idstr in data_list:
                    self.data[did] = {"value": value, "idstr": idstr}
                    self._emit(did, idstr, value)

        except Exception as exc:
            self.logger.error(f"[Open3Eclient] Unerwarteter Fehler beim Lesen aller DIDs: {exc}", exc_info=True)

    def write_did(
        self, 
        did: int, 
        value: Any, 
        raw: bool = False, 
        use_service_77: bool = False, 
        sub: Optional[Any] = None, 
        read_ecu: Optional[Any] = None
    ) -> bool:
        """Schreibt einen Wert auf ein DID per UDS."""
        if not self.o3e:
            raise RuntimeError("Client ist nicht verbunden. Bitte erst connect() aufrufen.")
        
        try:
            success, _code = self.o3e.writeByDid(
                did=did, 
                val=value, 
                raw=raw, 
                useService77=use_service_77, 
                sub=sub, 
                readecu=read_ecu
            )
            
            if isinstance(_code, str) and _code.startswith("ERR/"):
                self.logger.error(f"[Open3Eclient] Schreibfehler auf DID {did}: {success} ({_code})")
                return False

            self.logger.info(f"[Open3Eclient] Schreiben erfolgreich auf DID {did}: {value}")
            return True

        except Exception as exc:
            self.logger.error(f"[Open3Eclient] Unerwarteter Schreibfehler auf DID {did}: {exc}", exc_info=True)
            return False

    # =========================================================================
    # 4. DATENZUGRIFF PER KLARTEXT-NAME (Name-based Access)
    # =========================================================================

    def lookup_did(self, name: str) -> Optional[int]:
        """Liefert die DID-Nummer zu einem Klartext-Namen in O(1)."""
        return self._name_to_did_map.get(name.lower()) if self.o3e else None

    def read_by_name(self, name: str) -> Optional[Any]:
        """Liest einen einzelnen Datenpunkt anhand seines Klartext-Namens."""
        did = self.lookup_did(name)
        return self.read_did(did) if did is not None else None

    def read_dids_by_name(self, names: List[str]) -> Dict[str, Any]:
        """Liest mehrere Datenpunkte anhand ihrer Klartext-Namen."""
        results: Dict[str, Any] = {}
        for name in names:
            did = self.lookup_did(name)
            if did is not None:
                results[name] = self.read_did(did)
            else:
                self.logger.warning(f"[Open3Eclient] Name '{name}' konnte keiner DID zugeordnet werden.")
                results[name] = None
        return results

    # =========================================================================
    # 5. METADATEN & INSPEKTION (Schema, Codecs & Cache)
    # =========================================================================

    def describe_did(self, did: int) -> Optional[Dict[str, Any]]:
        """Liefert die Struktur einer DID (Sub-Felder, Typen, Einheiten)."""
        dids = self._get_data_identifiers()
        if dids is None:
            return None

        codec = dids.get(did)
        get_info = getattr(codec, "getCodecInfo", None)
        return get_info() if callable(get_info) else None

    def describe_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Wie describe_did, erwartet aber einen Klartext-Namen."""
        did = self.lookup_did(name)
        return self.describe_did(did) if isinstance(did, int) else None

    def list_dids(self) -> Dict[int, Dict[str, Any]]:
        """Listet alle DIDs des aktuellen DpSets auf."""
        dids = self._get_data_identifiers()
        if not dids:
            return {}

        descriptions: Dict[int, Dict[str, Any]] = {}
        for did, codec in sorted(dids.items()):
            get_info = getattr(codec, "getCodecInfo", None)
            if callable(get_info):
                desc = get_info()
                if desc:
                    descriptions[did] = desc

        return descriptions

    def _get_data_identifiers(self) -> Optional[Dict[int, Any]]:
        """Interne Hilfsmethode zum sicheren Abrufen des dataIdentifiers-Dicts."""
        o3e = getattr(self, "o3e", None)
        return getattr(o3e, "dataIdentifiers", None) if o3e else None

    def _build_name_cache(self) -> None:
        """Baut das Name-to-DID Mapping für schnellen O(1) Zugriff auf."""
        self._name_to_did_map.clear()
        dids = self._get_data_identifiers()
        if not dids:
            return

        for did, codec in dids.items():
            codec_id = getattr(codec, "id", None)
            if codec_id:
                self._name_to_did_map[str(codec_id).lower()] = did