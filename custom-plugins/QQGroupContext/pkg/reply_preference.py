from qunling import reply_preferences as preferences
from pkg.errors import VisionError


def apply(plugin, context, params):
    binding = plugin.get_installation_binding()
    if binding is None:
        raise VisionError("当前请求没有可验证的成员身份。")
    scope = preferences.key(binding, context.bot, context.group)
    if params == {"action": "get"}:
        return {"status": "ok", "preference": preferences.get(scope, context.sender)}
    if set(params) != {"action", "mode", "rule", "evidence"} or params["action"] != "set":
        raise VisionError("互动约定只接受当前成员的方式、条件和本轮原文。")
    if context.reply_preference_revision is None:
        raise VisionError("互动约定在本轮开始时未能读取，请重新提出。")
    try:
        result = preferences.save(scope, context.sender, mode=params["mode"], rule=params["rule"],
            expected_revision=context.reply_preference_revision, source_message_id=context.anchor,
            source_time=context.timestamp, evidence=params["evidence"], text=context.current_question)
    except preferences.PreferenceError as exc:
        raise VisionError(str(exc)) from None
    context.reply_preference_revision = result["revision"]
    return {"status": "saved", "preference": result}
