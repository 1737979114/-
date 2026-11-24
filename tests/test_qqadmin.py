# -*- coding: utf-8 -*-
"""QQ 官方群管插件的离线测试。"""

import asyncio
import base64
import importlib.util
import json
import sqlite3
import sys
import tempfile
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
from review_store import ReviewStore  # noqa: E402
from roster import RosterStore  # noqa: E402


class _SilentLogger:
    """离线测试用日志桩，不依赖内置 logging。"""

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


def _ensure_astrbot() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _SilentLogger()
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

    web_mod = types.ModuleType("astrbot.api.web")

    class _FakeResponse:
        def __init__(self, payload: dict, status_code: int = 200):
            self.body = json.dumps(payload, ensure_ascii=False)
            self.status_code = status_code

    def _json_response(data=None, *, status_code=200, headers=None):
        return _FakeResponse({} if data is None else data, status_code)

    def _error_response(message, *, status_code=400, data=None, headers=None):
        return _FakeResponse(
            {"status": "error", "message": message, "data": data}, status_code
        )

    web_mod.json_response = _json_response
    web_mod.error_response = _error_response
    web_mod.request = SimpleNamespace(query={}, json=None)

    api.event = event_mod
    api.star = star_mod
    api.web = web_mod
    astrbot.api = api

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.web"] = web_mod


_ensure_astrbot()

_spec = importlib.util.spec_from_file_location(
    "qqadmin_plugin_under_test", PLUGIN_DIR / "main.py"
)
plugin_main = importlib.util.module_from_spec(_spec)
sys.modules["qqadmin_plugin_under_test"] = plugin_main
_spec.loader.exec_module(plugin_main)


class FakeAPI:
    def __init__(self, ready=True, result=None, role="owner", role_denied=False):
        self.ready = ready
        self.result = result or ApiResult(ok=True)
        self.role = role
        self.role_denied = role_denied
        self.calls = []

    async def member_info(self, group_openid, member_openid):
        self.calls.append(("member_info", group_openid, member_openid))
        if self.role_denied:
            return ApiResult(
                ok=False, status=400, error_code="11253", message="应用无接口访问权限"
            )
        return ApiResult(ok=True, data={"member": {"role": self.role}})

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

    async def join_requests(self, group, cursor=""):
        self.calls.append(("join_requests", group))
        return ApiResult(
            ok=True,
            data={
                "join_requests": [
                    {"member_openid": "J1", "username": "甲"},
                    {"member_openid": "J2", "username": "乙", "apply_comment": "朋友介绍"},
                ]
            },
        )

    async def approve_join(self, group, member_openid, approve=True):
        self.calls.append(("approve_join", group, member_openid, approve))
        return self.result

    async def blacklist_op(self, group, members, op="add"):
        self.calls.append(("blacklist_op", group, tuple(members), op))
        return self.result

    async def query_restrict(self, group):
        self.calls.append(("status", group))
        return ApiResult(ok=True, data={"global_rule": {"mode": "none"}, "members": []})

    async def set_global_mute(self, group, enable):
        self.calls.append(("global", group, enable))
        return self.result

    async def group_info(self, group):
        self.calls.append(("group_info", group))
        return ApiResult(ok=True, data={"group_name": "测试群", "group_member_num": 12})

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
        "auto_unmute": False,
    }
    config.update(overrides)
    plugin = plugin_main.QQGroupAdminPlugin(context=None, config=config)
    plugin.api = api or FakeAPI()

    # 测试不读写真实状态/配置文件，避免用例之间以及和线上配置互相污染
    plugin._state_path = lambda: Path(tempfile.mkdtemp()) / "state.json"
    plugin._config_path = lambda: Path(tempfile.mkdtemp()) / "config.json"
    media_dir = Path(tempfile.mkdtemp())
    plugin._media_dir = lambda: media_dir
    plugin.roster.close()
    plugin.roster = RosterStore(str(Path(tempfile.mkdtemp()) / "roster.db"))
    plugin._roster_touched.clear()
    if plugin._review is not None:
        plugin._review.close()
    plugin._review = ReviewStore(str(Path(tempfile.mkdtemp()) / "review.db"), limit=200)
    plugin.groups.clear()
    plugin.daily.clear()
    plugin.records.clear()
    plugin._mutes.clear()
    plugin.misjudge_samples.clear()
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


class TestRoster(unittest.TestCase):
    """官方拿不到成员列表，靠「谁说过话」攒名册。"""

    def test_touch_and_page(self):
        store = RosterStore(str(Path(tempfile.mkdtemp()) / "roster.db"))
        now = time.time()
        store.touch("g", "U1", "玄鱼", now - 60)
        store.touch("g", "U2", "小北", now - 30)
        store.touch("g", "U1", "玄鱼改名", now)

        rows, total = store.page("g")
        self.assertEqual(total, 2)
        self.assertEqual([r["openid"] for r in rows], ["U1", "U2"])
        self.assertEqual(rows[0]["name"], "玄鱼改名", "昵称以最新一次为准")
        store.close()

    def test_keyword_and_scope(self):
        store = RosterStore(str(Path(tempfile.mkdtemp()) / "roster.db"))
        store.touch("g", "AAAA1111", "玄鱼")
        store.touch("g", "BBBB2222", "小北")
        store.touch("other", "CCCC3333", "别人")

        rows, total = store.page("g", keyword="玄")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["openid"], "AAAA1111")

        rows, total = store.page("g", keyword="BBBB")
        self.assertEqual(total, 1)
        self.assertEqual(store.count("g"), 2)
        self.assertEqual(store.count("other"), 1)
        store.close()

    def test_empty_name_keeps_old(self):
        store = RosterStore(str(Path(tempfile.mkdtemp()) / "roster.db"))
        store.touch("g", "U1", "玄鱼")
        store.touch("g", "U1", "")
        self.assertEqual(store.get("g", "U1")["name"], "玄鱼")
        store.close()

    def test_throttle(self):
        plugin = make_plugin()
        plugin._touch_roster("g", "U1", "玄鱼")
        plugin._touch_roster("g", "U1", "玄鱼")
        rows, total = plugin.roster.page("g")
        self.assertEqual(total, 1)
        self.assertGreaterEqual(plugin.roster.get("g", "U1")["last_seen"], 0)


