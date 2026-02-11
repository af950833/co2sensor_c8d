import asyncio
import logging
import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

DOMAIN = "co2sensor_c8d"

# C8D Query payload (datasheet)
QUERY_PAYLOAD_HEX = "64 69 03 5E 4E"
UNIT_PPM = "ppm"

# Query response frame length (datasheet example: 14 bytes)
RESPONSE_LEN = 14

# Timeouts tuned to avoid HA "Update ... taking over 10 seconds" warnings
CONNECT_TIMEOUT = 2.5
DRAIN_TIMEOUT = 2.0
READ_TIMEOUT = 2.5

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up the CO2 sensor from a config entry.

    IMPORTANT:
    - scan_interval is NOT hard-coded here.
    - It must be provided by config_flow and stored in entry.data (or entry.options).
    """
    sensor = Co2TcpSensor(hass, entry)
    async_add_entities([sensor])

    # scan_interval is the single source of truth: config flow -> entry.data
    scan_seconds = entry.data["scan_interval"]
    scan_interval = datetime.timedelta(seconds=int(scan_seconds))

    async_track_time_interval(hass, sensor.async_update_interval, scan_interval)


class Co2TcpSensor(SensorEntity, RestoreEntity):
    """TCP CO2 sensor (C8D) - Query mode only (14-byte Modbus CRC frame)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self._name = entry.data["name"]
        self._host = entry.data["host"]
        self._port = entry.data["port"]

        self._state = None
        self._unit_of_measurement = UNIT_PPM
        self._payload = QUERY_PAYLOAD_HEX

        # Prevent overlapping updates when network stalls
        self._lock = asyncio.Lock()

    async def async_added_to_hass(self):
        """Restore last state on HA restart."""
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            self._state = last_state.state

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return self._unit_of_measurement

    @property
    def unique_id(self):
        return f"{self._host}_{self._port}"

    async def async_update_interval(self, _now):
        await self.async_update()

    async def async_update(self):
        """Fetch new data. On error, keep previous value."""
        # Avoid stacked updates if previous one is still running
        if self._lock.locked():
            _LOGGER.debug(f"[{self._name}] Previous update still running - skipping this tick")
            return

        writer = None
        async with self._lock:
            try:
                # 1) Connect with timeout
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port),
                    timeout=CONNECT_TIMEOUT,
                )

                # 2) Send query payload
                writer.write(bytes.fromhex(self._payload))
                await asyncio.wait_for(writer.drain(), timeout=DRAIN_TIMEOUT)

                # 3) Read response (prefer exact 14 bytes, else scan buffer)
                data = await self._read_response(reader)

                # Close connection cleanly
                writer.close()
                await writer.wait_closed()
                writer = None

                _LOGGER.debug(f"[{self._name}] Raw Data Received: {data.hex() if data else '<empty>'}")

                if not data:
                    _LOGGER.warning(f"[{self._name}] No Data Received - Keeping Previous Value")
                    return

                new_state = self._process_data(data)
                if new_state is not None and new_state != self._state:
                    self._state = new_state
                    self.async_write_ha_state()

            except asyncio.TimeoutError:
                _LOGGER.error(f"[{self._name}] Timeout - Keeping Previous Value")
            except ConnectionResetError as e:
                _LOGGER.error(f"[{self._name}] TCP Sensor Error: {e} (reset by peer) - Keeping Previous Value")
            except OSError as e:
                # covers Errno 104 and other socket-level issues
                _LOGGER.error(f"[{self._name}] TCP Sensor Error: {e} - Keeping Previous Value")
            except Exception as e:
                _LOGGER.exception(f"[{self._name}] Unexpected Error: {e} - Keeping Previous Value")
            finally:
                if writer is not None:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

    async def _read_response(self, reader: asyncio.StreamReader) -> bytes:
        """
        Obtain a valid query response frame (14 bytes + Modbus CRC).
        - Fast path: readexactly(14) and validate.
        - Fallback: read up to 256 bytes and scan for a valid 14-byte window.
        """
        # Fast path
        try:
            frame = await asyncio.wait_for(reader.readexactly(RESPONSE_LEN), timeout=READ_TIMEOUT)
            if self._is_valid_query_frame(frame):
                return frame
            _LOGGER.debug(f"[{self._name}] 14-byte frame received but invalid CRC, scanning buffer")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            pass

        # Fallback scan
        try:
            buf = await asyncio.wait_for(reader.read(256), timeout=READ_TIMEOUT)
        except asyncio.TimeoutError:
            return b""

        found = self._find_valid_query_frame(buf)
        return found if found is not None else buf

    def _process_data(self, data: bytes):
        """Validate CRC and extract CO2 value."""
        frame = data
        if len(data) != RESPONSE_LEN:
            found = self._find_valid_query_frame(data)
            if found is not None:
                frame = found

        if len(frame) < RESPONSE_LEN:
            _LOGGER.warning(f"[{self._name}] Invalid Data: packet too short ({len(frame)} bytes)")
            return None

        if not self._is_valid_query_frame(frame):
            _LOGGER.error(f"[{self._name}] CRC Error: Received data failed CRC check")
            return None

        return self._extract_co2_value(frame)

    def _is_valid_query_frame(self, frame: bytes) -> bool:
        """Validate a 14-byte query response frame via Modbus CRC-16 (little endian)."""
        if len(frame) != RESPONSE_LEN:
            return False

        # Header sanity (datasheet example shows 0x64 0x69)
        if frame[0] != 0x64 or frame[1] != 0x69:
            return False

        crc_calculated = self._calculate_crc(frame[:-2])
        crc_received = int.from_bytes(frame[-2:], byteorder="little")

        _LOGGER.debug(
            f"[{self._name}] CRC Calculated: {hex(crc_calculated)}, CRC Received: {hex(crc_received)}"
        )
        return crc_calculated == crc_received

    def _find_valid_query_frame(self, buf: bytes):
        """Scan buffer for a valid 14-byte frame (0x64 0x69 ... CRC OK)."""
        if len(buf) < RESPONSE_LEN:
            return None

        for i in range(0, len(buf) - RESPONSE_LEN + 1):
            if buf[i] != 0x64 or buf[i + 1] != 0x69:
                continue
            window = buf[i : i + RESPONSE_LEN]
            if self._is_valid_query_frame(window):
                return window

        return None

    def _calculate_crc(self, data: bytes) -> int:
        """CRC-16 Modbus."""
        crc = 0xFFFF
        polynomial = 0xA001

        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x0001:
                    crc = (crc >> 1) ^ polynomial
                else:
                    crc >>= 1

        return crc & 0xFFFF

    def _extract_co2_value(self, frame: bytes):
        """
        Extract CO2 from query frame.
        Datasheet example indicates: CO2 = BYTE5(high) * 256 + BYTE4(low)
        """
        high_byte = frame[5]
        low_byte = frame[4]
        co2_value = (high_byte * 256) + low_byte
        _LOGGER.debug(f"[{self._name}] Extracted CO2 Value: {co2_value} {UNIT_PPM}")
        return co2_value
