from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


EcuInfo = Tuple[int, int, str]
"""Tuple: (cob_id, devprop_byte, devprop_name)"""

DidInfo = Tuple[int, int, Any]
"""Tuple: (did, data_length, raw_data)"""


class Open3EScanner:
    """Spezialisierte Klasse zum Abscannen des CAN-Busses nach verfügbaren ECUs und DIDs."""

    SCANNER_VERSION = '0.0.2'

    def __init__(self, bus: str = "can0", logger: Optional[logging.Logger] = None) -> None:
        """Initialisiert den Open3E-Scanner.

        Args:
            bus: CAN-Schnittstellenname (z.B. 'can0').
            logger: Logger-Instanz. Wird bei None automatisch erstellt.
        """
        self.bus_name: str = bus
        self.logger: logging.Logger = logger or logging.getLogger("open3e_scanner")
        self.logger.debug("Open3EScanner initialisiert")
        
        self._cached_did_variants: Optional[Dict[int, Dict[int, Any]]] = None
        self._cached_global_dids: Optional[Dict[int, Any]] = None
        self._cached_did_enums: Optional[Dict[int, str]] = None
        self._did_info_cache: Dict[int, Tuple[int, str]] = {}

    def scan_ecus(self, start_cob: int = START_COB, last_cob: int = LAST_COB, test_did: int = START_DID) -> List[EcuInfo]:
        """Scannt den COB-ID-Bereich ab und liefert eine Liste aller antwortenden ECUs.

        Args:
            start_cob: Start-COB-ID fuer den Scan.
            last_cob: End-COB-ID fuer den Scan.
            test_did: Test-DID zur Erkennung aktiver ECUs.

        Returns:
            Liste von Tupeln (cob_id, devprop_byte, devprop_name).

        Raises:
            RuntimeError: Wenn die open3e-Bibliothek nicht verfuegbar ist.
        """
        if not HAS_OPEN3E:
            raise RuntimeError("open3e-Bibliothek nicht verfuegbar.")

        found_ecus: List[EcuInfo] = []
        self.logger.info(
            f"Starte ECU-Scan auf Bus '{self.bus_name}' "
            f"({hex(start_cob)} - {hex(last_cob)})"
        )

        try:
            from open3e.Open3Eenums import E3Enums
            devices = E3Enums.get('Devices', {})
        except Exception:
            devices = {}

        for cob_id in range(start_cob, last_cob + 1):
            response_id = cob_id + 0x10
            o3e: Optional[O3Eclass] = None

            try:
                o3e = O3Eclass(can=self.bus_name, ecutx=cob_id, ecurx=response_id)
                result = o3e.readByDid(test_did, raw=False)

                if _is_error(result):
                    continue

                iprop = result[0]
                devprop_name = devices.get(iprop, str(iprop))
                self.logger.info(f"ECU gefunden bei COB-ID: {hex(cob_id)} ({devprop_name})")
                found_ecus.append((cob_id, iprop, devprop_name))

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

    def scan_ecu_dids(self, cob_id: int, start_did: int = START_DID, last_did: int = LAST_DID) -> List[DidInfo]:
        """Fragt fuer eine konkrete ECU alle DIDs im angegebenen Bereich ab.

        Args:
            cob_id: Die COB-ID der ECU.
            start_did: Start-DID fuer den Scan.
            last_did: End-DID fuer den Scan.

        Returns:
            Liste von Tupeln (did, data_length, raw_data).

        Raises:
            RuntimeError: Wenn die open3e-Bibliothek nicht verfuegbar ist.
        """
        if not HAS_OPEN3E:
            raise RuntimeError("open3e-Bibliothek nicht verfuegbar.")

        ecu_results: List[DidInfo] = []
        self.logger.info(
            f"Starte DID-Scan ({start_did}-{last_did}) "
            f"für ECU {hex(cob_id)}"
        )

        o3e: Optional[O3Eclass] = None
        try:
            o3e = O3Eclass(can=self.bus_name, ecutx=cob_id)

            for did in range(start_did, last_did + 1):
                try:
                    result = o3e.readByDid(did, raw=True)
                    if _is_error(result):
                        continue
                    if isinstance(result, tuple) and len(result) >= 3:
                        value, idstr, did_num = result
                        if hasattr(value, 'hex'):
                            raw_data = value.hex()
                            dlen = len(value)
                        elif isinstance(value, bytes):
                            raw_data = value.decode('utf-8', errors='replace')
                            dlen = len(value)
                        else:
                            raw_data = str(value)
                            dlen = len(str(value)) if hasattr(value, '__len__') else 0
                        ecu_results.append((did, dlen, value))
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

    # =========================================================================
    # 3. DATEIEN SCHREIBEN
    # =========================================================================

    def write_devices_json(self, ecus: List[EcuInfo], config_dir: Path) -> None:
        """Schreibt eine devices.json Datei mit den gefundenen ECUs.

        Args:
            ecus: Liste von Tupeln (cob_id, devprop_byte, devprop_name).
            config_dir: Verzeichnis für die Ausgabedatei.
        """
        json_path = config_dir / "devices.json"
        result_dict = {}

        for cob_id, devprop_byte, devprop_name in ecus:
            hex_id = f"0x{cob_id:03x}"
            dp_file = f"Open3Edatapoints_{cob_id:03x}.py"

            result_dict[hex_id] = {
                'tx': hex_id,
                'dpList': dp_file,
                'prop': devprop_name
            }

        try:
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(result_dict, f, indent=2)
            self.logger.info(f"devices.json geschrieben mit {len(result_dict)} Geräten")
        except Exception as exc:
            self.logger.error(f"Schreiben der devices.json fehlgeschlagen: {exc}")

    def write_datapoints_file(self, dids: List[DidInfo], cob_id: int, devprop: str, config_dir: Path) -> None:
        """Schreibt eine Datapoints-Python-Datei für eine ECU.

        Args:
            dids: Liste von Tupeln (did, dlen, raw_data).
            cob_id: COB-ID der ECU.
            devprop: Geräte-Property (Gerätename).
            config_dir: Verzeichnis für die Ausgabedatei.
        """
        filename = f"Open3Edatapoints_{cob_id:03x}.py"
        filepath = config_dir / filename
        variants = self._get_did_variants()
        data_identifiers = self._get_global_data_identifiers()

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write('from open3e.Open3Ecodecs import *\n\n')
                f.write('dataIdentifiers = {\n')
                f.write(f'    "name": "{devprop}",\n')
                f.write('    "dids" :\n')
                f.write('    {\n')

                for did, dlen, raw_data in dids:
                    genlen, idstr = self._did_info(did, data_identifiers)

                    if dlen == genlen:
                        codec_str = 'None,'
                    else:
                        varitem = self._get_variant_codec(did, dlen, variants)
                        if varitem:
                            codec_str = varitem + ','
                        else:
                            codec_str = f'RawCodec({dlen}, "{idstr}"),'

                    f.write(f'        {did} : {codec_str}\n')

                f.write('    }\n')
                f.write('}\n')

            self.logger.info(f"Datapoints-Datei geschrieben: {filename} ({len(dids)} DIDs)")
        except Exception as exc:
            self.logger.error(f"Schreiben der Datapoints-Datei fehlgeschlagen: {exc}")

    def _get_did_variants(self) -> Dict[int, Dict[int, Any]]:
        """Lädt die DID-Varianten aus Open3EdatapointsVariants.py (cached)."""
        if self._cached_did_variants is not None:
            return self._cached_did_variants
        
        try:
            import open3e
            import importlib.util

            o3e_path = Path(open3e.Open3Edatapoints.__file__).parent
            variants_file = o3e_path / "Open3EdatapointsVariants.py"

            if not variants_file.is_file():
                self._cached_did_variants = {}
                return self._cached_did_variants

            spec = importlib.util.spec_from_file_location("Open3EdatapointsVariants", variants_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            self._cached_did_variants = module.dataIdentifiers.get("dids", {})
            return self._cached_did_variants
        except Exception:
            self._cached_did_variants = {}
            return self._cached_did_variants

    def _get_variant_codec(self, did: int, dlen: int, variants: Dict[int, Dict[int, Any]]) -> Optional[str]:
        """Gibt den Codec-String für eine DID-Variante zurück."""
        if did in variants and dlen in variants[did]:
            varitem = variants[did][dlen]
            get_codec_string = getattr(varitem, 'getCodecString', None)
            if callable(get_codec_string):
                return get_codec_string()
        return None

    def _load_did_enums(self) -> Dict[int, str]:
        """Lädt DidEnums.txt einmalig und cached das Ergebnis."""
        if self._cached_did_enums is not None:
            return self._cached_did_enums
        
        self._cached_did_enums = {}
        try:
            import open3e
            o3e_path = Path(open3e.Open3Edatapoints.__file__).parent
            enums_file = o3e_path / "DidEnums.txt"
            if enums_file.is_file():
                with open(enums_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if '(' in line:
                            parts = line.split('(')
                            if len(parts) == 2:
                                name = parts[0].strip()
                                num_str = parts[1].split(')')[0].strip()
                                try:
                                    self._cached_did_enums[int(num_str)] = name
                                except ValueError:
                                    pass
        except Exception:
            pass
        return self._cached_did_enums

    def _get_global_data_identifiers(self) -> Dict[int, Any]:
        """Lädt die globalen dataIdentifiers aus Open3Edatapoints.py (cached)."""
        if self._cached_global_dids is not None:
            return self._cached_global_dids
        
        try:
            from open3e.Open3Edatapoints import dataIdentifiers
            self._cached_global_dids = dataIdentifiers.get("dids", {})
            return self._cached_global_dids
        except Exception:
            self._cached_global_dids = {}
            return self._cached_global_dids

    def _did_info(self, did: int, data_identifiers: Dict[int, Any]) -> Tuple[int, str]:
        """Gibt (genlen, idstr) für eine DID zurück (cached)."""
        if did in self._did_info_cache:
            return self._did_info_cache[did]

        if did in data_identifiers:
            codec = data_identifiers[did]
            genlen = getattr(codec, 'string_len', 0)
            idstr = getattr(codec, 'id', str(did))
        else:
            enums = self._load_did_enums()
            if did in enums:
                genlen, idstr = 0, enums[did]
            else:
                genlen, idstr = 0, "Unknown"

        self._did_info_cache[did] = (genlen, idstr)
        return genlen, idstr

    def reset(self) -> None:
        """Setzt alle gecachten Daten zurück."""
        self._cached_did_variants = None
        self._cached_global_dids = None
        self._cached_did_enums = None
        self._did_info_cache.clear()
        self.logger.debug("Scanner-Cache zurückgesetzt")
    