class TestGroupList(unittest.TestCase):
    """官方没有群列表接口，只能从名册和历史里补。"""

    def test_merge_from_roster(self):
        plugin = make_plugin()
        plugin.groups.clear()
        gid = "F" * 31 + "1"
        plugin.roster.touch(gid, "U1", "玄鱼")
        added = plugin._merge_known_groups()
        self.assertEqual(added, 1)
        self.assertIn(gid, plugin.groups)

    def test_numeric_group_ignored(self):
        plugin = make_plugin()
        plugin.groups.clear()
        plugin.roster.touch("1090353201", "U1", "老王")
        self.assertEqual(plugin._merge_known_groups(), 0)
        self.assertNotIn("1090353201", plugin.groups)

    def test_state_keeps_member_count(self):
        plugin = make_plugin()
        state_file = Path(tempfile.mkdtemp()) / "state.json"
        plugin._state_path = lambda: state_file
        gid = "B" * 32
        plugin.groups[gid] = {
            "group_id": gid,
            "name": "测试群",
            "last_seen": 1.0,
            "blocked": 2,
            "member_count": 88,
        }
        plugin._save_state(force=True)
        reloaded = make_plugin()
        reloaded._state_path = lambda: state_file
        reloaded.groups.clear()
        reloaded._load_state()
        self.assertEqual(reloaded.groups[gid]["member_count"], 88)
        self.assertEqual(reloaded.groups[gid]["blocked"], 2)

    def test_state_path_under_plugin_data(self):
        fake = SimpleNamespace(
            _roster_path=lambda: Path("/tmp")
            / "plugin_data"
            / plugin_main.PLUGIN_NAME
            / "roster.db"
        )
        path = plugin_main.QQGroupAdminPlugin._state_path(fake)
        self.assertEqual(path.name, "state.json")
        self.assertEqual(path.parent.name, plugin_main.PLUGIN_NAME)

    def test_legacy_state_migrates_to_plugin_data(self):
        root = Path(tempfile.mkdtemp())
        legacy = root / "config" / f"{plugin_main.PLUGIN_NAME}_state.json"
        legacy.parent.mkdir(parents=True)
        gid = "C" * 32
        legacy.write_text(
            json.dumps(
                {
                    "groups": {
                        gid: {"name": "老群", "last_seen": 1.0, "blocked": 3}
                    }
                }
            ),
            encoding="utf-8",
        )
        new = root / "plugin_data" / plugin_main.PLUGIN_NAME / "state.json"
        fake = SimpleNamespace(
            _state_path=lambda: new,
            _legacy_state_path=lambda: legacy,
            groups={},
            daily={},
            records=[],
            rule_hits={},
            _mutes={},
        )
        plugin_main.QQGroupAdminPlugin._load_state(fake)
        self.assertFalse(legacy.exists())
        self.assertTrue(new.exists())
        self.assertEqual(fake.groups[gid]["blocked"], 3)
        self.assertEqual(fake.groups[gid]["name"], "老群")

    def test_fill_group_names(self):
        plugin = make_plugin(api=FakeAPI())
        gid = "A" * 32
        plugin.groups.clear()
        plugin.groups[gid] = {"group_id": gid, "name": "", "last_seen": 0, "blocked": 0}

        done = asyncio.run(plugin._fill_group_names(limit=5))
        self.assertEqual(done, 1)
        self.assertEqual(plugin.groups[gid]["name"], "测试群")
        self.assertEqual(plugin.groups[gid]["member_count"], 12)


class TestSeedTime(unittest.TestCase):
    def test_parse_db_time_is_utc(self):
        import calendar

        ts = plugin_main.QQGroupAdminPlugin._parse_db_time(
            "2026-11-01 06:36:20.784287"
        )
        expected = calendar.timegm((2026, 11, 1, 6, 36, 20, 0, 0, 0))
        self.assertAlmostEqual(ts, expected, delta=1)

    def test_parse_db_time_bad_value(self):
        self.assertEqual(plugin_main.QQGroupAdminPlugin._parse_db_time(""), 0.0)
        self.assertEqual(plugin_main.QQGroupAdminPlugin._parse_db_time(None), 0.0)


