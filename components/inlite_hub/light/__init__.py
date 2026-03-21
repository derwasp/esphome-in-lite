import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import light
from esphome.const import CONF_OUTPUT_ID

from .. import CONF_INLITE_HUB_ID, InliteHub, inlite_hub_ns

DEPENDENCIES = ["inlite_hub"]

CONF_LINE = "line"

InliteLineLight = inlite_hub_ns.class_(
    "InliteLineLight", light.LightOutput, cg.Component
)

CONFIG_SCHEMA = cv.All(
    light.BRIGHTNESS_ONLY_LIGHT_SCHEMA.extend(
        {
            cv.GenerateID(CONF_OUTPUT_ID): cv.declare_id(InliteLineLight),
            cv.GenerateID(CONF_INLITE_HUB_ID): cv.use_id(InliteHub),
            cv.Required(CONF_LINE): cv.int_range(min=0, max=15),
        }
    ).extend(cv.COMPONENT_SCHEMA)
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_OUTPUT_ID])
    await cg.register_component(var, config)
    await light.register_light(var, config)

    hub = await cg.get_variable(config[CONF_INLITE_HUB_ID])
    cg.add(var.set_parent(hub))
    cg.add(var.set_line(config[CONF_LINE]))
    cg.add(hub.register_line_light(var))
