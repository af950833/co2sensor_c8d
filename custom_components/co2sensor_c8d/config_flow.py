import voluptuous as vol
from homeassistant import config_entries

DOMAIN = "co2sensor_c8d"

class HexTcpConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """TCP 센서 설정 GUI 지원"""

    async def async_step_user(self, user_input=None):
        """사용자가 GUI에서 설정한 값 처리"""
        errors = {}
        if user_input is not None:
            return self.async_create_entry(title=user_input["name"], data=user_input)

        data_schema = vol.Schema({
            vol.Required("name"): str,
            vol.Required("host"): str,
            vol.Required("port", default=8899): int,
            vol.Optional("scan_interval", default=20): int,
        })

        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)