class TestMemberAction(unittest.TestCase):
    """面板上的禁言 / 解禁 / 踢人按钮。"""

    def test_mute_then_unmute(self):
        plugin = make_plugin(api=FakeAPI())
        result = asyncio.run(
            plugin._member_action(
                {"group": "g", "openid": "U1", "name": "玄鱼", "action": "mute", "seconds": 600}
            )
        )
        body = json.loads(result.body)
        self.assertTrue(body["ok"])
        self.assertIn("已禁言", body["data"]["message"])

        result = asyncio.run(
            plugin._member_action({"group": "g", "openid": "U1", "action": "unmute"})
        )
        body = json.loads(result.body)
        self.assertTrue(body["ok"])
        self.assertIn("解除", body["data"]["message"])

    def test_kick(self):
        plugin = make_plugin(api=FakeAPI())
        result = asyncio.run(
            plugin._member_action({"group": "g", "openid": "U1", "action": "kick"})
        )
        body = json.loads(result.body)
        self.assertTrue(body["ok"])

    def test_action_needs_credentials(self):
        plugin = make_plugin(api=FakeAPI(ready=False))
        result = asyncio.run(
            plugin._member_action({"group": "g", "openid": "U1", "action": "mute"})
        )
        body = json.loads(result.body)
        self.assertEqual(body["status"], "error")
        self.assertIn("AppID", body["message"])

    def test_grant_and_revoke(self):
        plugin = make_plugin(api=FakeAPI())
        asyncio.run(
            plugin._member_action({"group": "g", "openid": "U1", "action": "grant"})
        )
        self.assertIn("U1", plugin.group_admins.get("g", []))

        asyncio.run(
            plugin._member_action({"group": "g", "openid": "U1", "action": "revoke"})
        )
        self.assertNotIn("U1", plugin.group_admins.get("g", []))

    def test_unknown_action(self):
        plugin = make_plugin(api=FakeAPI())
        result = asyncio.run(
            plugin._member_action({"group": "g", "openid": "U1", "action": "explode"})
        )
        body = json.loads(result.body)
        self.assertEqual(body["status"], "error")


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
        plugin = make_plugin(api=FakeAPI(role="member"), admin_openids=["OTHER"])
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
        self.assertEqual(api.calls[-1][0], "mute")
        self.assertEqual(api.calls[-1][2], "TARGET_1")
        self.assertEqual(api.calls[-1][3], 600)

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
        self.assertEqual(api.calls[-1][0], "unmute")

    def test_kick(self):
        api = FakeAPI()
        plugin = make_plugin(api=api)
        asyncio.run(
            collect_dispatch(
                plugin, FakeEvent("踢人 @小明", mentions=[mention("TARGET_1")])
            )
        )
        self.assertEqual(api.calls[-1][0], "kick")

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
        self.assertEqual(api.calls[-1][2], "OLD_1")

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
        self.assertEqual(api.calls[-1][0], "global")

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

class TestJoinApproval(unittest.TestCase):
    def test_list_join_requests(self):
        plugin = make_plugin()
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("入群申请")))
        self.assertIn("待审批入群申请", out[0][1])
        self.assertIn("1. 甲", out[0][1])
        self.assertIn("同意 1", out[0][1])

    def test_approve_first(self):
        plugin = make_plugin()
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("同意 1")))
        self.assertIn("已通过 1 条", out[0][1])
        self.assertIn(("approve_join", "GROUP_1", "J1", True), plugin.api.calls)

    def test_reject_all(self):
        plugin = make_plugin()
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("拒绝 全部")))
        self.assertIn("已拒绝 2 条", out[0][1])
        self.assertIn(("approve_join", "GROUP_1", "J2", False), plugin.api.calls)

    def test_index_out_of_range(self):
        plugin = make_plugin()
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("同意 9")))
        self.assertIn("序号超了", out[0][1])


class TestBlacklistChange(unittest.TestCase):
    def test_add_blacklist(self):
        plugin = make_plugin()
        event = FakeEvent("拉黑", mentions=[{"id": "TARGET_1", "username": "小红"}])
        out = asyncio.run(collect_dispatch(plugin, event))
        self.assertIn("加入黑名单", out[0][1])
        self.assertIn(("blacklist_op", "GROUP_1", ("TARGET_1",), "add"), plugin.api.calls)

    def test_remove_blacklist(self):
        plugin = make_plugin()
        event = FakeEvent("解除拉黑", mentions=[{"id": "TARGET_1"}])
        out = asyncio.run(collect_dispatch(plugin, event))
        self.assertIn("移出黑名单", out[0][1])
        self.assertIn(("blacklist_op", "GROUP_1", ("TARGET_1",), "del"), plugin.api.calls)


class TestAutoUnmute(unittest.TestCase):
    def test_schedule_records_deadline(self):
        plugin = make_plugin()
        plugin.auto_unmute = True
        plugin._ensure_unmute_task = lambda: None
        plugin._schedule_unmute("GROUP_1", "U1", 60)
        self.assertIn("GROUP_1:U1", plugin._mutes)

    def test_disabled_does_nothing(self):
        plugin = make_plugin()
        plugin.auto_unmute = False
        plugin._schedule_unmute("GROUP_1", "U1", 60)
        self.assertEqual(plugin._mutes, {})

class TestClaimAdmin(unittest.TestCase):
    def test_claim_when_empty(self):
        plugin = make_plugin()
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("/初始化管理员")))
        self.assertIn("已把你设为管理员", out[0][1])
        self.assertEqual(plugin.admin_openids, {"MEMBER_1"})

    def test_claim_refused_when_taken(self):
        plugin = make_plugin(admin_openids=["OTHER"])
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("/初始化管理员")))
        self.assertIn("已经有内容", out[0][1])
        self.assertEqual(plugin.admin_openids, {"OTHER"})

    def test_permission_hint_when_api_denied(self):
        plugin = make_plugin(api=FakeAPI(role_denied=True))
        event = FakeEvent("/禁言", mentions=[{"id": "TARGET_1"}])
        out = asyncio.run(collect_dispatch(plugin, event))
        self.assertIn("初始化管理员", out[0][1])
        self.assertIn("11253", out[0][1])

