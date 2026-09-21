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
LAST_DID: int = 266  # 4000

class Open3EScanner:
    """Spezialisierte Klasse zum Abscannen des CAN-Busses nach verfügbaren ECUs und DIDs."""

    def __init__(self, bus: str = "can0", logger: Optional[logging.Logger] = None) -> None:
        self.bus_name: str = bus
        self.logger: logging.Logger = logger or logging.getLogger("open3e_scanner")
        self.logger.info("[Open3EScanner]")

    def scan_ecus(self, start_cob: int = START_COB, last_cob: int = LAST_COB, test_did: int = START_DID) -> List[int]:
        """
        Scannt den COB-ID-Bereich ab und liefert eine Liste aller antwortenden ECUs zurück.
        """

        found_ecus: List[int] = []
        self.logger.info(
            f"[Open3EScanner] Starte ECU-Scan auf Bus '{self.bus_name}' "
            f"({hex(start_cob)} - {hex(last_cob)})..."
        )

        for cob_id in range(start_cob, last_cob + 1):
            response_id = cob_id + 0x10
            temp_o3e: Optional[O3Eclass] = None

            try:
                temp_o3e = O3Eclass(can=self.bus_name, ecutx=cob_id, ecurx=response_id)
                val = temp_o3e.readByDid(test_did, raw=False)

                if val is not None:
                    # UDS-Fehler abfangen
                    if isinstance(val, tuple) and len(val) == 3 and isinstance(val[1], str) and val[1].startswith("ERR/"):
                        continue

                    self.logger.info(f"[Open3EScanner] ECU gefunden bei COB-ID: {hex(cob_id)}")
                    found_ecus.append(cob_id)

            except Exception as exc:
                self.logger.debug(f"[Open3EScanner] Keine Antwort von {hex(cob_id)}: {exc}")
            finally:
                if temp_o3e and hasattr(temp_o3e, "close"):
                    try:
                        temp_o3e.close()
                    except Exception:
                        pass

        self.logger.info(f"[Open3EScanner] Scan beendet. {len(found_ecus)} ECU(s) gefunden: {found_ecus}")
        return found_ecus

    def scan_dids_of_ecu(self, cob_id: int, start_did: int = START_DID, last_did: int = LAST_DID) -> Dict[int, Any]:
        """
        Fragt für eine konkrete ECU alle DIDs im angegebenen Bereich ab.
        """

        ecu_results: Dict[int, Any] = {}

        self.logger.info(
            f"[Open3EScanner] Starte DID-Scan ({start_did}-{last_did}) "
            f"für ECU {hex(cob_id)}..."
        )

        temp_o3e: Optional[O3Eclass] = None
        try:
            temp_o3e = O3Eclass(can=self.bus_name, ecutx=cob_id)

            for did in range(start_did, last_did + 1):
                try:
                    result = temp_o3e.readByDid(did, raw=False)

                    if isinstance(result, tuple) and len(result) == 3:
                        val, idstr, _ = result
                        if isinstance(idstr, str) and idstr.startswith("ERR/"):
                            continue
                        ecu_results[did] = val
                    elif result is not None:
                        ecu_results[did] = result

                except Exception:
                    pass

        except Exception as exc:
            self.logger.error(f"[Open3EScanner] Fehler beim Scan für ECU {hex(cob_id)}: {exc}")
        finally:
            if temp_o3e and hasattr(temp_o3e, "close"):
                try:
                    temp_o3e.close()
                except Exception:
                    pass

        self.logger.info(f"[Open3EScanner] Scan für {hex(cob_id)} beendet. {len(ecu_results)} DIDs gefunden: {ecu_results}")
        return ecu_results
    