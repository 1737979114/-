# -*- coding: utf-8 -*-
"""astrbot_plugin_qq_group_admin

适配 **QQ 官方机器人（QQ 开放平台）** 的群管插件。

    @机器人 禁言 @某人 10分钟
    @机器人 解除禁言 @某人
    @机器人 踢人 @某人
    @机器人 撤回 / 撤回 3
    @机器人 禁言状态
    @机器人 全员禁言 / 解除全员禁言
    @机器人 我的id

前提：

1. 机器人在群里被设为**管理员**，否则平台返回 40062003
2. 插件里填了 AppID / AppSecret（用于换 token）
3. 撤回只能撤回 2 分钟内的消息，所以插件会缓存最近收到的消息来定位目标
4. 踢人是内邀接口，没开通会返回 11253

和 NapCat（aiocqhttp）那套不是一个协议，本插件只处理 qq_official 平台。
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star

try:
    from astrbot.api import AstrBotConfig
except Exception:  # pragma: no cover
    AstrBotConfig = dict  # type: ignore

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from admin_core import (  # noqa: E402
    ALL_DEFAULT_RULES,
    RULE_CATEGORY,
    CachedMessage,
    MessageCache,
    format_blacklist,
    format_duration,
    format_group_info,
    format_join_requests,
    format_kick_ok,
    format_member_detail,
    format_members,
    format_mute_ok,
    format_recall_ok,
    format_status,
    format_unmute_ok,
    format_usage,
    parse_duration,
    parse_recall_count,
)
from lexicon import DEFAULT_CATEGORIES, Lexicon  # noqa: E402
from official_api import ApiError, QQOfficialAPI  # noqa: E402

try:  # 可视化视图依赖新版 AstrBot，没有也不影响命令
    from astrbot.api.web import error_response, json_response, request

    WEB_API_READY = True
except Exception:  # pragma: no cover
    WEB_API_READY = False
    error_response = json_response = request = None  # type: ignore

PLUGIN_NAME = "astrbot_plugin_qq_group_admin"

AT_PREFIX_RE = re.compile(r"^@\S+\s*")

MUTE_RE = re.compile(r"^[/!！]?\s*(?:禁言|mute)\s*(.*)$", re.IGNORECASE)
UNMUTE_RE = re.compile(
    r"^[/!！]?\s*(?:解除禁言|解禁|取消禁言|unmute)\s*(.*)$", re.IGNORECASE
)
KICK_RE = re.compile(r"^[/!！]?\s*(?:踢人|踢出|移除成员|kick)\s*(.*)$", re.IGNORECASE)
RECALL_RE = re.compile(
    r"^[/!！]?\s*(?:撤回|撤回消息|recall)\s*(\d{0,2})\s*$", re.IGNORECASE
)
STATUS_RE = re.compile(r"^[/!！]?\s*(?:禁言状态|查询禁言)\s*$", re.IGNORECASE)
GLOBAL_OFF_RE = re.compile(
    r"^[/!！]?\s*(?:解除全员禁言|关闭全员禁言)\s*$", re.IGNORECASE
)
GLOBAL_ON_RE = re.compile(r"^[/!！]?\s*(?:全员禁言|全体禁言)\s*$", re.IGNORECASE)
WHOAMI_RE = re.compile(r"^[/!！]?\s*(?:我的id|我的ID|whoami)\s*$", re.IGNORECASE)

GROUP_INFO_RE = re.compile(r"^[/!！]?\s*(?:群信息|本群信息|群资料)\s*$", re.IGNORECASE)
MEMBERS_RE = re.compile(r"^[/!！]?\s*(?:成员列表|群成员|成员)\s*$", re.IGNORECASE)
MEMBER_INFO_RE = re.compile(
    r"^[/!！]?\s*(?:查成员|成员详情|成员信息)\s*$", re.IGNORECASE
)
BLACKLIST_RE = re.compile(r"^[/!！]?\s*(?:黑名单|群黑名单)\s*$", re.IGNORECASE)
JOIN_RE = re.compile(r"^[/!！]?\s*(?:入群申请|待审申请)\s*$", re.IGNORECASE)

#: 密钥掩码，前端原样提交时不覆盖真实值
SECRET_MASK = "••••••••"

#: 智能审核的默认提示词
DEFAULT_MODERATION_PROMPT = (
    "你是群聊内容审核员。判断下面这条消息是否属于广告引流、辱骂攻击、色情低俗、"
    "诈骗或刷屏。只输出 JSON，不要解释：\n"
    '{"violation":true,"reason":"广告"} 或 {"violation":false,"reason":""}\n'
    "消息：{text}"
)

#: 审核跳过的命令词，避免把管理命令本身判成违规
MODERATION_SKIP_PREFIXES = (
    "禁言",
    "解除禁言",
    "解禁",
    "踢人",
    "撤回",
    "禁言状态",
    "全员禁言",
    "解除全员禁言",
    "我的id",
)


class QQGroupAdminPlugin(Star):
    """QQ 官方机器人群管。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)

        self.config = config if config is not None else {}

        #: 可视化页展示的操作记录，只存内存
        self.logs: list[dict[str, Any]] = []
        self._started_at = time.time()

        #: 违规记录 / 每日统计 / 见过的群 / 刷屏计数 / 规则命中次数
        self.records: list[dict[str, Any]] = []
        self.daily: dict[str, dict[str, int]] = {}
        self.groups: dict[str, dict[str, Any]] = {}
        self.spam: dict[str, list[float]] = {}
        self.rule_hits: dict[str, int] = {}

        self._apply_config()
        self._register_web_apis()

        logger.info(
            "[qqadmin] 已加载 | 启用=%s | 凭据=%s | 管理员名单=%s | 消息缓存=%s",
            self.enable,
            "已配置" if self.api.ready else "缺 AppID/AppSecret",
            len(self.admin_openids) or "未限制",
            self.cache_enabled,
        )
        if not self.api.ready:
            logger.warning("[qqadmin] 缺少 AppID/AppSecret，群管命令会直接提示配置")
        if not self.admin_openids:
            logger.warning(
                "[qqadmin] 未配置 admin_openids，群内任何人都能触发管理命令（建议配置）"
            )

    def _apply_config(self) -> None:
        """从配置对象读取全部设置。保存设置后重新调用即可热更新。"""
        self.enable = self._cfg_bool("enable", True)
        self.admin_openids = set(self._cfg_list("admin_openids"))
        self.app_id = str(self._cfg("app_id", "")).strip()
        self.app_secret = str(self._cfg("app_secret", "")).strip()
        self.timeout = self._cfg_int("timeout", 10, minimum=5, maximum=60)
        self.verify_ssl = self._cfg_bool("verify_ssl", True)

        self.api = QQOfficialAPI(
            app_id=self.app_id,
            app_secret=self.app_secret,
            timeout=self.timeout,
            verify_ssl=self.verify_ssl,
        )

        self.cache_enabled = self._cfg_bool("cache_messages", True)
        size = self._cfg_int("cache_size", 30, minimum=5, maximum=200)
        ttl = self._cfg_int("cache_ttl", 600, minimum=120, maximum=3600)
        if getattr(self, "cache", None) is None:
            self.cache = MessageCache(limit=size, ttl=ttl)
        else:
            self.cache.limit = size
            self.cache.ttl = ttl

        # ---- 智能审核 ----
        self.moderation_enabled = self._cfg_bool("moderation_enabled", False)
        self.moderation_action = str(
            self._cfg("moderation_action", "recall") or "recall"
        ).strip()
        if self.moderation_action not in ("warn", "recall", "mute"):
            self.moderation_action = "recall"
        self.moderation_mute_seconds = self._cfg_int(
            "moderation_mute_seconds", 600, minimum=60, maximum=30 * 86400
        )
        self.auto_seed_rules = self._cfg_bool("auto_seed_rules", True)
        configured = self._cfg_list("moderation_keywords")
        # 首次安装自动装一套预置规则，开箱就能拦
        if not configured and self.auto_seed_rules:
            configured = list(ALL_DEFAULT_RULES)
        self.moderation_keywords = configured
        self.moderation_whitelist = set(self._cfg_list("moderation_whitelist"))
        self.moderation_use_llm = self._cfg_bool("moderation_use_llm", True)
        self.moderation_prompt = str(self._cfg("moderation_prompt", "") or "").strip()
        self.llm_provider_id = str(self._cfg("llm_provider_id", "") or "").strip()

        # ---- 刷屏监控 ----
        self.spam_enabled = self._cfg_bool("spam_enabled", False)
        self.spam_limit = self._cfg_int("spam_limit", 10, minimum=3, maximum=50)
        self.spam_window = self._cfg_int("spam_window", 60, minimum=10, maximum=600)

        # ---- 生效平台（为空表示所有 QQ 官方实例） ----
        self.platform_ids = set(self._cfg_list("platform_ids"))

        # ---- 分类词库 ----
        self.lexicon_categories = self._cfg_list("lexicon_categories") or list(
            DEFAULT_CATEGORIES
        )
        if getattr(self, "lexicon", None) is None:
            self.lexicon = Lexicon(_PLUGIN_DIR / "lexicon.json")
            self.lexicon.load()
        self.lexicon.set_enabled(self.lexicon_categories)

        # ---- 功能开关 ----
        self.mute_enabled = self._cfg_bool("mute_enabled", True)
        self.kick_enabled = self._cfg_bool("kick_enabled", True)
        self.recall_enabled = self._cfg_bool("recall_enabled", True)
        self.whole_ban_enabled = self._cfg_bool("whole_ban_enabled", True)
        self.member_query_enabled = self._cfg_bool("member_query_enabled", True)
        self.join_request_enabled = self._cfg_bool("join_request_enabled", True)

        # ---- 审核增强 ----
        self.moderation_admin_exempt = self._cfg_bool("moderation_admin_exempt", True)
        self.moderation_notify = self._cfg_bool("moderation_notify", True)
        self.moderation_fail_open = self._cfg_bool("moderation_fail_open", True)
        self.group_whitelist = set(self._cfg_list("group_whitelist"))

        # ---- 高级 ----
        self.max_records = self._cfg_int("max_records", 500, minimum=50, maximum=2000)
        self.command_rate_limit = self._cfg_int(
            "command_rate_limit", 0, minimum=0, maximum=60
        )
        self._cmd_last: dict[str, float] = getattr(self, "_cmd_last", {})

    # ------------------------------------------------------------------ 可视化视图

    def _register_web_apis(self) -> None:
        """注册管理页用到的接口。路由必须以插件名开头。"""
        if not WEB_API_READY:
            logger.debug("[qqadmin] 当前 AstrBot 不支持 register_web_api，跳过管理页")
            return

        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            return

        routes = (
            (f"/{PLUGIN_NAME}/status", self.page_status, ["GET"], "群管状态"),
            (f"/{PLUGIN_NAME}/overview", self.page_overview, ["GET"], "总览数据"),
            (f"/{PLUGIN_NAME}/records", self.page_records, ["GET"], "违规记录"),
            (
                f"/{PLUGIN_NAME}/records/action",
                self.page_records_action,
                ["POST"],
                "记录操作",
            ),
            (f"/{PLUGIN_NAME}/rules", self.page_rules_list, ["GET"], "规则列表"),
            (f"/{PLUGIN_NAME}/rules", self.page_rules, ["POST"], "规则增删"),
            (f"/{PLUGIN_NAME}/groups", self.page_groups, ["GET"], "群列表"),
            (f"/{PLUGIN_NAME}/members", self.page_members, ["GET"], "成员查询"),
            (f"/{PLUGIN_NAME}/options", self.page_options, ["GET"], "可选平台与模型"),
            (f"/{PLUGIN_NAME}/spam", self.page_spam, ["GET"], "刷屏监控"),
            (f"/{PLUGIN_NAME}/config", self.page_save_config, ["POST"], "保存设置"),
            (f"/{PLUGIN_NAME}/test", self.page_test, ["POST"], "测试连接"),
            (f"/{PLUGIN_NAME}/logs", self.page_logs, ["GET"], "操作记录"),
        )
        for route, handler, methods, desc in routes:
            try:
                register(route, handler, methods, desc)
            except Exception as exc:
                logger.warning(f"[qqadmin] 注册路由 {route} 失败：{exc}")

    def _config_dict(self) -> dict[str, Any]:
        return {
            "enable": self.enable,
            "app_id": self.app_id,
            "app_secret": SECRET_MASK if self.app_secret else "",
            "app_secret_set": bool(self.app_secret),
            "admin_openids": sorted(self.admin_openids),
            "cache_messages": self.cache_enabled,
            "cache_size": self.cache.limit,
            "cache_ttl": self.cache.ttl,
            "timeout": self.timeout,
            "verify_ssl": self.verify_ssl,
            "moderation_enabled": self.moderation_enabled,
            "moderation_action": self.moderation_action,
            "moderation_mute_seconds": self.moderation_mute_seconds,
            "moderation_keywords": self.moderation_keywords,
            "moderation_whitelist": sorted(self.moderation_whitelist),
            "moderation_use_llm": self.moderation_use_llm,
            "moderation_prompt": self.moderation_prompt,
            "llm_provider_id": self.llm_provider_id,
            "spam_enabled": self.spam_enabled,
            "spam_limit": self.spam_limit,
            "spam_window": self.spam_window,
            "platform_ids": sorted(self.platform_ids),
            "lexicon_categories": sorted(self.lexicon_categories),
            "lexicon_total": self.lexicon.total_enabled,
            "mute_enabled": self.mute_enabled,
            "kick_enabled": self.kick_enabled,
            "recall_enabled": self.recall_enabled,
            "whole_ban_enabled": self.whole_ban_enabled,
            "member_query_enabled": self.member_query_enabled,
            "join_request_enabled": self.join_request_enabled,
            "moderation_admin_exempt": self.moderation_admin_exempt,
            "moderation_notify": self.moderation_notify,
            "moderation_fail_open": self.moderation_fail_open,
            "group_whitelist": sorted(self.group_whitelist),
            "max_records": self.max_records,
            "command_rate_limit": self.command_rate_limit,
            "moderation_keywords_count": len(self.moderation_keywords),
        }

    async def page_status(self):
        """管理页首屏数据。"""
        if not WEB_API_READY:
            return None
        return json_response(
            {
                "ok": True,
                "data": {
                    "config": self._config_dict(),
                    "ready": self.api.ready,
                    "uptime_minutes": int((time.time() - self._started_at) / 60),
                    "total_actions": len(self.logs),
                    "recent": list(reversed(self.logs[-8:])),
                },
            }
        )

    async def page_logs(self):
        if not WEB_API_READY:
            return None
        return json_response(
            {"ok": True, "data": {"logs": list(reversed(self.logs[-50:]))}}
        )

    async def page_overview(self):
        """总览：统计卡 + 近 7 天趋势 + 类型/时段分布。"""
        if not WEB_API_READY:
            return None

        today = time.strftime("%Y-%m-%d")
        bucket = self.daily.get(today, {})
        blocked = int(bucket.get("blocked", 0))
        passed = int(bucket.get("passed", 0))

        days = []
        for offset in range(6, -1, -1):
            key = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
            node = self.daily.get(key, {})
            days.append(
                {
                    "date": key[5:],
                    "blocked": int(node.get("blocked", 0)),
                    "passed": int(node.get("passed", 0)),
                }
            )

        kinds = Counter(str(item.get("kind") or "其他") for item in self.records)
        hours = [0] * 24
        for item in self.records:
            hour = int(item.get("hour") or 0)
            if 0 <= hour < 24:
                hours[hour] += 1

        return json_response(
            {
                "ok": True,
                "data": {
                    "cards": {
                        "today_blocked": blocked,
                        "today_checked": blocked + passed,
                        "today_passed": passed,
                        "total_records": len(self.records),
                        "rules": len(self.moderation_keywords),
                        "groups": len(self.groups),
                        "admins": len(self.admin_openids),
                        "cache": sum(len(v) for v in self.cache._data.values()),
                    },
                    "daily": days,
                    "kinds": [
                        {"name": name, "count": count}
                        for name, count in kinds.most_common(6)
                    ],
                    "hours": hours,
                    "recent": list(reversed(self.records[-8:])),
                },
            }
        )

    async def page_records(self):
        """违规记录，支持分页和按类型筛选。"""
        if not WEB_API_READY:
            return None

        page = request.query.get("page", 1, int) or 1
        size = min(max(request.query.get("size", 20, int) or 20, 5), 100)
        kind = str(request.query.get("kind", "") or "").strip()

        items = self.records
        if kind:
            items = [item for item in items if item.get("kind") == kind]

        ordered = list(reversed(items))
        start = max(0, (page - 1) * size)
        return json_response(
            {
                "ok": True,
                "data": {
                    "items": ordered[start : start + size],
                    "total": len(ordered),
                    "page": page,
                    "size": size,
                    "kinds": sorted(
                        {str(item.get("kind") or "其他") for item in self.records}
                    ),
                },
            }
        )

    async def page_records_action(self):
        """标记记录：确认 / 误判 / 删除。"""
        if not WEB_API_READY:
            return None

        payload = await request.json(default={}) or {}
        record_id = str((payload or {}).get("id") or "")
        action = str((payload or {}).get("action") or "")

        target = next(
            (item for item in self.records if item.get("id") == record_id), None
        )
        if target is None:
            return error_response("记录不存在")

        if action == "delete":
            self.records.remove(target)
        elif action in ("confirm", "misjudge"):
            target["status"] = action
        else:
            return error_response("未知操作")

        return json_response({"ok": True, "data": {"total": len(self.records)}})

    async def page_rules_list(self):
        """规则列表：自定义词 + 已启用分类的词库，支持分页、搜索、按分类筛。"""
        if not WEB_API_READY:
            return None

        page = request.query.get("page", 1, int) or 1
        size = min(max(request.query.get("size", 50, int) or 50, 10), 200)
        keyword = str(request.query.get("q", "") or "").strip()
        category = str(request.query.get("cat", "") or "").strip()

        items: list[dict[str, Any]] = []

        if category in ("", "all", "custom"):
            for word in self.moderation_keywords:
                if keyword and keyword not in word:
                    continue
                items.append(
                    {
                        "word": word,
                        "category": "自定义",
                        "hits": int(self.rule_hits.get(word, 0)),
                        "source": "custom",
                    }
                )

        if category in ("", "all") or category in self.lexicon.enabled:
            names = (
                [category]
                if category not in ("", "all")
                else list(self.lexicon.enabled)
            )
            for name in names:
                for word in self.lexicon.words.get(name, ()):
                    if keyword and keyword not in word:
                        continue
                    items.append(
                        {
                            "word": word,
                            "category": self.lexicon.label(name),
                            "hits": int(self.rule_hits.get(word, 0)),
                            "source": "lexicon",
                        }
                    )

        total = len(items)
        start = max(0, (page - 1) * size)
        return json_response(
            {
                "ok": True,
                "data": {
                    "items": items[start : start + size],
                    "total": total,
                    "page": page,
                    "size": size,
                    "stats": self.lexicon.stats(),
                    "enabled_total": self.lexicon.total_enabled,
                    "custom_total": len(self.moderation_keywords),
                },
            }
        )

    async def page_rules(self):
        """规则词库增删。"""
        if not WEB_API_READY:
            return None

        payload = await request.json(default={}) or {}
        action = str((payload or {}).get("action") or "")
        word = str((payload or {}).get("word") or "").strip()[:30]

        if action == "seed":
            for word in ALL_DEFAULT_RULES:
                if word not in self.moderation_keywords:
                    self.moderation_keywords.append(word)
        elif action == "clear":
            self.moderation_keywords.clear()
            # 记住用户主动清空，别下次重载又自动装回来
            self.auto_seed_rules = False
        elif action == "add":
            if not word:
                return error_response("关键词不能为空")
            if word in self.moderation_keywords:
                return error_response("这个词已经在库里了")
            if len(self.moderation_keywords) >= 200:
                return error_response("规则词最多 200 条")
            self.moderation_keywords.append(word)
        elif action == "delete":
            if word not in self.moderation_keywords:
                return error_response("这个词不在库里")
            self.moderation_keywords.remove(word)
        else:
            return error_response("未知操作")

        await self._save_config(
            {
                "moderation_keywords": self.moderation_keywords,
                "auto_seed_rules": self.auto_seed_rules,
            }
        )
        return json_response(
            {"ok": True, "data": {"total": len(self.moderation_keywords)}}
        )

    def _rules_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "word": word,
                "hits": int(self.rule_hits.get(word, 0)),
                "category": RULE_CATEGORY.get(word, "自定义"),
            }
            for word in self.moderation_keywords
        ]

    async def page_groups(self):
        """见过的群列表。官方没有「群列表」接口，只能统计收到过消息的群。"""
        if not WEB_API_READY:
            return None

        rows = []
        for group_id, node in self.groups.items():
            last_seen = node.get("last_seen") or 0
            rows.append(
                {
                    "group_id": group_id,
                    "name": node.get("name") or "",
                    "blocked": int(node.get("blocked", 0)),
                    "last_seen": time.strftime("%m-%d %H:%M", time.localtime(last_seen))
                    if last_seen
                    else "-",
                }
            )

        rows.sort(key=lambda item: item["last_seen"], reverse=True)
        return json_response({"ok": True, "data": {"groups": rows, "total": len(rows)}})

    async def page_spam(self):
        """刷屏监控：配置 + 当前窗口内的消息频率。"""
        if not WEB_API_READY:
            return None

        now = time.time()
        window = max(10, self.spam_window)
        rows = []
        for user, times in self.spam.items():
            recent = [item for item in times if now - item <= window]
            if recent:
                rows.append({"user": user, "count": len(recent)})

        rows.sort(key=lambda item: item["count"], reverse=True)
        return json_response(
            {
                "ok": True,
                "data": {
                    "enabled": self.spam_enabled,
                    "limit": self.spam_limit,
                    "window": self.spam_window,
                    "rows": rows[:12],
                },
            }
        )

    async def page_options(self):
        """可选的平台实例和审核模型，供设置页下拉。"""
        if not WEB_API_READY:
            return None

        platforms: list[dict[str, Any]] = []
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            config_path = Path(get_astrbot_data_path()) / "cmd_config.json"
            raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
            for item in raw.get("platform") or []:
                platform_id = str(item.get("id") or "")
                platform_type = str(item.get("type") or "")
                if not platform_id:
                    continue
                enabled = bool(item.get("enable", True))
                platforms.append(
                    {
                        "id": platform_id,
                        "type": platform_type,
                        "enable": enabled,
                        "label": f"{platform_type} · {platform_id}"
                        + ("" if enabled else "（已停用）"),
                    }
                )
        except Exception as exc:
            logger.debug(f"[qqadmin] 读取平台配置失败：{exc}")

        if not platforms:
            try:
                manager = getattr(self.context, "platform_manager", None)
                for inst in getattr(manager, "platform_insts", []) or []:
                    config = getattr(inst, "platform_config", {}) or {}
                    platform_id = str(config.get("id") or "")
                    platform_type = str(config.get("type") or "")
                    if platform_id:
                        platforms.append(
                            {
                                "id": platform_id,
                                "type": platform_type,
                                "enable": True,
                                "label": f"{platform_type} · {platform_id}",
                            }
                        )
            except Exception as exc:
                logger.debug(f"[qqadmin] 读取运行时平台失败：{exc}")

        providers: list[dict[str, Any]] = []
        try:
            for provider in self.context.get_all_providers():
                meta = provider.meta()
                providers.append(
                    {
                        "id": str(meta.id),
                        "model": str(meta.model or ""),
                        "type": str(meta.type or ""),
                    }
                )
        except Exception as exc:
            logger.debug(f"[qqadmin] 读取模型列表失败：{exc}")

        return json_response(
            {"ok": True, "data": {"platforms": platforms, "providers": providers}}
        )

    async def page_members(self):
        """管理页的成员查询。"""
        if not WEB_API_READY:
            return None

        group = str(request.query.get("group", "") or "").strip()
        if not group:
            return error_response("缺少群 openid")

        result = await self.api.group_members(group)
        if not result.ok:
            return error_response(result.hint)
        return json_response({"ok": True, "data": result.data})

    async def page_save_config(self):
        """保存设置：写回配置文件并立即生效，不需要重载插件。"""
        if not WEB_API_READY:
            return None

        payload = await request.json(default={}) or {}
        if not isinstance(payload, dict):
            return error_response("参数格式不对")

        allowed = (
            "enable",
            "app_id",
            "app_secret",
            "admin_openids",
            "cache_messages",
            "cache_size",
            "cache_ttl",
            "timeout",
            "verify_ssl",
            "moderation_enabled",
            "moderation_action",
            "moderation_mute_seconds",
            "moderation_keywords",
            "moderation_whitelist",
            "moderation_use_llm",
            "moderation_prompt",
            "llm_provider_id",
            "spam_enabled",
            "spam_limit",
            "spam_window",
            "platform_ids",
            "lexicon_categories",
            "mute_enabled",
            "kick_enabled",
            "recall_enabled",
            "whole_ban_enabled",
            "member_query_enabled",
            "join_request_enabled",
            "moderation_admin_exempt",
            "moderation_notify",
            "moderation_fail_open",
            "group_whitelist",
            "max_records",
            "command_rate_limit",
        )
        updates = {key: payload[key] for key in allowed if key in payload}

        # 掩码原样提交时不要覆盖真实密钥
        if updates.get("app_secret") in (SECRET_MASK, "", None):
            updates.pop("app_secret", None)

        for key in (
            "admin_openids",
            "moderation_keywords",
            "moderation_whitelist",
            "platform_ids",
            "lexicon_categories",
            "group_whitelist",
        ):
            if key not in updates:
                continue
            value = updates[key]
            if isinstance(value, str):
                value = re.split(r"[,，\n]+", value)
            if not isinstance(value, (list, tuple, set)):
                return error_response(f"{key} 格式不对")
            updates[key] = [str(item).strip() for item in value if str(item).strip()][
                :100
            ]

        for key in ("cache_size", "cache_ttl", "timeout", "moderation_mute_seconds"):
            if key in updates:
                try:
                    updates[key] = int(updates[key])
                except (TypeError, ValueError):
                    return error_response(f"{key} 必须是数字")

        if updates.get("moderation_action") not in (None, "warn", "recall", "mute"):
            return error_response("处置方式只支持 warn / recall / mute")

        if "app_id" in updates:
            value = str(updates["app_id"]).strip()
            # 防止管理页读取失败、表单为空时一键把凭据清掉
            if not value and self.app_id:
                return error_response("AppID 不能保存为空，如需更换请填新的")
            updates["app_id"] = value
        if "app_secret" in updates:
            value = str(updates["app_secret"]).strip()
            if not value and self.app_secret:
                return error_response("AppSecret 不能保存为空，如需更换请填新的")
            updates["app_secret"] = value

        saved = await self._save_config(updates)
        try:
            await self.api.close()
        except Exception:
            pass
        self._apply_config()
        self._log_action("settings", "系统", "保存设置", saved)

        return json_response(
            {
                "ok": True,
                "data": {
                    "saved": saved,
                    "config": self._config_dict(),
                    "ready": self.api.ready,
                },
            }
        )

    async def page_test(self):
        """测试凭据是否有效，可选验证某个群的管理权限。"""
        if not WEB_API_READY:
            return None

        payload = await request.json(default={}) or {}
        group = str((payload or {}).get("group_openid") or "").strip()

        if not self.api.ready:
            return error_response("还没填 AppID / AppSecret")

        try:
            await self.api._token_value()
        except ApiError as exc:
            return error_response(f"凭据无效：{exc}")
        except Exception as exc:
            return error_response(f"请求失败：{exc}")

        if not group:
            return json_response(
                {
                    "ok": True,
                    "data": {
                        "token_ok": True,
                        "scope_ok": None,
                        "message": "凭据有效。填一个群 openid 可以顺便验证管理权限。",
                    },
                }
            )

        result = await self.api.query_restrict(group)
        if result.ok:
            return json_response(
                {
                    "ok": True,
                    "data": {
                        "token_ok": True,
                        "scope_ok": True,
                        "message": "凭据有效，机器人有这个群的管理权限。",
                    },
                }
            )

        return json_response(
            {
                "ok": True,
                "data": {
                    "token_ok": True,
                    "scope_ok": False,
                    "message": f"凭据有效，但管理权限验证失败：{result.hint}",
                },
            }
        )

    async def _save_config(self, updates: dict[str, Any]) -> bool:
        """优先走 AstrBot 的保存接口，不行就自己写配置文件。"""
        saver = getattr(self.config, "save_config_async", None)
        if callable(saver):
            try:
                await saver(updates)
                for key, value in updates.items():
                    self.config[key] = value
                return True
            except Exception as exc:
                logger.warning(f"[qqadmin] 保存配置失败，改为直接写文件：{exc}")

        try:
            for key, value in updates.items():
                self.config[key] = value
            path = self._config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            data = dict(self.config) if isinstance(self.config, dict) else updates
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return True
        except Exception as exc:
            logger.warning(f"[qqadmin] 写配置文件失败：{exc}")
            return False

    @staticmethod
    def _config_path() -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            base = Path(get_astrbot_data_path())
        except Exception:
            base = _PLUGIN_DIR.parent.parent
        return base / "config" / f"{PLUGIN_NAME}_config.json"

    def _log_action(self, action: str, target: str, detail: str, ok: bool) -> None:
        self.logs.append(
            {
                "time": time.strftime("%H:%M:%S"),
                "date": time.strftime("%Y-%m-%d"),
                "action": action,
                "target": target,
                "detail": detail,
                "ok": ok,
            }
        )
        if len(self.logs) > 100:
            del self.logs[: len(self.logs) - 100]

    # ------------------------------------------------------------------ 智能审核

    async def _moderate(self, event: AstrMessageEvent) -> bool:
        """检查一条群消息，命中违规就按配置处置。返回 True 表示已拦截。"""
        if not self.enable or not self._is_allowed(event):
            return False
        if self._skip_moderation(event):
            return False

        text = self._clean_text(event)
        self._track_group(event)

        spam_reason = self._check_spam(event)
        if spam_reason:
            await self._punish(event, "刷屏", spam_reason, "频率统计")
            return True

        if not self.moderation_enabled:
            self._mark_passed()
            return False

        reason = ""
        kind = ""
        model = ""

        word = self._keyword_hit(text)
        if word:
            reason = f"命中规则词「{word}」"
            kind = "敏感词"
            model = "规则词"
            self.rule_hits[word] = self.rule_hits.get(word, 0) + 1
        elif self.moderation_use_llm:
            violation, llm_reason, category = await self._llm_violation(event, text)
            if violation:
                reason = llm_reason or "疑似违规"
                kind = category or "AI判定"
                model = "LLM"

        if not reason:
            self._mark_passed()
            return False

        await self._punish(event, kind, reason, model)
        return True

    def _skip_moderation(self, event: AstrMessageEvent) -> bool:
        text = self._clean_text(event)
        if len(text) < 2:
            return True

        if self.group_whitelist and self._group_id(event) in self.group_whitelist:
            return True

        sender = self._sender_id(event)
        if sender and sender in self.moderation_whitelist:
            return True
        if sender and sender == self._self_id(event):
            return True
        if (
            self.moderation_admin_exempt
            and self.admin_openids
            and sender in self.admin_openids
        ):
            return True
        if text.startswith(MODERATION_SKIP_PREFIXES):
            return True
        return False

    def _keyword_hit(self, text: str) -> str:
        for word in self.moderation_keywords:
            if word and word in text:
                return word
        return ""

    def _mark_passed(self) -> None:
        day = self._today_bucket()
        day["passed"] = day.get("passed", 0) + 1

    def _today_bucket(self) -> dict[str, int]:
        key = time.strftime("%Y-%m-%d")
        return self.daily.setdefault(key, {"blocked": 0, "passed": 0})

    def _track_group(self, event: AstrMessageEvent) -> None:
        group = self._group_id(event)
        if not group:
            return
        node = self.groups.setdefault(
            group, {"group_id": group, "name": "", "last_seen": 0, "blocked": 0}
        )
        node["last_seen"] = time.time()

        name = ""
        try:
            group_obj = getattr(event.message_obj, "group", None)
            name = str(getattr(group_obj, "group_name", "") or "")
        except Exception:
            name = ""
        if name:
            node["name"] = name

    def _check_spam(self, event: AstrMessageEvent) -> str:
        if not self.spam_enabled:
            return ""

        sender = self._sender_id(event)
        if not sender:
            return ""

        now = time.time()
        window = max(10, self.spam_window)
        bucket = [item for item in self.spam.get(sender, []) if now - item <= window]
        bucket.append(now)
        self.spam[sender] = bucket[-50:]

        if len(bucket) > self.spam_limit:
            return f"{window} 秒内发了 {len(bucket)} 条消息"
        return ""

    def _record(
        self,
        kind: str,
        reason: str,
        model: str,
        action: str,
        ok: bool,
        event: AstrMessageEvent,
    ) -> None:
        now = time.time()
        item = {
            "id": f"{int(now * 1000)}",
            "time": time.strftime("%m-%d %H:%M:%S", time.localtime(now)),
            "date": time.strftime("%Y-%m-%d", time.localtime(now)),
            "hour": int(time.strftime("%H", time.localtime(now))),
            "group": self._group_id(event),
            "user": self._sender_id(event),
            "name": self._sender_name(event) or "群友",
            "kind": kind,
            "reason": reason,
            "model": model,
            "action": action,
            "ok": ok,
            "status": "pending",
        }
        self.records.append(item)
        if len(self.records) > self.max_records:
            del self.records[: len(self.records) - self.max_records]

        day = self._today_bucket()
        day["blocked"] = day.get("blocked", 0) + 1

        group = item["group"]
        if group in self.groups:
            self.groups[group]["blocked"] = self.groups[group].get("blocked", 0) + 1

    async def _llm_violation(
        self, event: AstrMessageEvent, text: str
    ) -> tuple[bool, str, str]:
        try:
            provider_id = self.llm_provider_id
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(
                    umo=str(getattr(event, "unified_msg_origin", "") or "")
                )
            if not provider_id:
                return False, "", ""

            template = self.moderation_prompt or DEFAULT_MODERATION_PROMPT
            prompt = template.replace("{text}", text[:200])
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            raw = str(getattr(response, "completion_text", "") or "")

            match = re.search(r"\{[\s\S]*\}", raw)
            if not match:
                return False, "", ""
            data = json.loads(match.group(0))
            if not isinstance(data, dict):
                return False, "", ""

            violation = bool(data.get("violation"))
            reason = str(data.get("reason") or "").strip()[:30]
            category = str(data.get("category") or "").strip()[:10]
            return violation, reason, category
        except Exception as exc:
            logger.debug(f"[qqadmin] 审核模型调用失败：{exc}")
            # fail_open=True 时放行，否则按违规处理
            return (not self.moderation_fail_open), "", ""

    async def _punish(
        self, event: AstrMessageEvent, kind: str, reason: str, model: str = ""
    ) -> None:
        group = self._group_id(event)
        sender = self._sender_id(event)
        name = self._sender_name(event) or "该成员"
        action = self.moderation_action
        ok = True

        try:
            if action == "recall":
                message_id = self._message_id(event)
                if message_id:
                    result = await self.api.recall_message(group, message_id)
                    ok = result.ok
                if self.moderation_notify:
                    await event.send(
                        event.plain_result(f"已撤回一条消息（{reason}）。")
                    )
            elif action == "mute":
                result = await self.api.mute_member(
                    group, sender, self.moderation_mute_seconds
                )
                ok = result.ok
                if self.moderation_notify:
                    await event.send(
                        event.plain_result(
                            f"已禁言 {name}（{reason}），"
                            f"时长 {format_duration(self.moderation_mute_seconds)}。"
                        )
                    )
            else:
                if self.moderation_notify:
                    await event.send(
                        event.plain_result(f"{name} 请注意言辞（{reason}）。")
                    )
        except Exception as exc:
            ok = False
            logger.warning(f"[qqadmin] 审核处置失败：{exc}")

        logger.info(f"[qqadmin] 审核命中 {name}：{reason} -> {action}（{ok}）")
        self._record(kind, reason, model, action, ok, event)
        self._log_action("moderation", name, f"{reason} / {action}", ok)

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "开")
        return bool(value)

    def _cfg_int(
        self, key: str, default: int, minimum: int = 0, maximum: int = 10**6
    ) -> int:
        try:
            value = int(self._cfg(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    def _cfg_list(self, key: str) -> list[str]:
        value = self._cfg(key, [])
        if isinstance(value, str):
            return [
                item.strip() for item in re.split(r"[,，\n]+", value) if item.strip()
            ]
        if not isinstance(value, (list, tuple, set)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    # ------------------------------------------------------------------ 入口

    @filter.command("禁言")
    async def cmd_mute(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """禁言：/禁言 @某人 10分钟"""
        async for result in self._dispatch(event):
            yield result

    @filter.command("撤回")
    async def cmd_recall(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """撤回：/撤回 3 撤回最近几条消息"""
        async for result in self._dispatch(event):
            yield result

    @filter.command("踢人")
    async def cmd_kick(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """踢人：/踢人 @某人"""
        async for result in self._dispatch(event):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        try:
            self._cache_message(event)

            if await self._moderate(event):
                return

            async for result in self._dispatch(event):
                yield result
        except Exception as exc:  # pragma: no cover
            logger.exception(f"[qqadmin] 处理消息时出现未捕获异常：{exc}")

    # ------------------------------------------------------------------ 分发

    async def _dispatch(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        if not self.enable:
            return

        text = self._clean_text(event)

        if not (
            WHOAMI_RE.match(text)
            or MUTE_RE.match(text)
            or UNMUTE_RE.match(text)
            or KICK_RE.match(text)
            or RECALL_RE.match(text)
            or STATUS_RE.match(text)
            or GLOBAL_ON_RE.match(text)
            or GLOBAL_OFF_RE.match(text)
            or GROUP_INFO_RE.match(text)
            or MEMBERS_RE.match(text)
            or MEMBER_INFO_RE.match(text)
            or BLACKLIST_RE.match(text)
            or JOIN_RE.match(text)
        ):
            return

        if self._already_handled(event):
            return

        event.stop_event()

        blocked = self._feature_blocked(text)
        if blocked:
            yield event.plain_result(blocked)
            return

        if self._rate_limited(event):
            yield event.plain_result("操作太频繁，缓几秒再试。")
            return

        if not self._is_official(event):
            yield event.plain_result(
                "这个命令只支持 QQ 官方机器人（QQ 开放平台）。\n"
                "NapCat / OneBot 那套请用对应的群管插件。"
            )
            return

        if not self._is_allowed(event):
            return

        if WHOAMI_RE.match(text):
            yield event.plain_result(self._whoami(event))
            return

        if not self._allowed(event):
            yield event.plain_result(
                "你没有权限执行群管操作。把下面这个 id 加进插件的 admin_openids 才能用：\n"
                f"{self._sender_id(event)}"
            )
            return

        if not self.api.ready:
            yield event.plain_result(
                "还没配置 AppID / AppSecret。\n"
                "在 QQ 开放平台 → 开发设置里拿到，填进插件配置后再试。"
            )
            return

        try:
            if GLOBAL_OFF_RE.match(text):
                async for result in self._global_mute(event, False):
                    yield result
            elif GLOBAL_ON_RE.match(text):
                async for result in self._global_mute(event, True):
                    yield result
            elif STATUS_RE.match(text):
                async for result in self._status(event):
                    yield result
            elif GROUP_INFO_RE.match(text):
                async for result in self._group_info(event):
                    yield result
            elif MEMBERS_RE.match(text):
                async for result in self._members(event):
                    yield result
            elif MEMBER_INFO_RE.match(text):
                async for result in self._member_detail(event):
                    yield result
            elif BLACKLIST_RE.match(text):
                async for result in self._blacklist(event):
                    yield result
            elif JOIN_RE.match(text):
                async for result in self._join_requests(event):
                    yield result
            elif UNMUTE_RE.match(text):
                async for result in self._unmute(event):
                    yield result
            elif MUTE_RE.match(text):
                async for result in self._mute(event, text):
                    yield result
            elif KICK_RE.match(text):
                async for result in self._kick(event):
                    yield result
            elif RECALL_RE.match(text):
                async for result in self._recall(event, text):
                    yield result
        except ApiError as exc:
            logger.warning(f"[qqadmin] 接口异常：{exc}")
            yield event.plain_result(f"调用失败：{exc}")
        except Exception as exc:  # pragma: no cover
            logger.exception(f"[qqadmin] 执行命令异常：{exc}")
            yield event.plain_result("执行出错了，看下日志。")

    # ------------------------------------------------------------------ 具体命令

    def _feature_blocked(self, text: str) -> str:
        """功能开关：关掉的命令直接提示，不往下走。"""
        if (MUTE_RE.match(text) or UNMUTE_RE.match(text)) and not self.mute_enabled:
            return "禁言功能已在设置里关闭。"
        if KICK_RE.match(text) and not self.kick_enabled:
            return "踢人功能已在设置里关闭。"
        if RECALL_RE.match(text) and not self.recall_enabled:
            return "撤回功能已在设置里关闭。"
        if (
            GLOBAL_ON_RE.match(text) or GLOBAL_OFF_RE.match(text)
        ) and not self.whole_ban_enabled:
            return "全员禁言已在设置里关闭。"
        if (
            GROUP_INFO_RE.match(text)
            or MEMBERS_RE.match(text)
            or MEMBER_INFO_RE.match(text)
        ) and not self.member_query_enabled:
            return "成员查询已在设置里关闭。"
        if JOIN_RE.match(text) and not self.join_request_enabled:
            return "入群申请查询已在设置里关闭。"
        return ""

    def _rate_limited(self, event: AstrMessageEvent) -> bool:
        if self.command_rate_limit <= 0:
            return False

        key = self._sender_id(event)
        if not key:
            return False

        now = time.time()
        last = self._cmd_last.get(key, 0.0)
        if now - last < self.command_rate_limit:
            return True

        self._cmd_last[key] = now
        if len(self._cmd_last) > 500:
            self._cmd_last.clear()
        return False

    async def _mute(
        self, event: AstrMessageEvent, text: str
    ) -> AsyncGenerator[MessageEventResult, None]:
        target, name = self._target_member(event)
        if not target:
            yield event.plain_result("请 @ 要禁言的成员，例如：禁言 @某人 10分钟")
            return

        seconds = parse_duration(text)
        result = await self.api.mute_member(self._group_id(event), target, seconds)
        if result.ok:
            logger.info(f"[qqadmin] 禁言 {name} {format_duration(seconds)}")
            self._log_action("mute", name, format_duration(seconds), True)
            yield event.plain_result(format_mute_ok(name, seconds))
        else:
            self._log_action("mute", name, result.hint, False)
            yield event.plain_result(f"禁言失败：{result.hint}")

    async def _unmute(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        target, name = self._target_member(event)
        if not target:
            yield event.plain_result("请 @ 要解除禁言的成员。")
            return

        result = await self.api.unmute_member(self._group_id(event), target)
        if result.ok:
            self._log_action("unmute", name, "", True)
            yield event.plain_result(format_unmute_ok(name))
        else:
            self._log_action("unmute", name, result.hint, False)
            yield event.plain_result(f"解除禁言失败：{result.hint}")

    async def _kick(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        target, name = self._target_member(event)
        if not target:
            yield event.plain_result("请 @ 要踢出的成员。")
            return

        result = await self.api.remove_members(self._group_id(event), [target])
        if result.ok:
            logger.info(f"[qqadmin] 移出成员 {name}")
            self._log_action("kick", name, "", True)
            yield event.plain_result(format_kick_ok(name))
        else:
            self._log_action("kick", name, result.hint, False)
            yield event.plain_result(f"踢人失败：{result.hint}")

    async def _recall(
        self, event: AstrMessageEvent, text: str
    ) -> AsyncGenerator[MessageEventResult, None]:
        group = self._group_id(event)
        count = parse_recall_count(text)
        current_id = self._message_id(event)

        targets = self.cache.recent(group, count, exclude_id=current_id)
        if not targets:
            yield event.plain_result(
                "没找到可撤回的消息。\n"
                "撤回只能处理 2 分钟内的消息，且需要机器人能收到群消息"
                "（开放平台要开启「接收所有消息」）。"
            )
            return

        success = 0
        last_hint = ""
        for item in reversed(targets):
            result = await self.api.recall_message(group, item.message_id)
            if result.ok:
                success += 1
            else:
                last_hint = result.hint

        if success:
            logger.info(f"[qqadmin] 撤回 {success} 条")
            self._log_action("recall", "群聊", f"{success}/{len(targets)} 条", True)
            message = format_recall_ok(success)
            if success < len(targets):
                message += f"\n有 {len(targets) - success} 条没撤掉：{last_hint}"
            yield event.plain_result(message)
        else:
            yield event.plain_result(f"撤回失败：{last_hint or '未知原因'}")

    async def _status(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        result = await self.api.query_restrict(self._group_id(event))
        if result.ok:
            yield event.plain_result(format_status(result.data))
        else:
            yield event.plain_result(f"查询失败：{result.hint}")

    async def _group_info(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        result = await self.api.group_info(self._group_id(event))
        if result.ok:
            yield event.plain_result(format_group_info(result.data))
        else:
            yield event.plain_result(f"查询群信息失败：{result.hint}")

    async def _members(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        result = await self.api.group_members(self._group_id(event))
        if result.ok:
            yield event.plain_result(format_members(result.data))
        else:
            yield event.plain_result(f"查询成员失败：{result.hint}")

    async def _member_detail(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        target, _name = self._target_member(event)
        if not target:
            yield event.plain_result("请 @ 要查询的成员。")
            return

        result = await self.api.member_info(self._group_id(event), target)
        if result.ok:
            yield event.plain_result(format_member_detail(result.data))
        else:
            yield event.plain_result(f"查询失败：{result.hint}")

    async def _blacklist(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        result = await self.api.blacklist(self._group_id(event))
        if result.ok:
            yield event.plain_result(format_blacklist(result.data))
        else:
            yield event.plain_result(f"查询黑名单失败：{result.hint}")

    async def _join_requests(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        result = await self.api.join_requests(self._group_id(event))
        if result.ok:
            yield event.plain_result(format_join_requests(result.data))
        else:
            yield event.plain_result(f"查询入群申请失败：{result.hint}")

    async def _global_mute(
        self, event: AstrMessageEvent, enable: bool
    ) -> AsyncGenerator[MessageEventResult, None]:
        result = await self.api.set_global_mute(self._group_id(event), enable)
        if result.ok:
            self._log_action("global", "全员", "开启" if enable else "解除", True)
            yield event.plain_result(
                "已开启全员禁言。" if enable else "已解除全员禁言。"
            )
        else:
            yield event.plain_result(
                f"设置失败：{result.hint}\n"
                "全员禁言的设置接口目前没有正式文档，可能还没对你这个应用开放；"
                "可以先用「禁言状态」确认平台侧是否支持。"
            )

    # ------------------------------------------------------------------ 数据提取

    def _cache_message(self, event: AstrMessageEvent) -> None:
        if not self.cache_enabled or not self._is_allowed(event):
            return

        message_id = self._message_id(event)
        group = self._group_id(event)
        if not message_id or not group:
            return

        sender = self._sender_id(event)
        if sender and sender == self._self_id(event):
            return

        self.cache.add(
            CachedMessage(
                message_id=message_id,
                group=group,
                sender=sender,
                name=self._sender_name(event),
                text=self._clean_text(event)[:80],
                ts=time.time(),
            )
        )

    def _target_member(self, event: AstrMessageEvent) -> tuple[str, str]:
        """从原始消息的 mentions 里取被 @ 的成员 openid。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        mentions = getattr(raw, "mentions", None) or []

        for mention in mentions:
            if isinstance(mention, dict):
                is_you = bool(mention.get("is_you"))
                openid = str(mention.get("id") or mention.get("member_openid") or "")
                name = str(mention.get("username") or "")
            else:
                is_you = bool(getattr(mention, "is_you", False))
                openid = str(
                    getattr(mention, "id", "") or getattr(mention, "member_openid", "")
                )
                name = str(getattr(mention, "username", "") or "")

            if is_you or not openid:
                continue
            if openid == self._self_id(event):
                continue
            return openid, name

        return "", ""

    @staticmethod
    def _is_official(event: AstrMessageEvent) -> bool:
        try:
            name = str(event.get_platform_name() or "").lower()
        except Exception:
            name = ""
        return "qq_official" in name

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """平台过滤：只处理 QQ 官方，且命中用户勾选的平台实例。"""
        if not self._is_official(event):
            return False
        if not self.platform_ids:
            return True

        try:
            platform_id = str(event.platform_meta.id or "")
        except Exception:
            platform_id = ""
        return not platform_id or platform_id in self.platform_ids

    @staticmethod
    def _group_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or "")
        except Exception:
            return ""

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    @staticmethod
    def _sender_name(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_name() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _self_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.message_obj.self_id or "")
        except Exception:
            return ""

    @staticmethod
    def _message_id(event: AstrMessageEvent) -> str:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        return str(getattr(raw, "id", "") or "")

    @staticmethod
    def _clean_text(event: AstrMessageEvent) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        return AT_PREFIX_RE.sub("", text).strip()

    def _allowed(self, event: AstrMessageEvent) -> bool:
        if not self.admin_openids:
            return True
        return self._sender_id(event) in self.admin_openids

    def _whoami(self, event: AstrMessageEvent) -> str:
        openid = self._sender_id(event)
        role = "管理员名单内" if openid in self.admin_openids else "不在管理员名单"
        if not self.admin_openids:
            role = "未启用管理员名单（所有人都能用）"
        return f"你的 openid：\n{openid}\n{role}\n\n{format_usage()}"

    @staticmethod
    def _already_handled(event: AstrMessageEvent) -> bool:
        try:
            if event.get_extra("qqadmin_handled"):
                return True
            event.set_extra("qqadmin_handled", True)
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------ 生命周期

    async def terminate(self) -> None:
        self.cache._data.clear()
        try:
            await self.api.close()
        except Exception as exc:
            logger.debug(f"[qqadmin] 关闭客户端出错：{exc}")
        logger.info("[qqadmin] 插件已卸载")