class TestRecallParsing(unittest.TestCase):
    def test_count_with_trailing_words(self):
        # 用户可能把说明一起粘进来：「/撤回 3 撤回最近几条消息」
        plugin = make_plugin(admin_openids=["MEMBER_1"])
        out = asyncio.run(
            collect_dispatch(plugin, FakeEvent("/撤回 3 撤回最近几条消息"))
        )
        self.assertTrue(out, "带说明的撤回命令也要有回应")
        self.assertIn("撤回", out[0][1])

    def test_plain_recall(self):
        plugin = make_plugin(admin_openids=["MEMBER_1"])
        out = asyncio.run(collect_dispatch(plugin, FakeEvent("/撤回")))
        self.assertTrue(out)

class TestGroupAdmins(unittest.TestCase):
    def test_parse_from_string(self):
        parsed = plugin_main.QQGroupAdminPlugin._parse_group_admins(
            '{"G1": ["A", "B"], "G2": "C,D"}'
        )
        self.assertEqual(parsed["G1"], ["A", "B"])
        self.assertEqual(parsed["G2"], ["C", "D"])

    def test_group_admin_can_mute(self):
        plugin = make_plugin(admin_openids=["OTHER"])
        plugin.group_admins = {"GROUP_1": ["MEMBER_1"]}
        event = FakeEvent("/禁言", mentions=[{"id": "TARGET_1", "username": "目标"}])
        out = asyncio.run(collect_dispatch(plugin, event))
        self.assertIn("已禁言", out[0][1])
        self.assertIn(("mute", "GROUP_1", "TARGET_1", 600), plugin.api.calls)

    def test_other_group_admin_cannot(self):
        plugin = make_plugin(admin_openids=["OTHER"])
        plugin.group_admins = {"GROUP_2": ["MEMBER_1"]}
        event = FakeEvent("/禁言", mentions=[{"id": "TARGET_1"}])
        out = asyncio.run(collect_dispatch(plugin, event))
        self.assertIn("没有这个群的群管权限", out[0][1])

    def test_add_group_admin(self):
        plugin = make_plugin(admin_openids=["MEMBER_1"])
        plugin.group_admins = {}
        event = FakeEvent("/添加群管", mentions=[{"id": "TARGET_1", "username": "小红"}])
        out = asyncio.run(collect_dispatch(plugin, event))
        self.assertIn("设为群管", out[0][1])
        self.assertEqual(plugin.group_admins["GROUP_1"], ["TARGET_1"])


class TestRecordContent(unittest.TestCase):
    """违规记录要带原消息，撤回后也能回看是否误判。"""

    def test_record_stores_message_text(self):
        plugin = make_plugin()
        event = FakeEvent("傻逼吧", sender="C5C10DEE", name="草草")
        plugin._record("敏感词", "命中规则词「傻逼」", "规则词", "recall", True, event)

        item = plugin.records[-1]
        self.assertEqual(item["text"], "傻逼吧")
        self.assertEqual(item["msg_id"], "MSG_1")
        self.assertEqual(item["name"], "草草")
        self.assertEqual(item["kind"], "敏感词")

    def test_record_truncates_long_text(self):
        plugin = make_plugin()
        event = FakeEvent("啊" * 500)
        plugin._record("敏感词", "命中规则词「啊」", "规则词", "recall", True, event)
        self.assertEqual(len(plugin.records[-1]["text"]), 200)

    def test_record_strips_at_prefix(self):
        plugin = make_plugin()
        event = FakeEvent("<@!BOT_1> 傻逼吧")
        plugin._record("敏感词", "命中", "规则词", "recall", True, event)
        self.assertEqual(plugin.records[-1]["text"], "傻逼吧")


