import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import sensor
from esphome.const import ENTITY_CATEGORY_DIAGNOSTIC, UNIT_DECIBEL_MILLIWATT

from .. import CONF_INLITE_HUB_ID, InliteHub

DEPENDENCIES = ["inlite_hub"]

CONF_RSSI = "rssi"
CONF_LAST_COMMAND_STATUS = "last_command_status"

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(CONF_INLITE_HUB_ID): cv.use_id(InliteHub),
            cv.Optional(CONF_RSSI): sensor.sensor_schema(
                unit_of_measurement=UNIT_DECIBEL_MILLIWATT,
                accuracy_decimals=0,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
                icon="mdi:bluetooth",
            ),
            cv.Optional(CONF_LAST_COMMAND_STATUS): sensor.sensor_schema(
                accuracy_decimals=0,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
                icon="mdi:check-circle-outline",
            ),
        }
    ).extend(cv.COMPONENT_SCHEMA)
)


async def to_code(config):
    hub = await cg.get_variable(config[CONF_INLITE_HUB_ID])

    if rssi_conf := config.get(CONF_RSSI):
        sens = await sensor.new_sensor(rssi_conf)
        cg.add(hub.set_rssi_sensor(sens))

    if status_conf := config.get(CONF_LAST_COMMAND_STATUS):
        sens = await sensor.new_sensor(status_conf)
        cg.add(hub.set_last_command_status_sensor(sens))
