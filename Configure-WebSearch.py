"""Connect the installed web plugin to the existing sub2api provider."""

import json
import time

import requests

from local_api import LocalAPI, STATE


def read_config(api, path):
    # Saving plugin settings remounts the worker. The GET route is briefly absent.
    for attempt in range(21):
        try:
            return api.call("GET", path)["config"]
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in {404, 503} or attempt == 20:
                raise
            time.sleep(0.5)


def main():
    api = LocalAPI()
    state_path = STATE / "setup-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    provider = api.call(
        "GET",
        "/api/v1/provider/providers/" + state["provider_uuid"] + "?include_secret=true",
    )["provider"]
    if not provider.get("api_keys"):
        raise RuntimeError("Configure the existing model provider key first.")
    plugin = "local/QQWebSearch"
    config_path = "/api/v1/plugins/" + plugin + "/config"
    config = read_config(api, config_path)
    config.update(
        {
            "api_base_url": provider["base_url"],
            "api_key": provider["api_keys"][0],
            "model_name": config.get("model_name") or "gpt-6.1-sol",
            "vision_model_uuid": config.get("vision_model_uuid")
            or state["models"]["gpt-6.1-sol"],
            "request_timeout_seconds": config.get("request_timeout_seconds", 65),
        }
    )
    api.call("PUT", config_path, config)
    actual = read_config(api, config_path)
    if any(
        actual.get(key) != value for key, value in config.items() if key != "api_key"
    ):
        raise RuntimeError("Web plugin settings did not match the saved configuration.")
    state["web_search_plugin"] = plugin
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print("Web search configured from the existing provider; no credentials printed.")


if __name__ == "__main__":
    main()