class TestRecordBackfill(unittest.TestCase):
    """升级前的老记录没有原文，要能从 AstrBot 消息历史里补回来。"""

    def _history_db(self, stamp, text="傻逼吧", group="GROUP_1", sender="MEMBER_1"):
        path = Path(tempfile.mkdtemp()) / "data_v4.db"
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE platform_message_history ("
            "created_at TEXT, user_id TEXT, sender_id TEXT, sender_name TEXT, content TEXT)"
        )
        conn.execute(
            "INSERT INTO platform_message_history VALUES (?,?,?,?,?)",
            (
                stamp,
                f"QQ机器人:GroupMessage:{group}",
                sender,
                "草草",
                json.dumps(
                    {"type": "user", "message": [{"type": "plain", "text": text}]},
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()
        conn.close()
        return path

    def test_backfill_from_history(self):
        plugin = make_plugin()
        ts = time.mktime(time.strptime("2026-11-15 21:15:48", "%Y-%m-%d %H:%M:%S"))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts + 1)) + ".095152"
        plugin._history_db_path = lambda: self._history_db(stamp)

        item = {
            "date": "2026-11-15",
            "time": "11-15 21:15:48",
            "group": "GROUP_1",
            "user": "MEMBER_1",
        }
        plugin._fill_record_text([item])
        self.assertEqual(item["text"], "傻逼吧")
        self.assertEqual(item.get("text_source"), "history")

    def test_skip_when_text_exists(self):
        plugin = make_plugin()
        plugin._history_db_path = lambda: self._history_db("2000-01-01 00:00:00")
        item = {
            "date": "2026-11-15",
            "time": "11-15 21:15:48",
            "group": "GROUP_1",
            "user": "MEMBER_1",
            "text": "已有的原文",
        }
        plugin._fill_record_text([item])
        self.assertEqual(item["text"], "已有的原文")

    def test_no_match_keeps_empty(self):
        plugin = make_plugin()
        plugin._history_db_path = lambda: self._history_db("2020-01-01 00:00:00")
        item = {
            "date": "2026-11-15",
            "time": "11-15 21:15:48",
            "group": "GROUP_1",
            "user": "MEMBER_1",
        }
        plugin._fill_record_text([item])
        self.assertNotIn("text", item)


class Image:
    """测试用图片组件：类名必须是 Image，才能被 _image_urls 认出来。"""

    def __init__(self, url):
        self.url = url
        self.file = ""


class FakeLLMContext:
    def __init__(self, desc="", provider="vl-model"):
        self.desc = desc
        self.provider = provider
        self.calls = []

    def get_config(self, umo=""):
        return {"provider_ltm_settings": {"image_caption_provider_id": self.provider}}

    async def get_current_chat_provider_id(self, umo=""):
        return "chat-default"

    async def llm_generate(self, chat_provider_id, prompt=None, image_urls=None, **kwargs):
        self.calls.append((chat_provider_id, prompt, list(image_urls or [])))
        return SimpleNamespace(completion_text=self.desc)


class TestImageModeration(unittest.TestCase):
    """图片审核：转述成文字后走规则词，记录里保留描述。"""

    def test_image_urls(self):
        plugin = make_plugin()
        event = FakeEvent("")
        event.message_obj.message = [Image("https://example.com/a.png"), object()]
        self.assertEqual(plugin._image_urls(event), ["https://example.com/a.png"])

    def test_image_keyword_hit(self):
        plugin = make_plugin(
            moderation_enabled=True,
            moderation_images=True,
            moderation_keywords=["加微信"],
            moderation_action="recall",
            moderation_notify=False,
        )
        plugin.api = FakeAPI()
        plugin.context = FakeLLMContext(desc="图片里有一行字：加微信 abc123")

        event = FakeEvent("", sender="M1", name="草草", group="G1")
        event.message_obj.message = [Image("https://example.com/a.png")]

        hit = asyncio.run(plugin._moderate(event))
        self.assertTrue(hit)
        record = plugin.records[-1]
        self.assertEqual(record["kind"], "敏感词")
        self.assertIn("图片中命中规则词「加微信」", record["reason"])
        self.assertIn("[图片]", record["text"])
        self.assertEqual(plugin.context.calls[0][0], "vl-model")

    def test_image_disabled_is_skipped(self):
        plugin = make_plugin(
            moderation_enabled=True,
            moderation_images=False,
            moderation_keywords=["加微信"],
            moderation_notify=False,
        )
        plugin.context = FakeLLMContext(desc="图片里有一行字：加微信 abc123")
        event = FakeEvent("", sender="M1", group="G1")
        event.message_obj.message = [Image("https://example.com/a.png")]

        hit = asyncio.run(plugin._moderate(event))
        self.assertFalse(hit)
        self.assertEqual(plugin.records, [])

    def test_caption_failure_fails_open(self):
        plugin = make_plugin(
            moderation_enabled=True,
            moderation_images=True,
            moderation_keywords=["加微信"],
            moderation_notify=False,
        )

        class Broken(FakeLLMContext):
            async def llm_generate(self, *args, **kwargs):
                raise RuntimeError("模型超时")

        plugin.context = Broken()
        event = FakeEvent("", sender="M1", group="G1")
        event.message_obj.message = [Image("https://example.com/a.png")]

        hit = asyncio.run(plugin._moderate(event))
        self.assertFalse(hit)


class TestRecallMute(unittest.TestCase):
    """「撤回并禁言」要两个动作都执行，任何一个失败都记为失败。"""

    def test_recall_and_mute(self):
        plugin = make_plugin(
            moderation_enabled=True,
            moderation_action="recall_mute",
            moderation_keywords=["加微信"],
            moderation_notify=False,
            moderation_mute_seconds=600,
        )
        api = FakeAPI()
        plugin.api = api
        event = FakeEvent("加微信 abc123", sender="M1", name="草草", group="G1", message_id="MSG_9")

        self.assertTrue(asyncio.run(plugin._moderate(event)))
        names = [call[0] for call in api.calls]
        self.assertIn("recall", names)
        self.assertIn("mute", names)
        mute_call = [c for c in api.calls if c[0] == "mute"][0]
        self.assertEqual(mute_call[3], 600)
        record = plugin.records[-1]
        self.assertEqual(record["action"], "recall_mute")
        self.assertTrue(record["ok"])

    def test_mute_failure_marks_false(self):
        plugin = make_plugin(
            moderation_enabled=True,
            moderation_action="recall_mute",
            moderation_keywords=["加微信"],
            moderation_notify=False,
        )
        api = FakeAPI(result=ApiResult(ok=False, status=403, message="无权限"))
        plugin.api = api
        event = FakeEvent("加微信 abc123", sender="M1", group="G1", message_id="MSG_9")

        self.assertTrue(asyncio.run(plugin._moderate(event)))
        self.assertFalse(plugin.records[-1]["ok"])


class TestMediaStore(unittest.TestCase):
    """审核原图要能留存，并通过接口按文件名取回。"""

    def _event_payload(self, plugin, handler, query):
        class FakeRequest:
            def __init__(self):
                self.query = dict(query)

        old = (
            plugin_main.WEB_API_READY,
            plugin_main.request,
            plugin_main.json_response,
            plugin_main.error_response,
        )
        plugin_main.WEB_API_READY = True
        plugin_main.request = FakeRequest()
        plugin_main.json_response = lambda data: {"ok": True, "data": data}
        plugin_main.error_response = lambda msg, **kw: {"ok": False, "error": msg}
        try:
            return asyncio.run(handler())
        finally:
            (
                plugin_main.WEB_API_READY,
                plugin_main.request,
                plugin_main.json_response,
                plugin_main.error_response,
            ) = old

    def test_save_and_serve(self):
        plugin = make_plugin()
        src = Path(tempfile.mkdtemp()) / "a.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        name = plugin._save_record_image("1794748548305", str(src))
        self.assertEqual(name, "1794748548305.png")
        self.assertTrue((plugin._media_dir() / name).is_file())

        resp = self._event_payload(plugin, plugin.page_media, {"f": name})
        self.assertEqual(resp.media_type, "image/png")
        self.assertEqual(resp.body[:8], b"\x89PNG\r\n\x1a\n")

    def test_media_data_base64(self):
        plugin = make_plugin()
        src = Path(tempfile.mkdtemp()) / "a.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\nhello")
        name = plugin._save_record_image("999", str(src))

        resp = self._event_payload(plugin, plugin.page_media_data, {"f": name})
        payload = resp["data"]["data"]
        self.assertEqual(payload["mime"], "image/png")
        self.assertEqual(payload["size"], 13)
        self.assertEqual(base64.b64decode(payload["data"]), b"\x89PNG\r\n\x1a\nhello")

    def test_media_data_rejects_bad_name(self):
        plugin = make_plugin()
        resp = self._event_payload(
            plugin, plugin.page_media_data, {"f": "../../config.json"}
        )
        self.assertFalse(resp["ok"])

    def test_reject_bad_name(self):
        plugin = make_plugin()
        resp = self._event_payload(plugin, plugin.page_media, {"f": "../config.json"})
        self.assertFalse(resp["ok"])
        self.assertIn("不合法", resp["error"])

    def test_missing_file(self):
        plugin = make_plugin()
        resp = self._event_payload(plugin, plugin.page_media, {"f": "nope.png"})
        self.assertFalse(resp["ok"])

    def test_record_stores_image(self):
        plugin = make_plugin(
            moderation_enabled=True,
            moderation_keywords=["加微信"],
            moderation_action="recall",
            moderation_notify=False,
        )
        plugin.api = FakeAPI()
        src = Path(tempfile.mkdtemp()) / "pic.png"
        src.write_bytes(b"png")
        event = FakeEvent("加微信 abc", sender="M1", group="G1", message_id="M9")
        event.set_extra("qqadmin_image_path", str(src))
        event.set_extra("qqadmin_record_text", "[图片] 海报写着加微信")

        self.assertTrue(asyncio.run(plugin._moderate(event)))
        record = plugin.records[-1]
        self.assertTrue(record["image"].endswith(".png"))
        self.assertTrue((plugin._media_dir() / record["image"]).is_file())


