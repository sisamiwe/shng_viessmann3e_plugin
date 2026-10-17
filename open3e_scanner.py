from __future__ import annotations

import sys
import binascii
import importlib.util
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# WORKAROUND: Blockade von SmartHomeNG durch udsoncan.setup_logging verhindern
try:
    import udsoncan
    udsoncan.setup_logging = lambda *args, **kwargs: None
    from udsoncan.client import Client as UdsClient
    from udsoncan.services import ReadDataByIdentifier
    from udsoncan.connections import PythonIsoTpConnection
except ImportError:
    pass

import can
from can.interfaces.socketcan import SocketcanBus
import isotp

try:
    import open3e.Open3Edatapoints
    import open3e.Open3Eenums
    HAS_OPEN3E = True
except Exception:
    HAS_OPEN3E = False

DEFAULT_STARTCOB = 0x680
DEFAULT_LASTCOB = 0x6ef
DEFAULT_STARTDID = 256
DEFAULT_LASTDID = 4000
DEFAULT_MAX_WORKERS = 32
DEFAULT_DELAY_MS = 20

@dataclass
class EcuInfo:
    tx: int
    devprop: str


class SharedCanContext:
    """One shared SocketcanBus + can.Notifier for all probed/scanned addresses."""

    def __init__(self, channel: str):
        """Initialize shared CAN context with SocketcanBus and Notifier.

        Args:
            channel: CAN interface name (e.g., 'can0').
        """
        self.bus = SocketcanBus(channel=channel, bitrate=250000)
        self.notifier = can.Notifier(self.bus, listeners=[], timeout=1.0)
        self.channel = channel

    def open_stack(self, ecutx: int, ecurx: int) -> isotp.NotifierBasedCanStack:
        """Create an ISOTP stack for communication with an ECU.

        Args:
            ecutx: ECU transmission CAN ID.
            ecurx: ECU reception CAN ID.

        Returns:
            isotp.NotifierBasedCanStack: Configured ISOTP stack.
        """
        tp_addr = isotp.Address(isotp.AddressingMode.Normal_11bits, txid=ecutx, rxid=ecurx)
        stack = isotp.NotifierBasedCanStack(
            bus=self.bus, notifier=self.notifier, address=tp_addr, params=isotp.socketcan.CyclicSendTask.get_default_params() if hasattr(isotp, "socketcan") else {}
        )
        return stack

    def close(self) -> None:
        """Stop the notifier and shutdown the CAN bus."""
        self.notifier.stop()
        self.bus.shutdown()


def make_client_config(p2_timeout: Optional[float] = None) -> dict:
    """Create a udsoncan client configuration dictionary.

    Args:
        p2_timeout: Optional P2 timeout override in seconds.

    Returns:
        dict: Client configuration dictionary.
    """
    config = dict(udsoncan.configs.default_client_config)
    if p2_timeout is not None:
        config['p2_timeout'] = p2_timeout
    return config


