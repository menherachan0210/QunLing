"""Enable on-demand group discussion context without changing reply triggers."""

import json
from local_api import LocalAPI, STATE

api = LocalAPI()
path = STATE / "setup-state.json"
state = json.loads(path.read_text(encoding="utf-8"))
plugin = "local/QQGroupContext"
config = api.call("GET", "/api/v1/plugins/" + plugin + "/config")["config"]
if not config.get("model_uuid"):
    config["model_uuid"] = state["models"]["gpt-6.1-sol"]
api.call("PUT", "/api/v1/plugins/" + plugin + "/config", config)
state["group_context_plugin"] = plugin
path.write_text(json.dumps(state, indent=2), encoding="utf-8")
print(
    "Group discussion context configured; model choices and reply triggers preserved."
)