class TestReviewStore(unittest.TestCase):
    """放行库：写入、筛选、统计、标注、裁剪。"""

    def _store(self, limit=200):
        return ReviewStore(str(Path(tempfile.mkdtemp()) / "review.db"), limit=limit)

    def test_add_and_filter(self):
        store = self._store()
        store.add(group_id="G1", group_name="一群", user_id="u1", user_name="小明",
                  text="今天天气不错", source="llm")
        store.add(group_id="G2", group_name="二群", user_id="u2", user_name="小红",
                  text="加微信 abc", source="rule_only", risk=0)
        store.add(group_id="G1", group_name="一群", user_id="u3", user_name="小刚",
                  text="图片转述失败", source="vision_error", risk=1, has_image=True)

        items, total = store.list_items()
        self.assertEqual(total, 3)
        # 倒序：最新的在最前
        self.assertEqual(items[0]["text"], "图片转述失败")
        self.assertEqual(items[0]["source"], "vision_error")
        self.assertEqual(items[0]["has_image"], 1)

        hits, total = store.list_items(keyword="微信")
        self.assertEqual(total, 1)
        self.assertEqual(hits[0]["user_name"], "小红")

        hits, total = store.list_items(group="G1")
        self.assertEqual(total, 2)

        hits, total = store.list_items(risky_only=True)
        self.assertEqual(total, 1)
        self.assertEqual(hits[0]["source"], "vision_error")

        hits, total = store.list_items(page=1, size=2)
        self.assertEqual(len(hits), 2)
        self.assertEqual(total, 3)

    def test_mark_and_stats(self):
        store = self._store()
        first = store.add(group_id="G1", text="a", source="llm")
        second = store.add(group_id="G1", text="b", source="llm")

        self.assertEqual(store.stats()["pending"], 2)
        self.assertEqual(store.mark([first], "violation", "漏了广告", "tester"), 1)
        self.assertEqual(store.mark([second], "clean", None, "tester"), 1)

        item = store.get(first)
        self.assertEqual(item["review"], "violation")
        self.assertEqual(item["note"], "漏了广告")
        self.assertEqual(item["reviewer"], "tester")

        stats = store.stats()
        self.assertEqual(stats["violation"], 1)
        self.assertEqual(stats["clean"], 1)
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["total"], 2)

        # 非法结论要报错，避免写坏数据
        with self.assertRaises(ValueError):
            store.mark([first], "whatever")

    def test_prune_and_clear(self):
        store = self._store(limit=100)
        for i in range(120):
            store.add(group_id="G1", text=f"m{i}", source="llm")
        store.prune()
        _, total = store.list_items()
        self.assertEqual(total, 100)
        self.assertEqual(store.clear(), 100)
        self.assertEqual(store.stats()["total"], 0)

    def test_groups_and_days(self):
        store = self._store()
        store.add(group_id="G1", group_name="一群", text="a")
        store.add(group_id="G1", group_name="一群", text="b")
        store.add(group_id="G2", group_name="二群", text="c")
        groups = {row["group_id"]: row for row in store.groups()}
        self.assertEqual(groups["G1"]["n"], 2)
        self.assertEqual(groups["G2"]["group_name"], "二群")
        self.assertEqual(len(store.days()), 1)


