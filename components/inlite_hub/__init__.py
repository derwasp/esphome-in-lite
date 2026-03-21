import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import ble_client, esp32_ble_tracker
from esphome.const import CONF_ID

AUTO_LOAD = ["light", "sensor", "binary_sensor"]
DEPENDENCIES = ["esp32", "ble_client"]
MULTI_CONF = True

CONF_INLITE_HUB_ID = "inlite_hub_id"
CONF_HUB_ID = "hub_id"
CONF_COMMAND_TIMEOUT = "command_timeout"
CONF_RETRIES = "retries"
CONF_POLL_INTERVAL = "poll_interval"
CONF_STATE_REFRESH_INTERVAL = "state_refresh_interval"
CONF_NETWORK_PASSPHRASE_HEX = "network_passphrase_hex"
CONF_AUTO_DISCOVER = "auto_discover"
CONF_DISCOVER_NAME_FILTER = "discover_name_filter"
CONF_DISCOVER_MATCH_ADDRESS = "discover_match_address"

inlite_hub_ns = cg.esphome_ns.namespace("inlite_hub")
InliteHub = inlite_hub_ns.class_(
    "InliteHub",
    cg.PollingComponent,
    ble_client.BLEClientNode,
    esp32_ble_tracker.ESPBTDeviceListener,
)


def _validate_passphrase_hex(value):
    hex_value = value[2:] if value.lower().startswith("0x") else value
    if len(hex_value) == 0:
        raise cv.Invalid("network_passphrase_hex cannot be empty.")
    if len(hex_value) % 2 != 0:
        raise cv.Invalid("network_passphrase_hex must have an even number of characters.")
    if any(c not in "0123456789abcdefABCDEF" for c in hex_value):
        raise cv.Invalid("network_passphrase_hex must contain only hex characters.")
    return value


CONFIG_SCHEMA = (
    ble_client.BLE_CLIENT_SCHEMA.extend(esp32_ble_tracker.ESP_BLE_DEVICE_SCHEMA)
    .extend(
        {
            cv.GenerateID(): cv.declare_id(InliteHub),
            cv.Required(CONF_HUB_ID): cv.int_range(min=0, max=0xFFFF),
            cv.Required(CONF_NETWORK_PASSPHRASE_HEX): cv.All(
                cv.string_strict, _validate_passphrase_hex
            ),
            cv.Optional(CONF_AUTO_DISCOVER, default=True): cv.boolean,
            cv.Optional(CONF_DISCOVER_NAME_FILTER, default="inlite"): cv.string_strict,
            cv.Optional(CONF_DISCOVER_MATCH_ADDRESS): cv.mac_address,
            cv.Optional(
                CONF_COMMAND_TIMEOUT, default="600ms"
            ): cv.positive_time_period_milliseconds,
            cv.Optional(CONF_RETRIES, default=2): cv.int_range(min=0, max=10),
            cv.Optional(
                CONF_POLL_INTERVAL, default="15s"
            ): cv.positive_time_period_milliseconds,
            cv.Optional(
                CONF_STATE_REFRESH_INTERVAL, default="5min"
            ): cv.positive_time_period_milliseconds,
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await ble_client.register_ble_node(var, config)
    await esp32_ble_tracker.register_ble_device(var, config)

    cg.add(var.set_hub_id(config[CONF_HUB_ID]))
    cg.add(var.set_network_passphrase_hex(config[CONF_NETWORK_PASSPHRASE_HEX]))
    cg.add(var.set_auto_discover(config[CONF_AUTO_DISCOVER]))
    cg.add(var.set_discover_name_filter(config[CONF_DISCOVER_NAME_FILTER]))
    if CONF_DISCOVER_MATCH_ADDRESS in config:
        cg.add(var.set_discover_match_address(config[CONF_DISCOVER_MATCH_ADDRESS].as_hex))
    cg.add(var.set_command_timeout(config[CONF_COMMAND_TIMEOUT]))
    cg.add(var.set_retries(config[CONF_RETRIES]))
    cg.add(var.set_update_interval(config[CONF_POLL_INTERVAL]))
    cg.add(var.set_state_refresh_interval(config[CONF_STATE_REFRESH_INTERVAL]))
