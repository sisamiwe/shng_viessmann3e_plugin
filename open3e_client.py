from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

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

READ_FAULTS_BEFORE_BLACKLISTED: int = 3

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
        """Initialisiert den Open3E-Client.

        Args:
            bus: CAN-Schnittstellenname (z.B. 'can0').
            devtype: Pfad zur Datenpunktliste (DpFile).
            e3_address: Transmit-Adresse der ECU (Standard: 0x680).
            logger: Logger-Instanz. Wird bei None automatisch erstellt.
        """
        self.bus_name: str = bus
        self.devtype: Optional[str] = devtype
        self.e3_address: int = e3_address
        self.logger: logging.Logger = logger or logging.getLogger("open3e_client")

        self.logger.info(
            f"Initialisiere Client für Bus='{self.bus_name}', "
            f"devtype='{self.devtype}', E3-Adresse={hex(self.e3_address)}"
        )

        self.o3e: Optional[O3Eclass] = None
        self.data_callbacks: List[CallbackType] = []
        self._cb_lock: threading.Lock = threading.Lock()
        
        # Performance-Cache für name -> did Lookups O(1)
        self._name_to_did_map: Dict[str, int] = {}

        # Lokaler Datenspeicher: did -> {'value': ..., 'idstr': ...}
        self.data: Dict[int, Dict[str, Any]] = {}

    @property
    def connected(self) -> bool:
        """True, wenn eine aktive Verbindung besteht."""
        return self.o3e is not None

    def connect(self) -> None:
        """Baut die UDS/CAN-Verbindung auf und initialisiert O3Eclass."""
        self.logger.debug(
            f"Starte O3Eclass für Bus='{self.bus_name}', "
            f"devtype='{self.devtype}', E3-Adresse={hex(self.e3_address)}"
        )
        try:
            self.o3e = O3Eclass(can=self.bus_name, dev=self.devtype, ecutx=self.e3_address)
            self._build_name_cache()
            self.logger.info(f"Verbunden mit Bus '{self.bus_name}' ({self.devtype})")
        except Exception:
            self.o3e = None
            raise

    def disconnect(self) -> None:
        """Trennt die Verbindung und gibt die O3Eclass-Instanz frei."""
        if self.o3e is None:
            return

        try:
            self.o3e.close()
        except Exception as exc:
            self.logger.warning(f"Fehler beim Schließen der Verbindung: {exc}")

        self.o3e = None
        self._name_to_did_map.clear()
        self.logger.info("Verbindung getrennt.")

    # =========================================================================
    # 2. CALLBACK-VERWALTUNG & EVENT-DISPATCHING
    # =========================================================================

    def add_callback(self, callback_func: CallbackType) -> None:
        """Registriert einen Callback thread-sicher.

        Args:
            callback_func: Funktion mit Signatur (did_id: int, did_name: str, value: Any).
        """
        with self._cb_lock:
            if callback_func not in self.data_callbacks:
                self.data_callbacks.append(callback_func)

    def remove_callback(self, callback_func: CallbackType) -> None:
        """Entfernt einen registrierten Callback thread-sicher.

        Args:
            callback_func: Der vorher registrierte Callback.
        """
        with self._cb_lock:
            if callback_func in self.data_callbacks:
                self.data_callbacks.remove(callback_func)

    def _emit(self, did_id: int, did_name: str, value: Any) -> None:
        """Ruft alle registrierten Callbacks isoliert und thread-sicher auf.

        Args:
            did_id: DID-Nummer.
            did_name: Name der DID.
            value: Gelesener Wert.
        """
        with self._cb_lock:
            callbacks = list(self.data_callbacks)

        for cb in callbacks:
            try:
                cb(did_id, did_name, value)
            except Exception as exc:
                self.logger.error(
                    f"Fehler im Callback für DID {did_id} ({did_name}): {exc}",
                    exc_info=True
                )

    def _process_read_result(self, result: Any) -> Optional[Any]:
        """Verarbeitet ein O3Eclass-Leseergebnis und emittiert Callbacks.

        Args:
            result: Rohresultat von O3Eclass.readByDid() oder readAll().

        Returns:
            Den extrahierten Wert oder None bei Fehler/ungueltigem Format.
        """
        if isinstance(result, tuple) and len(result) == 3:
            val, idstr, idid = result

            if isinstance(idstr, str) and idstr.startswith("ERR/"):
                self.logger.error(f"Fehler bei DID {idid}: {val}")
                return None

            self.data[idid] = {"value": val, "idstr": idstr}
            self._emit(idid, idstr, val)
            return val

        self.logger.error(f"Unerwartetes Rückgabeformat: {result}")
        return None

    # =========================================================================
    # 3. DATEN ZUGRIFF PER ID (Read & Write by DID-ID)
    # =========================================================================

    def read_did(self, did: int) -> Optional[Any]:
        """Liest ein einzelnes DID per UDS, emittiert den Wert und gibt ihn zurueck.

        Args:
            did: Die DID-Nummer.

        Returns:
            Den gelesenen Wert oder None bei Fehler.
        """
        if not self.connected:
            raise RuntimeError("Client ist nicht verbunden. Bitte erst connect() aufrufen.")

        try:
            result = self.o3e.readByDid(did, raw=False)
            self.logger.debug(f"Lese DID {did}: Ergebnis = {result}")
            return self._process_read_result(result)
        except Exception as exc:
            self.logger.error(f"Fehler beim Lesen von DID {did}: {exc}", exc_info=True)
            # Fehler bei DID 425: Device rejected this read access. Probably DID 425 is not available. ReadDataByIdentifier service execution returned a negative response RequestOutOfRange (0x31)
            return None

    def read_dids(self, dids: List[int]) -> None:
        """Liest eine Liste von DIDs per UDS und emittiert deren Werte.

        Args:
            dids: Liste von DID-Nummern.
        """
        for did in dids:
            self.read_did(did)

    def read_all_dids(self) -> List[List[Any]]:
        """Liest alle verfuegbaren DIDs per UDS und emittiert die Werte.

        Returns:
            Liste von [did, value, idstr]_tuples.
        """
        if not self.connected:
            raise RuntimeError("Client ist nicht verbunden. Bitte erst connect() aufrufen.")

        try:
            data_list = self.o3e.readAll(raw=False) or []
            self.logger.debug(f"Lese alle DIDs: {len(data_list)} Eintraege")

            for did, value, idstr in data_list:
                self.data[did] = {"value": value, "idstr": idstr}
                self._emit(did, idstr, value)
            return data_list
        except Exception as exc:
            self.logger.error(f"Fehler beim Lesen aller DIDs: {exc}", exc_info=True)
            return []

    def read_did_raw(self, did: int, binary: bool = False) -> Tuple[Any, str]:
        """Liest rohe DID-Daten ohne Codec-Decodierung.

        Nuetzlich fur DIDs ohne Datapunkt-Definition oder Debugging.

        Args:
            did: Die DID-Nummer.
            binary: True fuer rohe Bytes, False fuer hex-String.

        Returns:
            Tuple aus Wert und ID-String.

        Raises:
            RuntimeError: Wenn der Client nicht verbunden ist.
        """
        if not self.connected:
            raise RuntimeError("Client ist nicht verbunden. Bitte erst connect() aufrufen.")

        return self.o3e.readPure(did, binary=binary)

    def write_did(
        self,
        did: int,
        value: Any,
        raw: bool = False,
        use_service_77: bool = False,
        sub: Optional[Any] = None,
        read_ecu: Optional[Any] = None
    ) -> bool:
        """Schreibt einen Wert auf ein DID per UDS.

        Args:
            did: Die DID-Nummer.
            value: Der zu schreibende Wert.
            raw: Rohwert ohne Konvertierung verwenden.
            use_service_77: Service 77 statt 0x2E verwenden.
            sub: Sub-Parameter für verschachtelte Werte.
            read_ecu: ECU-Parameter.

        Returns:
            True bei Erfolg, False bei Fehler.
        """
        if not self.connected:
            raise RuntimeError("Client ist nicht verbunden. Bitte erst connect() aufrufen.")

        try:
            success, code = self.o3e.writeByDid(
                did=did,
                val=value,
                raw=raw,
                useService77=use_service_77,
                sub=sub,
                readecu=read_ecu
            )

            if isinstance(code, str) and code.startswith("ERR/"):
                self.logger.error(f"Schreibfehler auf DID {did}: {success} ({code})")
                return False

            self.logger.info(f"Schreiben erfolgreich auf DID {did}: {value}")
            return True
        except Exception as exc:
            self.logger.error(f"Fehler beim Schreiben auf DID {did}: {exc}", exc_info=True)
            return False

    # =========================================================================
    # 4. DATENZUGRIFF PER KLARTEXT-NAME (Name-based Access)
    # =========================================================================

    def lookup_did(self, name: str) -> Optional[int]:
        """Liefert die DID-Nummer zu einem Klartext-Namen in O(1).

        Args:
            name: Klartext-Name der DID.

        Returns:
            Die DID-Nummer oder None, falls nicht gefunden.
        """
        return self._name_to_did_map.get(name.lower()) if self.connected else None

    def read_by_name(self, name: str) -> Optional[Any]:
        """Liest einen einzelnen Datenpunkt anhand seines Klartext-Namens.

        Args:
            name: Klartext-Name der DID.

        Returns:
            Den gelesenen Wert oder None.
        """
        did = self.lookup_did(name)
        return self.read_did(did) if did is not None else None

    def read_dids_by_name(self, names: List[str]) -> Dict[str, Any]:
        """Liest mehrere Datenpunkte anhand ihrer Klartext-Namen.

        Args:
            names: Liste von Klartext-Namen.

        Returns:
            Dictionary mit Namen als Schluessel und Werten.
        """
        results: Dict[str, Any] = {}
        for name in names:
            did = self.lookup_did(name)
            if did is not None:
                results[name] = self.read_did(did)
            else:
                self.logger.warning(f"Name '{name}' keiner DID zuordenbar.")
                results[name] = None
        return results

    # =========================================================================
    # 5. METADATEN & INSPEKTION (Schema, Codecs & Cache)
    # =========================================================================

    def describe_did(self, did: int) -> Optional[Dict[str, Any]]:
        """Liefert die Struktur einer DID (Sub-Felder, Typen, Einheiten).

        Args:
            did: Die DID-Nummer.

        Returns:
            Codec-Info-Dict oder None.
        """
        dids = self._get_data_identifiers()
        codec = dids.get(did) if dids else None
        get_info = getattr(codec, "getCodecInfo", None) if codec else None
        return get_info() if callable(get_info) else None

    def describe_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Liefert die Struktur einer DID anhand ihres Klartext-Namens.

        Args:
            name: Klartext-Name der DID.

        Returns:
            Codec-Info-Dict oder None.
        """
        did = self.lookup_did(name)
        return self.describe_did(did) if did else None

    def list_dids(self) -> Dict[int, Dict[str, Any]]:
        """Listet alle DIDs des aktuellen DpSets auf.

        Returns:
            Dictionary mit DID-Nummern als Schluessel und Codec-Info-Dicts.
        """
        dids = self._get_data_identifiers()
        if not dids:
            return {}

        return {
            did: get_info()
            for did, codec in sorted(dids.items())
            if (get_info := getattr(codec, "getCodecInfo", None)) and callable(get_info) and get_info()
        }

    def _get_data_identifiers(self) -> Optional[Dict[int, Any]]:
        """Gibt das dataIdentifiers-Dict der O3Eclass-Instanz zurueck."""
        return getattr(self.o3e, "dataIdentifiers", None) if self.o3e else None

    def _build_name_cache(self) -> None:
        """Baut das Name-to-DID Mapping fur schnellen O(1) Zugriff auf."""
        dids = self._get_data_identifiers()
        if not dids:
            self._name_to_did_map.clear()
            return

        self._name_to_did_map = {
            str(codec.id).lower(): did
            for did, codec in dids.items()
            if getattr(codec, "id", None)
        }