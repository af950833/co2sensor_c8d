import asyncio
import logging
import datetime
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

DOMAIN = "co2sensor_c8d"

DEFAULT_PAYLOAD = "64 69 03 5E 4E"
DEFAULT_UNIT = "ppm"
_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """GUI에서 추가된 설정을 기반으로 센서를 생성"""
    sensor = Co2TcpSensor(hass, entry)
    async_add_entities([sensor])

    # scan_interval 적용 (업데이트 주기 자동 실행)
    scan_interval = datetime.timedelta(seconds=entry.data.get("scan_interval", 20))
    async_track_time_interval(hass, sensor.async_update_interval, scan_interval)

class Co2TcpSensor(SensorEntity, RestoreEntity):
    """TCP 센서 (항상 16진수 payload 처리 및 상태 복원 지원)"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """GUI에서 설정된 값 적용"""
        self.hass = hass
        self._name = entry.data["name"]
        self._host = entry.data["host"]
        self._port = entry.data["port"]
        self._scan_interval = entry.data.get("scan_interval", 20)
        self._state = None  # 기본 상태 (재시작 후 복원됨)
        self._unit_of_measurement = DEFAULT_UNIT
        self._payload = DEFAULT_PAYLOAD

    async def async_added_to_hass(self):
        """Home Assistant 재시작 후 마지막 상태 복원"""
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in [None, "unknown", "unavailable"]:
            self._state = last_state.state  # 이전 상태 복원

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
        """고유 ID 설정 (IP + Port를 조합)"""
        return f"{self._host}_{self._port}"

    async def async_update_interval(self, _):
        """scan_interval 주기로 업데이트 실행"""
        await self.async_update()

    async def async_update(self):
        """센서 데이터를 주기적으로 업데이트 (에러 발생 시 기존 값 유지)"""
        try:
            reader, writer = await asyncio.open_connection(self._host, self._port)
            writer.write(bytes.fromhex(self._payload))
            await writer.drain()

            data = await reader.read(1024)
            writer.close()
            await writer.wait_closed()

            # 수신된 데이터 확인
            _LOGGER.debug(f"Raw Data Received: {data.hex()}")

            if data:
                new_state = self._process_data(data)
                if new_state is not None and new_state != self._state:  # ✅ 오류 없는 경우에만 상태 업데이트
                    self._state = new_state
                    self.async_write_ha_state()
            else:
                _LOGGER.warning("No Data Received - Keeping Previous Value")

        except Exception as e:
            _LOGGER.error(f"TCP Sensor Error: {str(e)} - Keeping Previous Value")

    def _process_data(self, data):
        """데이터 수신 후 CRC 검증 및 CO2 값 추출"""
        if len(data) < 4:
            _LOGGER.warning("Invalid Data: CRC Received packet is too short - Keeping Previous Value")
            return

        if not self._validate_crc(data):
            _LOGGER.error("CRC Error: Received data failed CRC check")
            return

        return self._extract_co2_value(data)

    def _validate_crc(self, data):
        """CRC 검증 수행"""
        if len(data) < 4:
            return False

        crc_calculated = self._calculate_crc(data[:-2])  # 마지막 2바이트 제외하고 CRC 계산
        crc_received = int.from_bytes(data[-2:], byteorder="little")  # Modbus 기준: Little Endian

        _LOGGER.debug(f"CRC Calculated: {hex(crc_calculated)}, CRC Received: {hex(crc_received)}")

        return crc_calculated == crc_received

    def _calculate_crc(self, data):
        """CRC-16 Modbus 계산 (비트 연산 방식)"""
        crc = 0xFFFF
        polynomial = 0xA001  # Modbus 표준 다항식

        for byte in data:
            crc ^= byte  # 바이트와 XOR 연산
            for _ in range(8):  # 8비트씩 XOR 연산
                if crc & 0x0001:
                    crc = (crc >> 1) ^ polynomial
                else:
                    crc >>= 1

        return crc & 0xFFFF  # 16비트 값 반환

    def _extract_co2_value(self, data):
        """Modbus 데이터에서 CO2 값을 올바른 위치에서 추출"""
        if len(data) < 12:
            return "Invalid Data"

        high_byte = data[5]
        low_byte = data[4]

        co2_value = (high_byte * 256) + low_byte

        _LOGGER.debug(f"Updated Extracted CO2 Value: {co2_value} ppm")

        return co2_value
