import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import button
from esphome.const import ENTITY_CATEGORY_DIAGNOSTIC

from .. import CONF_INLITE_HUB_ID, InliteHub, inlite_hub_ns

DEPENDENCIES = ["inlite_hub"]

CONF_REFRESH_STATE = "refresh_state"
CONF_RECONNECT = "reconnect"

InliteRefreshStateButton = inlite_hub_ns.class_(
    "InliteRefreshStateButton", button.Button
)
InliteReconnectButton = inlite_hub_ns.class_(
    "InliteReconnectButton", button.Button
)

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(CONF_INLITE_HUB_ID): cv.use_id(InliteHub),
            cv.Optional(CONF_REFRESH_STATE): button.button_schema(
                InliteRefreshStateButton,
                icon="mdi:sync",
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
            cv.Optional(CONF_RECONNECT): button.button_schema(
                InliteReconnectButton,
                icon="mdi:bluetooth-connect",
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
        }
    ).extend(cv.COMPONENT_SCHEMA)
)


async def to_code(config):
    hub = await cg.get_variable(config[CONF_INLITE_HUB_ID])

    if refresh_conf := config.get(CONF_REFRESH_STATE):
        btn = await button.new_button(refresh_conf)
        cg.add(btn.set_parent(hub))

    if reconnect_conf := config.get(CONF_RECONNECT):
        btn = await button.new_button(reconnect_conf)
        cg.add(btn.set_parent(hub))