class TestReviewCapture(unittest.TestCase):
    """审核通过时，消息要落到复审库，并且来源标注正确。"""

    def _plugin(self, **kwargs):
        kwargs.setdefault("moderation_enabled", True)
        kwargs.setdefault("moderation_use_llm", True)
        kwargs.setdefault("moderation_keywords", [])
        kwargs.setdefault("auto_seed_rules", False)
        kwargs.setdefault("moderation_notify", False)
        plugin = make_plugin(**kwargs)

        # 直接替换实例属性：_moderate 调用 plugin._llm_violation(event, text)
        async def _clean(event, text):
            return False, "", ""

        plugin._llm_violation = _clean
        return plugin

    def test_llm_pass_recorded(self):
        plugin = self._plugin()
        event = FakeEvent("今天天气不错", sender="M1", name="草草", group="G1")
        self.assertFalse(asyncio.run(plugin._moderate(event)))

        items, total = plugin._review.list_items()
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["text"], "今天天气不错")
        self.assertEqual(items[0]["source"], "llm")
        self.assertEqual(items[0]["risk"], 0)
        self.assertEqual(items[0]["review"], "pending")
        self.assertEqual(items[0]["group_id"], "G1")
        self.assertEqual(items[0]["msg_id"], "MSG_1")

    def test_rule_only_pass_recorded(self):
        plugin = self._plugin(moderation_use_llm=False)
        event = FakeEvent("普通聊天", sender="M1", group="G1")
        asyncio.run(plugin._moderate(event))
        items, _ = plugin._review.list_items()
        self.assertEqual(items[0]["source"], "rule_only")

    def test_disabled_recorded(self):
        plugin = self._plugin(moderation_enabled=False)
        event = FakeEvent("审核没开时说的话", sender="M1", group="G1")
        asyncio.run(plugin._moderate(event))
        items, _ = plugin._review.list_items()
        self.assertEqual(items[0]["source"], "disabled")

    def test_vision_failure_flagged_risky(self):
        plugin = self._plugin(moderation_images=True)

        async def one_image(self, event):
            return ["C:/tmp/x.png"]

        async def no_desc(self, event, urls):
            return ""

        plugin._image_sources = types.MethodType(one_image, plugin)
        plugin._describe_images = types.MethodType(no_desc, plugin)

        event = FakeEvent("", sender="M1", group="G1")
        asyncio.run(plugin._moderate(event))

        items, total = plugin._review.list_items()
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["source"], "vision_error")
        self.assertEqual(items[0]["risk"], 1)
        self.assertEqual(items[0]["text"], "[图片]")

    def test_blocked_not_recorded_as_passed(self):
        plugin = self._plugin(moderation_keywords=["加微信"])
        plugin.api = FakeAPI()
        event = FakeEvent("加微信 abc", sender="M1", group="G1")
        self.assertTrue(asyncio.run(plugin._moderate(event)))

        items, total = plugin._review.list_items()
        self.assertEqual(total, 0)

    def test_exempt_message_not_recorded(self):
        plugin = self._plugin(moderation_keywords=[])
        event = FakeEvent("禁言 @某人 10分钟", sender="M1", group="G1")
        asyncio.run(plugin._moderate(event))
        _, total = plugin._review.list_items()
        self.assertEqual(total, 0)


