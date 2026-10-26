"""Reviewed QQ operations. This catalog describes APIs, never matches chat text.

Endpoint contracts: NapCat 4.18.37 OpenAPI. Chat/bot/actor come from the Host
Query; they are intentionally absent from all model-visible schemas.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def text(limit=200, **kw):
    return {"type": "string", "maxLength": limit, **kw}


ID = text(100, minLength=1)
BOOL = {"type": "boolean"}


def number(low, high):
    return {"type": "integer", "minimum": low, "maximum": high}


@dataclass(frozen=True)
class Operation:
    label: str
    category: str
    endpoint: str = ""
    access: str = "member"
    group: bool = False
    write: bool = False
    properties: dict = field(default_factory=dict)
    required: tuple = ()

    @property
    def schema(self):
        return {"type": "object", "properties": self.properties,
                "required": list(self.required), "additionalProperties": False}


OPS = {
    "group_info": Operation("本群资料", "read", "get_group_info", group=True),
    "members": Operation("本群成员、昵称、群名片及身份", "read", "get_group_member_list", group=True),
    "honors": Operation("本群荣誉", "read", "get_group_honor_info", group=True,
                        properties={"type": text(20, enum=["all", "talkative", "performer", "legend", "strong_newbie", "emotion"])}),
    "notices": Operation("读取群公告", "read", "_get_group_notice", group=True),
    "essence": Operation("读取群精华", "read", "get_essence_msg_list", group=True),
    "muted_members": Operation("本群禁言列表", "read", "get_group_shut_list", group=True),
    "at_all_remaining": Operation("本群 @全体剩余额度", "read", "get_group_at_all_remain", group=True),
    "file_storage": Operation("本群文件容量", "read", "get_group_file_system_info", group=True),
    "files": Operation("列出本群目录（返回文件和目录编号）", "read", group=True,
                       properties={"folder_id": ID}),
    "message": Operation("读取当前会话消息；省略编号读取触发消息", "read",
                         properties={"message_id": ID}),
    "events": Operation("本会话最近的戳一戳、表情回应、撤回、成员变动", "read",
                        properties={"limit": number(1, 50)}),
    "requests": Operation("待处理加群申请；机器人管理员还可查看好友申请和入群邀请", "read", access="admin"),
    "friends": Operation("机器人好友列表（仅机器人管理员）", "account", "get_friend_list", access="owner"),
    "bot_status": Operation("机器人 QQ 连接与账号状态", "read", "get_status"),
    "poke": Operation("戳一戳当前会话成员", "interaction", write=True,
                      properties={"user_id": ID}, required=("user_id",)),
    "react": Operation("给本会话消息添加或取消 QQ 表情回应", "interaction", "set_msg_emoji_like", write=True,
                       properties={"message_id": ID, "emoji_id": text(20, minLength=1), "set": BOOL}, required=("emoji_id",)),
    "like": Operation("给当前会话成员 QQ 资料点赞", "interaction", "send_like", write=True,
                      properties={"user_id": ID, "times": number(1, 10)}, required=("user_id",)),
    "face": Operation("发送 QQ 内置表情、骰子或猜拳", "interaction", write=True,
                      properties={"kind": text(10, enum=["face", "dice", "rps"]), "face_id": number(0, 10000)}, required=("kind",)),
    "say": Operation("发送文字，可选择引用和 @；直接回复无需调用本操作", "interaction", write=True,
                     properties={"text": text(2000, minLength=1), "user_id": ID, "message_id": ID}, required=("text",)),
    "at_all": Operation("在本群发送 @全体通知", "management", access="admin", group=True, write=True,
                        properties={"text": text(2000, minLength=1)}, required=("text",)),
    "forward": Operation("将本会话已有消息组装为合并转发；保留实际作者", "interaction", write=True,
                         properties={"message_ids": {"type": "array", "minItems": 1, "maxItems": 10, "uniqueItems": True, "items": ID}}, required=("message_ids",)),
    "recall": Operation("撤回本会话消息；普通成员只能撤回机器人消息", "management", write=True,
                        properties={"message_id": ID}, required=("message_id",)),
    "mute": Operation("禁言本群成员，duration 为秒；0 为解除", "management", "set_group_ban", "admin", True, True,
                      {"user_id": ID, "duration": number(0, 2592000)}, ("user_id", "duration")),
    "kick": Operation("移出本群成员", "management", "set_group_kick", "admin", True, True,
                      {"user_id": ID, "reject_add_request": BOOL}, ("user_id",)),
    "whole_mute": Operation("开启或关闭全员禁言", "management", "set_group_whole_ban", "admin", True, True,
                            {"enable": BOOL}, ("enable",)),
    "group_name": Operation("修改群名", "management", "set_group_name", "admin", True, True,
                            {"group_name": text(60, minLength=1)}, ("group_name",)),
    "member_card": Operation("修改群名片", "management", "set_group_card", "admin", True, True,
                             {"user_id": ID, "card": text(60)}, ("user_id", "card")),
    "member_title": Operation("修改专属头衔，需要机器人为群主及机器人管理员发起", "management", "set_group_special_title", "group_owner", True, True,
                              {"user_id": ID, "special_title": text(60)}, ("user_id", "special_title")),
    "administrator": Operation("设置或取消管理员，需要机器人管理员发起且机器人为群主", "management", "set_group_admin", "group_owner", True, True,
                               {"user_id": ID, "enable": BOOL}, ("user_id", "enable")),
    "leave": Operation("机器人退出当前群（不解散群）；仅机器人管理员", "management", "set_group_leave", "owner", True, True),
    "publish_notice": Operation("发布群公告", "management", "_send_group_notice", "admin", True, True,
                                {"content": text(4000, minLength=1), "pinned": BOOL}, ("content",)),
    "delete_notice": Operation("删除已读取的群公告", "management", "_del_group_notice", "admin", True, True,
                               {"notice_id": ID}, ("notice_id",)),
    "set_essence": Operation("设置或移除群精华消息", "management", access="admin", group=True, write=True,
                             properties={"message_id": ID, "enable": BOOL}, required=("message_id", "enable")),
    "respond_request": Operation("处理实际收到的好友/加群/邀请事件，使用 requests 返回的 request_ref", "management", access="admin", write=True,
                                 properties={"request_ref": ID, "approve": BOOL, "reason": text(200), "remark": text(60)}, required=("request_ref", "approve")),
    "create_folder": Operation("创建群文件夹", "files", "create_group_file_folder", "admin", True, True,
                               {"folder_name": text(100, minLength=1)}, ("folder_name",)),
    "delete_folder": Operation("删除已读取的群文件夹及其中内容", "files", "delete_group_folder", "admin", True, True,
                               {"folder_id": ID}, ("folder_id",)),
    "delete_file": Operation("删除已读取的群文件", "files", "delete_group_file", "admin", True, True,
                             {"file_id": ID}, ("file_id",)),
    "rename_file": Operation("重命名已读取的群文件", "files", "rename_group_file", "admin", True, True,
                             {"file_id": ID, "new_name": text(200, minLength=1)}, ("file_id", "new_name")),
    "move_file": Operation("移动群文件到已读取的目录", "files", "move_group_file", "admin", True, True,
                           {"file_id": ID, "target_parent_directory": ID}, ("file_id", "target_parent_directory")),
    "upload_file": Operation("把当前文件工作区素材上传到本群指定目录", "files", access="member", group=True, write=True,
                             properties={"asset_ref": ID, "folder_id": ID}, required=("asset_ref",)),
    "voices": Operation("获取本群 QQ 语音角色", "read", "get_ai_characters", group=True),
    "speak": Operation("使用 voices 返回的 QQ 角色发送语音", "media", "send_group_ai_record", group=True, write=True,
                       properties={"character": ID, "text": text(500, minLength=1)}, required=("character", "text")),
    "media": Operation("列出当前或指定消息中的图片、语音、视频和文件", "read", properties={"message_id": ID}),
    "import_media": Operation("将 media 返回的素材导入文件工作区，可交给其他工具", "read",
                              properties={"media_ref": ID}, required=("media_ref",)),
    "send_media": Operation("把工作区素材作为 QQ 图片/语音/视频发送", "media", write=True,
                            properties={"asset_ref": ID, "kind": text(12, enum=["image", "record", "video"])}, required=("asset_ref", "kind")),
    "transcribe": Operation("识别已导入的语音，需配置语音识别服务", "media", properties={"asset_ref": ID}, required=("asset_ref",)),
    "video_frames": Operation("抽取视频关键帧为图片素材，交给视觉工具分析", "media",
                              properties={"asset_ref": ID, "seconds": number(1, 120), "frames": number(1, 8)}, required=("asset_ref",)),
    "analyze_images": Operation("分析已导入的图片或视频关键帧实际画面", "media",
                                properties={"asset_refs": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True, "items": ID},
                                            "question": text(1000, minLength=1)}, required=("asset_refs", "question")),
}


def validate_arguments(schema, value):
    """Small strict JSON schema subset shared by catalog and execution."""
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) - schema["properties"].keys() or not set(schema.get("required", ())) <= value.keys():
            raise ValueError("参数缺失或包含未开放字段。")
        for key, item in value.items():
            validate_arguments(schema["properties"][key], item)
    elif kind == "array":
        if not isinstance(value, list) or not schema.get("minItems", 0) <= len(value) <= schema["maxItems"]:
            raise ValueError("列表长度超限。")
        for item in value:
            validate_arguments(schema["items"], item)
        if schema.get("uniqueItems") and len(set(value)) != len(value):
            raise ValueError("列表不能重复。")
    elif kind == "string":
        if (not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema["maxLength"]
                or any(ord(c) < 32 and c not in "\n\t" or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
            raise ValueError("文字参数无效或过长。")
    elif kind == "integer":
        if type(value) is not int or not schema["minimum"] <= value <= schema["maximum"]:
            raise ValueError("数字参数超出范围。")
    elif kind == "boolean" and type(value) is not bool:
        raise ValueError("开关必须为布尔值。")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("不支持的参数选项。")


def continuation_allowed(params):
    if not isinstance(params, dict) or set(params) - {"operation", "arguments"}:
        return False
    operation = OPS.get(params.get("operation")) if isinstance(params.get("operation"), str) else None
    return operation is not None and (not operation.write or operation.category == "interaction")
