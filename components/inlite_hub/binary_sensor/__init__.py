import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import binary_sensor
from esphome.const import DEVICE_CLASS_CONNECTIVITY, ENTITY_CATEGORY_DIAGNOSTIC

from .. import CONF_INLITE_HUB_ID, InliteHub

DEPENDENCIES = ["inlite_hub"]

CONF_CONNECTED = "connected"

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(CONF_INLITE_HUB_ID): cv.use_id(InliteHub),
            cv.Optional(CONF_CONNECTED): binary_sensor.binary_sensor_schema(
                device_class=DEVICE_CLASS_CONNECTIVITY,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
                icon="mdi:bluetooth-connect",
            ),
        }
    ).extend(cv.COMPONENT_SCHEMA)
)


async def to_code(config):
    hub = await cg.get_variable(config[CONF_INLITE_HUB_ID])

    if connected_conf := config.get(CONF_CONNECTED):
        sens = await binary_sensor.new_binary_sensor(connected_conf)
        cg.add(hub.set_connected_binary_sensor(sens))
