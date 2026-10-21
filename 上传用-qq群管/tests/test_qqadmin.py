# -*- coding: utf-8 -*-
"""QQ 官方群管插件的离线测试。"""

import asyncio
import importlib.util
import logging
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from admin_core import (  # noqa: E402
    CachedMessage,
    MessageCache,
    format_duration,
    format_status,
    parse_duration,
    parse_recall_count,
)
from official_api import ApiResult, ERROR_HINTS  # noqa: E402


def _ensure_astrbot() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("fake.astrbot")
    api.AstrBotConfig = dict

    event_mod = types.ModuleType("astrbot.api.event")

    class _EventMessageType:
        ALL = "all"

    class _Filter:
        EventMessageType = _EventMessageType

        @staticmethod
        def command(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def event_message_type(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

    class AstrMessageEvent:
        pass

    class MessageEventResult:
        pass

    event_mod.filter = _Filter
    event_mod.EventMessageType = _EventMessageType
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageEventResult = MessageEventResult

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context=None):
            self.context = context

    class Context:
        pass

    star_mod.Star = Star
    star_mod.Context = Context

    api.event = event_mod
    api.star = star_mod
    astrbot.api = api

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod


_ensure_astrbot()

_spec = importlib.util.spec_from_file_location(
    "qqadmin_plugin_under_test", PLUGIN_DIR / "main.py"
)
plugin_main = importlib.util.module_from_spec(_spec)
sys.modules["qqadmin_plugin_under_test"] = plugin_main
_spec.loader.exec_module(plugin_main)


class FakeAPI:
    def __init__(self, ready=True, result=None):
        self.ready = ready
        self.result = result or ApiResult(ok=True)
        self.calls = []

    async def mute_member(self, group, member, seconds):
        self.calls.append(("mute", group, member, seconds))
        return self.result

    async def unmute_member(self, group, member):
        self.calls.append(("unmute", group, member))
        return self.result

    async def remove_members(self, group, members, blacklist=False):
        self.calls.append(("kick", group, tuple(members)))
        return self.result

    async def recall_message(self, group, message_id):
        self.calls.append(("recall", group, message_id))
        return self.result

    async def query_restrict(self, group):
        self.calls.append(("status", group))
        return ApiResult(ok=True, data={"global_rule": {"mode": "none"}, "members": []})

    async def set_global_mute(self, group, enable):
        self.calls.append(("global", group, enable))
        return self.result

    async def close(self):
        pass


class FakeEvent:
    def __init__(
        self,
        text,
        *,
        platform="qq_official",
        sender="MEMBER_1",
        group="GROUP_1",
        message_id="MSG_1",
        mentions=None,
        name="小明",
    ):
        raw = SimpleNamespace(
            id=message_id,
            mentions=mentions or [],
        )
        self.message_str = text
        self.message_obj = SimpleNamespace(
            raw_message=raw,
            self_id="BOT_1",
            message=[],
        )
        self.platform_name = platform
        self._sender = sender
        self._group = group
        self._name = name
        self._extra = {}
        self.stopped = False

    def get_platform_name(self):
        return self.platform_name

    def get_group_id(self):
        return self._group

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._name

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def plain_result(self, text):
        return ("plain", text)

    def stop_event(self):
        self.stopped = True


async def collect_dispatch(plugin, event):
    return [item async for item in plugin._dispatch(event)]


def make_plugin(api=None, **overrides):
    config = {
        "enable": True,
        "app_id": "app",
        "app_secret": "secret",
        "admin_openids": [],
        "cache_messages": True,
        "cache_size": 30,
        "cache_ttl": 600,
        "timeout": 10,
    }
    config.update(overrides)
    plugin = plugin_main.QQGroupAdminPlugin(context=None, config=config)
    plugin.api = api or FakeAPI()
    return plugin


def mention(openid, name="小红", is_you=False):
    return SimpleNamespace(id=openid, username=name, is_you=is_you)


class TestDuration(unittest.TestCase):
    def test_minutes(self):
        self.assertEqual(parse_duration("禁言 @某人 10分钟"), 600)

    def test_hours(self):
        self.assertEqual(parse_duration("2小时"), 7200)

    def test_seconds(self):
        self.assertEqual(parse_duration("30秒"), 60)  # 低于 1 分钟按 1 分钟算

    def test_plain_number_is_minutes(self):
        self.assertEqual(parse_duration("30"), 1800)

    def test_default(self):
        self.assertEqual(parse_duration("", default_seconds=600), 600)

    def test_cap_30_days(self):
        self.assertEqual(parse_duration("999天"), 30 * 86400)

    def test_format(self):
        self.assertEqual(format_duration(600), "10 分钟")
        self.assertEqual(format_duration(7200), "2 小时")
        self.assertEqual(format_duration(86400), "1 天")

    def test_recall_count(self):
        self.assertEqual(parse_recall_count("撤回"), 1)
        self.assertEqual(parse_recall_count("撤回 3"), 3)
        self.assertEqual(parse_recall_count("撤回 99"), 10)


class TestCache(unittest.TestCase):
    def test_add_and_recent(self):
        cache = MessageCache(limit=5, ttl=60)
        for index in range(3):
            cache.add(
                CachedMessage(
                    message_id=f"m{index}", group="g", sender="u", name="n", text="t"
                )
            )
        self.assertEqual(len(cache.recent("g", 2)), 2)

    def test_exclude_current(self):
        cache = MessageCache(limit=5, ttl=60)
        cache.add(
            CachedMessage(message_id="m1", group="g", sender="u", name="n", text="t")
        )
        cache.add(
            CachedMessage(message_id="m2", group="g", sender="u", name="n", text="t")
        )
        items = cache.recent("g", 1, exclude_id="m2")
        self.assertEqual(items[0].message_id, "m1")

    def test_ttl(self):
        cache = MessageCache(limit=5, ttl=1)
        cache.add(
            CachedMessage(
                message_id="m1",
                group="g",
                sender="u",
                name="n",
                text="t",
                ts=time.time() - 10,
            )
        )
        self.assertEqual(cache.recent("g", 1), [])

    def test_limit(self):
        cache = MessageCache(limit=2, ttl=60)
        for index in range(5):
            cache.add(
                CachedMessage(
                    message_id=f"m{index}", group="g", sender="u", name="n", text="t"
                )
            )
        self.assertEqual(len(cache._data["g"]), 2)


class TestStatus(unittest.TestCase):
    def test_format(self):
        text = format_status(
            {
                "global_rule": {"mode": "always"},
                "members": [{"member_openid": "A", "mute_expire_at": "2026-01-01"}],
            }
        )
        self.assertIn("已开启", text)
        self.assertIn("1 人", text)

    def test_error_hint(self):
        result = ApiResult(ok=False, error_code="40062003", status=403)
        self.assertIn("管理员", result.hint)
        self.assertIn("40062003", ERROR_HINTS)


class TestFlow(unittest.TestCase):
    def test_non_official_platform_rejected(self):
        plugin = make_plugin()
        out = asyncio.run(
            collect_dispatch(
                plugin, FakeEvent("禁言 @小明 10分钟", platform="aiocqhttp")
            )
        )
        self.assertIn("只支持 QQ 官方机器人", out[0][1])

    def test_missing_credentials(self):
        plugin = make_plugin(api=FakeAPI(ready=False))
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("禁言 @小明 10分钟")))
        self.assertIn("AppID", out[0][1])

    def test_whoami(self):
        plugin = make_plugin()
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("我的id")))
        self.assertIn("MEMBER_1", out[0][1])

    def test_admin_restriction(self):
        plugin = make_plugin(admin_openids=["OTHER"])
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("禁言 @小明 10分钟")))
        self.assertIn("没有权限", out[0][1])

    def test_admin_allowed(self):
        api = FakeAPI()
        plugin = make_plugin(api=api, admin_openids=["MEMBER_1"])
        out = asyncio.run(
            collect_dispatch(
                plugin, FakeEvent("禁言 @小明 10分钟", mentions=[mention("TARGET_1")])
            )
        )
        self.assertIn("已禁言", out[0][1])
        self.assertEqual(api.calls[0][0], "mute")
        self.assertEqual(api.calls[0][2], "TARGET_1")
        self.assertEqual(api.calls[0][3], 600)

    def test_mute_without_mention(self):
        plugin = make_plugin()
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("禁言 10分钟")))
        self.assertIn("请 @", out[0][1])

    def test_mute_permission_error(self):
        api = FakeAPI(result=ApiResult(ok=False, error_code="40062003", status=403))
        plugin = make_plugin(api=api)
        out = asyncio.run(
            collect_dispatch(
                plugin, FakeEvent("禁言 @小明 10分钟", mentions=[mention("TARGET_1")])
            )
        )
        self.assertIn("管理员", out[0][1])

    def test_unmute(self):
        api = FakeAPI()
        plugin = make_plugin(api=api)
        out = asyncio.run(
            collect_dispatch(
                plugin, FakeEvent("解除禁言 @小明", mentions=[mention("TARGET_1")])
            )
        )
        self.assertIn("已解除", out[0][1])
        self.assertEqual(api.calls[0][0], "unmute")

    def test_kick(self):
        api = FakeAPI()
        plugin = make_plugin(api=api)
        asyncio.run(
            collect_dispatch(
                plugin, FakeEvent("踢人 @小明", mentions=[mention("TARGET_1")])
            )
        )
        self.assertEqual(api.calls[0][0], "kick")

    def test_recall_without_cache(self):
        plugin = make_plugin()
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("撤回")))
        self.assertIn("没找到可撤回", out[0][1])

    def test_recall_with_cache(self):
        api = FakeAPI()
        plugin = make_plugin(api=api)
        plugin.cache.add(
            CachedMessage(
                message_id="OLD_1",
                group="GROUP_1",
                sender="X",
                name="广告",
                text="广告",
            )
        )
        out = asyncio.run(
            collect_dispatch(plugin, FakeEvent("撤回", message_id="MSG_NOW"))
        )
        self.assertIn("已撤回 1 条", out[0][1])
        self.assertEqual(api.calls[0][2], "OLD_1")

    def test_recall_multiple(self):
        api = FakeAPI()
        plugin = make_plugin(api=api)
        for index in range(3):
            plugin.cache.add(
                CachedMessage(
                    message_id=f"OLD_{index}",
                    group="GROUP_1",
                    sender="X",
                    name="n",
                    text="t",
                )
            )
        asyncio.run(collect_dispatch(plugin, FakeEvent("撤回 2", message_id="MSG_NOW")))
        recalled = [call for call in api.calls if call[0] == "recall"]
        self.assertEqual(len(recalled), 2)

    def test_status(self):
        plugin = make_plugin()
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("禁言状态")))
        self.assertIn("全员禁言", out[0][1])

    def test_global_mute(self):
        api = FakeAPI()
        plugin = make_plugin(api=api)
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("全员禁言")))
        self.assertIn("已开启全员禁言", out[0][1])
        self.assertEqual(api.calls[0][0], "global")

    def test_global_mute_unsupported(self):
        api = FakeAPI(result=ApiResult(ok=False, status=404, message="not found"))
        plugin = make_plugin(api=api)
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("解除全员禁言")))
        self.assertIn("设置失败", out[0][1])

    def test_plain_chat_ignored(self):
        plugin = make_plugin()
        self.assertEqual(
            asyncio.run(collect_dispatch(plugin, FakeEvent("今天真热"))), []
        )

    def test_dedup(self):
        plugin = make_plugin()
        event = FakeEvent("禁言状态")
        first = asyncio.run(collect_dispatch(plugin, event))
        second = asyncio.run(collect_dispatch(plugin, event))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