class Open3EScanner:
    """Spezialisierte Klasse zum Abscannen des CAN-Busses nach verfügbaren ECUs und DIDs."""

    SCANNER_VERSION = '0.2.0'

    def __init__(self, bus: str = "can0", logger: Optional[logging.Logger] = None, write_simul_file: bool = True) -> None:
        """Initialize the Open3E scanner.

        Args:
            bus: CAN interface name (default: 'can0').
            logger: Optional logger instance.
            write_simul_file: Whether to write simulation data files (default: True).
        """
        self.bus_name: str = bus
        self.logger: logging.Logger = logger or logging.getLogger("open3e_scanner")
        self.logger.debug("Open3EScanner initialisiert")

        self.write_simul_file: bool = write_simul_file

    # ~~~~~~~~~~~~~~~~~~~~~~ utils ~~~~~~~~~~~~~~~~~~~~~~

    def read_didenums(self, path: Path) -> dict:
        """Read DID enumerations from a text file.

        Parses a file with format 'Name(ID)' lines and returns a dictionary
        mapping numeric IDs to names.

        Args:
            path: Path to the DidEnums.txt file.

        Returns:
            dict: Mapping of DID numbers to descriptive names.
        """
        dicenums = {}
        lines = []
        self.logger.info("read DID enums ...")
        try:
            with open(path, 'r') as file:
                lines = file.readlines()
        except Exception as ex:
            self.logger.warning("could not read DID enums file %s: %s", path, ex)

        for line in lines:
            line = line.strip()
            if line:
                parts = line.split('(')
                if len(parts) == 2:
                    name = parts[0].strip()
                    num_str = parts[1].split(')')[0].strip()
                    try:
                        num = int(num_str)
                        dicenums[num] = name
                    except ValueError:
                        self.logger.debug("could not parse '%s' as an integer", num_str)
        self.logger.info("%d DIDs listed.", len(dicenums))
        return dicenums

    def get_variants_dict(self, o3e_path: Path) -> dict:
        """Load codec variants from Open3EdatapointsVariants.py if present.

        Args:
            o3e_path: Path to the open3e package directory.

        Returns:
            dict: Dictionary of DID variants or empty dict if file not found.
        """
        module_name = "Open3EdatapointsVariants"
        source = Path(o3e_path, f"{module_name}.py")
        if not source.is_file():
            self.logger.info("no codec variants - %s.py not found", module_name)
            return {}
        try:
            spec = importlib.util.spec_from_file_location(module_name, source)
            variants_temp = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(variants_temp)
            return variants_temp.dataIdentifiers["dids"]
        except Exception as e:
            self.logger.error("ERROR getting DID variants: %s", e)
            return {}
        finally:
            if module_name in sys.modules:
                del sys.modules[module_name]

    def prop_str(self, iprop: int, e3_devices: dict) -> str:
        """Resolve device property ID to a human-readable name.

        Args:
            iprop: Device property ID (integer).
            e3_devices: Dictionary mapping property IDs to device names.

        Returns:
            str: Device name or 'Unknown Device (iprop)' if not found.
        """
        return e3_devices.get(iprop, f"Unknown Device ({iprop})")

    @staticmethod
    def did_info(did: int, data_identifiers: dict, did_enums: dict) -> Tuple[int, str]:
        """Get the expected length and identifier string for a DID.

        Args:
            did: Data Identifier to look up.
            data_identifiers: Dictionary of known DIDs from Open3E.
            did_enums: Dictionary of DID enum names.

        Returns:
            Tuple[int, str]: (expected_length, identifier_string).
        """
        if did in data_identifiers:
            didlen = vars(data_identifiers[did])['string_len']
            idstr = vars(data_identifiers[did])['id']
            return (didlen, idstr)
        elif did in did_enums:
            return (0, did_enums[did])
        else:
            return (0, "Unknown")

    @staticmethod
    def shex(nbr: int) -> str:
        """Convert an integer to a 3-digit lowercase hex string.

        Args:
            nbr: Integer to convert.

        Returns:
            str: Zero-padded 3-character hex string (e.g., '680').
        """
        return format(nbr, '03x')

    # ~~~~~~~~~~~~~~~~~~~~~~ file writers ~~~~~~~~~~~~~~~~~~~~~~

    def write_devices_json(self, ecus: list, config_dir: Path) -> None:
        """Write discovered ECUs to a JSON configuration file.

        Args:
            ecus: List of EcuInfo objects to write.
            config_dir: Directory where devices.json will be written.
        """
        json_path = config_dir / "devices.json"

        self.logger.info("[SCANNER] write %s ...", json_path)
        result_dict = {}

        for ecu in sorted(ecus, key=lambda e: e.tx):
            scob = hex(ecu.tx)
            sdplist = "Open3Edatapoints_" + self.shex(ecu.tx) + ".py"
            result_dict[scob] = {'tx': scob, 'dpList': sdplist, 'prop': ecu.devprop}

        try:
            with open(json_path, 'w') as json_file:
                json.dump(result_dict, json_file, indent=2)
            self.logger.info("[SCANNER] devices.json geschrieben mit %d Geräten", len(result_dict))
        except Exception as exc:
            self.logger.error("[SCANNER] Schreiben der devices.json fehlgeschlagen: %s", exc)

    def write_simul_datafile(self, lstdids: list, cobid: int, devprop: str, config_dir: Path) -> None:
        """Write simulation data file for debugging/replay purposes.

        Args:
            lstdids: List of (did, dlen, data) tuples.
            cobid: CAN ID of the ECU.
            devprop: Device property string.
            config_dir: Directory where the file will be written.
        """
        filename = config_dir / ("virtdata_" + self.shex(cobid) + ".txt")

        self.logger.info("[SCANNER] write simulation data file %s ...", filename)

        with open(filename, "w") as file:
            file.write(f"# {hex(cobid)}:{devprop}\n")
            for did, dlen, data in lstdids:
                sdata = binascii.hexlify(data).decode('utf-8')
                file.write(str(did) + " " + sdata + "\n")
        self.logger.info("[SCANNER] writing simulation file done.")

    def write_datapoints_file(self, lstdids: list, cobid: int, devprop: str, dicvariants: dict, data_identifiers: dict, did_enums: dict, config_dir: Path) -> None:
        """Write Python datapoints file for the discovered DIDs.

        Args:
            lstdids: List of (did, dlen, data) tuples.
            cobid: CAN ID of the ECU.
            devprop: Device property string.
            dicvariants: Dictionary of codec variants.
            data_identifiers: Dictionary of known DIDs.
            did_enums: Dictionary of DID enum names.
            config_dir: Directory where the file will be written.
        """
        filename = config_dir / ("Open3Edatapoints_" + self.shex(cobid) + ".py")
        
        self.logger.info("[SCANNER] write datapoints file %s ...", filename)
        
        with open(filename, "w") as file:
            shead = 'from open3e.Open3Ecodecs import *\n\n'
            shead += 'dataIdentifiers = {\n'
            shead += '    \"name\": \"' + str(devprop) + '\",\n'
            shead += '    \"dids\" :\n'
            shead += '    {\n'
            file.write(shead)
            for did, dlen, data in lstdids:
                sline = '        ' + str(did) + " : "
                genlen, idstr = self.did_info(did, data_identifiers, did_enums)
                if dlen == genlen:
                    sline += 'None,'
                elif (varitem := dicvariants.get(did, {}).get(dlen)):
                    sline += varitem.getCodecString() + ','
                else:
                    sline += 'RawCodec(' + str(dlen) + ', \"' + idstr + '\"),'
                file.write(sline + '\n')
            file.write('    }\n')
            file.write('}\n')
        self.logger.info("[SCANNER] writing datapoints file done.")

    # ~~~~~~~~~~~~~~~~~~~~~~ scan workers ~~~~~~~~~~~~~~~~~~~~~~

    def probe_ecu(self, shared: SharedCanContext, tx: int, e3_devices: dict) -> Optional[EcuInfo]:
        """Probe one ECU address for DID 256 (BusIdentification) via SocketCAN.

        Args:
            shared: Shared CAN context for SocketCAN communication.
            tx: Transmission CAN ID of the ECU being probed.
            e3_devices: Dictionary containing device property mappings.

        Returns:
            Optional[EcuInfo]: An EcuInfo object containing the ECU transmission ID 
            and resolved device property if it responds successfully; otherwise, None.
        """

        rx, t0 = tx + 0x10, time.monotonic()
        try:
            conn = PythonIsoTpConnection(shared.open_stack(tx, rx))
            with UdsClient(conn, config=make_client_config()) as client:
                try:
                    response = client.send_request(udsoncan.Request(service=ReadDataByIdentifier, data=b"\x01\x00"))
                except udsoncan.exceptions.TimeoutException:
                    self.logger.debug("[SCANNER] probe %s: no response (%.3fs)", hex(tx), time.monotonic() - t0)
                    return None
                except udsoncan.exceptions.NegativeResponseException as ex:
                    self.logger.debug("[SCANNER] probe %s: negative response %s (%.3fs)", hex(tx), getattr(ex.response, "code", "unknown"), time.monotonic() - t0)
                    return None

            if not response.positive:
                self.logger.debug("[SCANNER] probe %s: negative response %s", hex(tx), response.code)
                return None

            try:
                iprop = response.data[4]
            except IndexError:
                self.logger.warning("[SCANNER] probe %s: malformed positive response (too short): %r", hex(tx), response.data)
                return None

            devprop = self.prop_str(iprop, e3_devices)
            self.logger.info("[SCANNER] ECU found: %s : %s (%.3fs)", hex(tx), devprop, time.monotonic() - t0)
            return EcuInfo(tx=tx, devprop=devprop)
        except Exception as e:
            self.logger.warning("[SCANNER] probe %s failed: %s", hex(tx), e)
            return None
    
    def scan_ecu_dids(self, shared: SharedCanContext, tx: int, startdid: int, lastdid: int, delay_ms: int = 0) -> List[Tuple[int, int, bytes]]:
        """Scan DIDs startdid..lastdid on one ECU via SocketCAN.

        Args:
            shared: Shared CAN context for SocketCAN communication.
            tx: Transmission CAN ID of the ECU being scanned.
            startdid: Starting Data Identifier (DID).
            lastdid: Ending Data Identifier (DID).
            delay_ms: Optional pacing pause in milliseconds between requests.

        Returns:
            List[Tuple[int, int, bytes]]: A list of tuples containing found (did, dlen, data).
        """
        rx, lstfounds = tx + 0x10, []
        try:
            conn = PythonIsoTpConnection(shared.open_stack(tx, rx))
            with UdsClient(conn, config=make_client_config(p2_timeout=3)) as client:
                for did in range(startdid, lastdid + 1):
                    t0 = time.monotonic()
                    try:
                        response = client.send_request(udsoncan.Request(service=ReadDataByIdentifier, data=did.to_bytes(2, byteorder='big')))
                        if response.positive:
                            dlen = len(response) - 3
                            if dlen > 0:
                                data = response.data[2:]
                                lstfounds.append((did, dlen, data))
                                self.logger.debug("[SCANNER] DID %s/%d: found len=%d (%.3fs)", hex(tx), did, dlen, time.monotonic() - t0)
                            else:
                                self.logger.debug("[SCANNER] DID %s/%d: empty positive response, treated as not present (%.3fs)", hex(tx), did, time.monotonic() - t0)
                    except udsoncan.exceptions.NegativeResponseException:
                        self.logger.debug("[SCANNER] DID %s/%d: not present (%.3fs)", hex(tx), did, time.monotonic() - t0)
                    except Exception as e:
                        self.logger.warning("[SCANNER] DID %s/%d: error %s", hex(tx), did, e)
                    
                    if delay_ms > 0:
                        time.sleep(delay_ms / 1000.0)
        except Exception as e:
            self.logger.error("[SCANNER] ECU %s: scan setup failed: %s", hex(tx), e)
            return []

        self.logger.info("[SCANNER] %d DIDs found on %s.", len(lstfounds), hex(tx))
        return lstfounds

    # ~~~~~~~~~~~~~~~~~~~~~~ orchestration ~~~~~~~~~~~~~~~~~~~~~~

    def run_discovery(
        self,
        startcob: int,
        lastcob: int,
        shared: SharedCanContext,
        workers: int,
        e3_devices: dict,
    ) -> List[EcuInfo]:
        """Run discovery to scan a range of CAN IDs for active ECUs via SocketCAN.

        Args:
            startcob: Starting CAN ID.
            lastcob: Ending CAN ID.
            shared: Shared CAN context for SocketCAN communication.
            workers: Maximum number of worker threads.
            e3_devices: Dictionary containing device property mappings.

        Returns:
            List[EcuInfo]: A sorted list of EcuInfo objects representing responding ECUs.
        """
        addrs, found = list(range(startcob, lastcob + 1)), []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe") as ex:
            futures = {ex.submit(self.probe_ecu, shared, tx, e3_devices): tx for tx in addrs}
            for fut in as_completed(futures):
                info = fut.result()
                if info is not None:
                    found.append(info)
        found.sort(key=lambda e: e.tx)
        return found

    def run_did_scans(
        self,
        ecus: list,
        shared: SharedCanContext,
        startdid: int,
        lastdid: int,
        workers: int,
        delay_ms: int = 0,
    ) -> Dict[int, List[Tuple[int, int, bytes]]]:
        """Run DID scans across multiple ECUs concurrently via SocketCAN.

        Args:
            ecus: List of EcuInfo objects to scan.
            shared: Shared CAN context for SocketCAN communication.
            startdid: Starting Data Identifier (DID).
            lastdid: Ending Data Identifier (DID).
            workers: Maximum number of worker threads.
            delay_ms: Optional pacing pause in milliseconds between requests.

        Returns:
            Dict[int, List[Tuple[int, int, bytes]]]: A dictionary mapping ECU transmission IDs to their found DIDs.
        """
        results = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="didscan") as ex:
            futures = {ex.submit(self.scan_ecu_dids, shared, e.tx, startdid, lastdid, delay_ms): e for e in ecus}
            for fut in as_completed(futures):
                e = futures[fut]
                try:
                    results[e.tx] = fut.result()
                except Exception as e_inner:
                    self.logger.error("ECU %s: DID scan failed: %s", hex(e.tx), e_inner)
        return results

    # ~~~~~~~~~~~~~~~~~~~~~~ depict ~~~~~~~~~~~~~~~~~~~~~~

    def depict_system(self, config_dir: Path) -> None:
        """Depict the system by scanning the CAN bus for active ECUs and their DIDs, 
        then writing configuration and data files.

        Args:
            config_dir: Directory path where output configuration files are stored.
        """
        if not HAS_OPEN3E:
            self.logger.error("Open3E package is missing. Cannot depict system.")
            return

        t_start = time.monotonic()
        data_identifiers = dict(open3e.Open3Edatapoints.dataIdentifiers["dids"])
        e3_devices = open3e.Open3Eenums.E3Enums['Devices']

        open3e_path = Path(open3e.Open3Eenums.__file__).parent
        did_enums = self.read_didenums(open3e_path / "DidEnums.txt")
        did_variants = self.get_variants_dict(open3e_path)

        shared = SharedCanContext(self.bus_name)
        try:
            discovery_workers = min(DEFAULT_MAX_WORKERS, DEFAULT_LASTCOB - DEFAULT_STARTCOB + 1)
            ecus = self.run_discovery(DEFAULT_STARTCOB, DEFAULT_LASTCOB, shared, discovery_workers, e3_devices)
            self.logger.info("[SCANNER] %d responding ECUs found.", len(ecus))

            self.write_devices_json(ecus, config_dir=config_dir)
            
            did_workers = min(DEFAULT_MAX_WORKERS, max(len(ecus), 1))
            results = self.run_did_scans(ecus, shared, DEFAULT_STARTDID, DEFAULT_LASTDID, did_workers, delay_ms=DEFAULT_DELAY_MS)

            for e in ecus:
                lstdids = results.get(e.tx, [])
                if self.write_simul_file:
                    self.write_simul_datafile(lstdids, e.tx, e.devprop, config_dir=config_dir)
                self.write_datapoints_file(lstdids, e.tx, e.devprop, did_variants, data_identifiers, did_enums, config_dir=config_dir)
        
            elapsed = time.monotonic() - t_start
            self.logger.info("[SCANNER] depict system finalized within %ds with: ecus=%s, results=%s", int(elapsed), ecus, results)

        finally:
            shared.close()