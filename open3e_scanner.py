from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

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

START_COB: int = 0x680
LAST_COB: int = 0x6EF
START_DID: int = 256
LAST_DID: int = 4000


def _is_error(result: Any) -> bool:
    """Prueft, ob ein O3Eclass-Ergebnis einen Fehler signalisiert.

    Args:
        result: Rohergebnis von O3Eclass.readByDid().

    Returns:
        True, wenn das Ergebnis einen 'ERR/'-Fehler enthaelt.
    """
    return (isinstance(result, tuple) and len(result) == 3
            and isinstance(result[1], str) and result[1].startswith("ERR/"))

class Open3EScanner:
    """Spezialisierte Klasse zum Abscannen des CAN-Busses nach verfügbaren ECUs und DIDs."""

    def __init__(self, bus: str = "can0", logger: Optional[logging.Logger] = None) -> None:
        """Initialisiert den Open3E-Scanner.

        Args:
            bus: CAN-Schnittstellenname (z.B. 'can0').
            logger: Logger-Instanz. Wird bei None automatisch erstellt.
        """
        self.bus_name: str = bus
        self.logger: logging.Logger = logger or logging.getLogger("open3e_scanner")
        self.logger.debug("Open3EScanner initialisiert")

    def scan_ecus(self, start_cob: int = START_COB, last_cob: int = LAST_COB, test_did: int = START_DID) -> List[int]:
        """Scannt den COB-ID-Bereich ab und liefert eine Liste aller antwortenden ECUs.

        Args:
            start_cob: Start-COB-ID fuer den Scan.
            last_cob: End-COB-ID fuer den Scan.
            test_did: Test-DID zur Erkennung aktiver ECUs.

        Returns:
            Liste der gefundenen ECU-Adressen (COB-IDs).

        Raises:
            RuntimeError: Wenn die open3e-Bibliothek nicht verfuegbar ist.
        """
        if not HAS_OPEN3E:
            raise RuntimeError("open3e-Bibliothek nicht verfuegbar.")

        found_ecus: List[int] = []
        self.logger.info(
            f"Starte ECU-Scan auf Bus '{self.bus_name}' "
            f"({hex(start_cob)} - {hex(last_cob)})"
        )

        for cob_id in range(start_cob, last_cob + 1):
            response_id = cob_id + 0x10
            o3e: Optional[O3Eclass] = None

            try:
                o3e = O3Eclass(can=self.bus_name, ecutx=cob_id, ecurx=response_id)
                result = o3e.readByDid(test_did, raw=False)

                if _is_error(result):
                    continue

                self.logger.info(f"ECU gefunden bei COB-ID: {hex(cob_id)}")
                found_ecus.append(cob_id)

            except Exception as exc:
                self.logger.debug(f"Keine Antwort von {hex(cob_id)}: {exc}")
            finally:
                if o3e is not None:
                    try:
                        o3e.close()
                    except Exception:
                        pass

        self.logger.info(f"Scan beendet. {len(found_ecus)} ECU(s) gefunden: {found_ecus}")
        return found_ecus

    def scan_ecu_dids(self, cob_id: int, start_did: int = START_DID, last_did: int = LAST_DID) -> Dict[int, Any]:
        """Fragt fuer eine konkrete ECU alle DIDs im angegebenen Bereich ab.

        Args:
            cob_id: Die COB-ID der ECU.
            start_did: Start-DID fuer den Scan.
            last_did: End-DID fuer den Scan.

        Returns:
            Dictionary mit DID-IDs als Schluessel und gelesenen Werten.

        Raises:
            RuntimeError: Wenn die open3e-Bibliothek nicht verfuegbar ist.
        """
        if not HAS_OPEN3E:
            raise RuntimeError("open3e-Bibliothek nicht verfuegbar.")

        ecu_results: Dict[int, Any] = {}
        self.logger.info(
            f"Starte DID-Scan ({start_did}-{last_did}) "
            f"für ECU {hex(cob_id)}"
        )

        o3e: Optional[O3Eclass] = None
        try:
            o3e = O3Eclass(can=self.bus_name, ecutx=cob_id)

            for did in range(start_did, last_did + 1):
                try:
                    result = o3e.readByDid(did, raw=False)
                    if _is_error(result):
                        continue
                    ecu_results[did] = result[0]
                except Exception:
                    pass

        except Exception as exc:
            self.logger.error(f"Fehler beim Scan für ECU {hex(cob_id)}: {exc}")
        finally:
            if o3e is not None:
                try:
                    o3e.close()
                except Exception:
                    pass

        self.logger.info(f"Scan für {hex(cob_id)} beendet. {len(ecu_results)} DIDs gefunden")
        return ecu_results
    