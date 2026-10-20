"""Apply the versioned chat style while preserving models, tools and triggers."""

import json

from chat_style import styled_prompts
from local_api import LocalAPI, STATE


def main():
    api = LocalAPI()
    state = json.loads((STATE / "setup-state.json").read_text(encoding="utf-8"))
    path = "/api/v1/pipelines/" + state["pipeline_uuid"]
    config = api.call("GET", path)["pipeline"]["config"]
    runner = config["ai"]["runner_config"]["plugin:langbot-team/LocalAgent/default"]
    prompts = styled_prompts(runner["prompt"])
    if prompts == runner["prompt"]:
        print("Chat style already configured; no settings changed.")
        return
    runner["prompt"] = prompts
    api.call("PUT", path, {"config": config})
    actual = api.call("GET", path)["pipeline"]["config"]
    if actual != config:
        raise RuntimeError("Pipeline readback differs; inspect the configuration.")
    print("Natural chat style applied; model, tools, triggers and history preserved.")


if __name__ == "__main__":
    main()