class TestReviewApi(unittest.TestCase):
    """后台复审接口：列表筛选、标注、补撤回。"""

    def _call(self, plugin, handler, query=None, payload=None):
        class FakeRequest:
            def __init__(self):
                self.query = dict(query or {})

            async def json(self, default=None):
                return payload or {}

        old = (
            plugin_main.WEB_API_READY,
            plugin_main.request,
            plugin_main.json_response,
            plugin_main.error_response,
        )
        plugin_main.WEB_API_READY = True
        plugin_main.request = FakeRequest()
        # 真实的 json_response 直接把 data 当响应体，这里保持一致
        plugin_main.json_response = lambda data: data
        plugin_main.error_response = lambda msg, **kw: {"ok": False, "error": msg}
        try:
            return asyncio.run(handler())
        finally:
            (
                plugin_main.WEB_API_READY,
                plugin_main.request,
                plugin_main.json_response,
                plugin_main.error_response,
            ) = old

    def _seed(self, plugin):
        for i in range(25):
            plugin._review.add(
                group_id="G1",
                group_name="一群",
                user_id="u1",
                user_name="小明",
                text=f"第 {i} 条正常消息",
                source="llm",
            )

    def test_list_and_pagination(self):
        plugin = make_plugin()
        self._seed(plugin)

        data = self._call(plugin, plugin.page_review, {"page": 2, "size": 10})["data"]
        self.assertEqual(data["total"], 25)
        self.assertEqual(len(data["items"]), 10)
        self.assertEqual(data["page"], 2)
        self.assertEqual(data["stats"]["pending"], 25)
        self.assertTrue(data["groups"])
        self.assertTrue(data["sources"])

    def test_filter_by_review_state(self):
        plugin = make_plugin()
        self._seed(plugin)
        first = plugin._review.list_items()[0][0]
        plugin._review.mark([first["id"]], "violation", "测试")

        data = self._call(plugin, plugin.page_review, {"review": "violation"})["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["review"], "violation")

        data = self._call(plugin, plugin.page_review, {"q": "第 3 条"})["data"]
        self.assertEqual(data["total"], 1)

    def test_mark_violation_with_recall(self):
        plugin = make_plugin()
        row_id = plugin._review.add(
            group_id="G1", user_id="u1", text="加微信 abc",
            source="llm", msg_id="MSG_9",
        )
        api = FakeAPI()
        plugin.api = api

        data = self._call(
            plugin, plugin.page_review_action,
            payload={"ids": [row_id], "action": "violation", "note": "漏了广告", "recall": True},
        )["data"]
        self.assertEqual(data["updated"], 1)
        self.assertEqual(data["recalled"], 1)

        item = plugin._review.get(row_id)
        self.assertEqual(item["review"], "violation")
        self.assertEqual(item["note"], "漏了广告")
        self.assertIn("recall", [call[0] for call in api.calls])

    def test_bulk_mark(self):
        plugin = make_plugin()
        ids = [plugin._review.add(group_id="G1", text=f"m{i}", source="llm") for i in range(3)]
        data = self._call(
            plugin, plugin.page_review_action,
            payload={"ids": ids, "action": "clean"},
        )["data"]
        self.assertEqual(data["updated"], 3)
        self.assertEqual(plugin._review.stats()["clean"], 3)

    def test_reject_unknown_action(self):
        plugin = make_plugin()
        resp = self._call(
            plugin, plugin.page_review_action,
            payload={"ids": [1], "action": "delete_all"},
        )
        self.assertFalse(resp["ok"])

    def test_clear_all(self):
        plugin = make_plugin()
        self._seed(plugin)
        data = self._call(
            plugin, plugin.page_review_action, payload={"action": "clear_all"}
        )["data"]
        self.assertEqual(data["cleared"], 25)
        self.assertEqual(plugin._review.stats()["total"], 0)


class TestMisjudgeLearn(unittest.TestCase):
    """误判反馈：标一次误判，下次原样出现直接放行。"""

    def test_learn_only_llm_verdicts(self):
        plugin = make_plugin()
        self.assertTrue(
            plugin._learn_misjudge({"model": "LLM", "text": "这个AI发病了"})
        )
        self.assertFalse(
            plugin._learn_misjudge({"model": "规则词", "text": "加微信 abc"})
        )
        self.assertEqual(len(plugin.misjudge_samples), 1)

    def test_dedup_and_normalize(self):
        plugin = make_plugin()
        plugin._learn_misjudge({"model": "LLM", "text": "  Hello   World  "})
        plugin._learn_misjudge({"model": "LLM", "text": "hello world"})
        self.assertEqual(plugin.misjudge_samples, ["hello world"])
        self.assertTrue(plugin._is_misjudge_sample("HELLO   WORLD"))

    def test_moderate_skips_sample(self):
        plugin = make_plugin(
            moderation_enabled=True,
            moderation_keywords=["加微信"],
            moderation_use_llm=False,
            moderation_notify=False,
        )
        plugin.misjudge_samples.append(plugin._normalize_sample("加微信 abc"))
        event = FakeEvent("加微信 abc", sender="M1", group="G1")

        self.assertFalse(asyncio.run(plugin._moderate(event)))
        items, total = plugin._review.list_items()
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["source"], "misjudge_skip")

    def test_switch_off_does_not_learn(self):
        plugin = make_plugin(misjudge_learn=False)
        self.assertFalse(
            plugin._learn_misjudge({"model": "LLM", "text": "正常聊天"})
        )
        self.assertEqual(plugin.misjudge_samples, [])


class TestMisjudgeApi(unittest.TestCase):
    def _call(self, plugin, handler, payload=None):
        class FakeRequest:
            query = {}

            async def json(self, default=None):
                return payload or {}

        old = (
            plugin_main.WEB_API_READY,
            plugin_main.request,
            plugin_main.json_response,
            plugin_main.error_response,
        )
        plugin_main.WEB_API_READY = True
        plugin_main.request = FakeRequest()
        plugin_main.json_response = lambda data: data
        plugin_main.error_response = lambda msg, **kw: {"ok": False, "error": msg}
        try:
            return asyncio.run(handler())
        finally:
            (
                plugin_main.WEB_API_READY,
                plugin_main.request,
                plugin_main.json_response,
                plugin_main.error_response,
            ) = old

    def test_list_delete_clear(self):
        plugin = make_plugin()
        plugin.misjudge_samples = ["a 正常", "b 也正常"]

        data = self._call(plugin, plugin.page_misjudge)["data"]
        self.assertEqual(len(data["items"]), 2)

        data = self._call(
            plugin, plugin.page_misjudge_action,
            {"action": "delete", "text": "a 正常"},
        )["data"]
        self.assertEqual(data["removed"], 1)
        self.assertEqual(plugin.misjudge_samples, ["b 也正常"])

        data = self._call(plugin, plugin.page_misjudge_action, {"action": "clear"})["data"]
        self.assertEqual(data["removed"], 1)
        self.assertEqual(plugin.misjudge_samples, [])

    def test_reject_unknown(self):
        plugin = make_plugin()
        resp = self._call(plugin, plugin.page_misjudge_action, {"action": "whatever"})
        self.assertFalse(resp["ok"])
