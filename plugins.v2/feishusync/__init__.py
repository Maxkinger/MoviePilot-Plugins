"""飞书订阅同步插件"""

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.event import eventmanager
from app.log import logger
from app.db.site_oper import SiteOper
from app.plugins import _PluginBase
from app.schemas import Event
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class FeishuSync(_PluginBase):
    """飞书订阅同步插件"""

    plugin_name = "飞书订阅同步"
    plugin_desc = "将 MoviePilot 订阅数据同步到飞书多维表格。"
    plugin_icon = "feishu.png"
    plugin_version = "1.1.1"
    plugin_label = "消息通知"
    plugin_author = "Doctor"
    plugin_config_prefix = "feishusync_"
    plugin_order = 100
    auth_level = 1

    # 私有属性
    _enabled = False
    _app_id = ""
    _app_secret = ""
    _base_token = ""
    _table_id = ""
    _cron = ""
    _run_once = False
    _send_notify = False
    _delete_missing = False
    _auto_sync_on_subscribe_change = False
    _auto_sync_delay = 60
    _auto_sync_timer = None
    _feishu_token = None
    _feishu_token_expires = 0
    _site_id_map = {}
    _last_sync_time = ""
    _last_sync_result = ""

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态"""
        self.stop_service()
        self._enabled = False
        self._feishu_token = None
        self._feishu_token_expires = 0

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._app_id = str(config.get("app_id") or "")
        self._app_secret = str(config.get("app_secret") or "")
        self._base_token = str(config.get("base_token") or "")
        self._table_id = str(config.get("table_id") or "")
        self._cron = str(config.get("cron") or "")
        self._run_once = bool(config.get("run_once"))
        self._send_notify = bool(config.get("send_notify"))
        self._delete_missing = bool(config.get("delete_missing"))
        self._auto_sync_on_subscribe_change = bool(config.get("auto_sync_on_subscribe_change", True))
        try:
            self._auto_sync_delay = max(5, int(config.get("auto_sync_delay") or 60))
        except Exception:
            self._auto_sync_delay = 60
        self._last_sync_time = str(config.get("last_sync_time") or "")
        self._last_sync_result = str(config.get("last_sync_result") or "")

        # 构建站点ID→名称映射
        self._build_site_id_map()

        if self._enabled:
            # 定时服务由 get_service() 返回，MoviePilot 会自动注册
            # 如果勾选了立即运行一次
            if self._run_once:
                self._run_once = False
                saved_config = self.get_config() or {}
                saved_config.update({
                    "run_once": False,
                    "last_sync_time": self._last_sync_time,
                    "last_sync_result": self._last_sync_result,
                })
                self.update_config(saved_config)
                self.sync()

    def get_state(self) -> bool:
        """获取插件启用状态"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表"""
        return [
            {
                "cmd": "/sync_feishu",
                "event": "SyncFeishu",
                "desc": "手动同步订阅数据到飞书",
                "data": {},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表"""
        return [
            {
                "path": "sync",
                "endpoint": self.api_sync,
                "methods": ["GET", "POST"],
                "summary": "手动触发飞书同步",
                "description": "手动触发一次订阅数据同步到飞书",
            },
            {
                "path": "status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "summary": "查询同步状态",
                "description": "查询最近一次同步的状态信息",
            },
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "run_once",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "app_id",
                                            "label": "飞书 App ID",
                                            "placeholder": "cli_xxxxxxxxxxxxxx",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "app_secret",
                                            "label": "飞书 App Secret",
                                            "placeholder": "xxxxxxxxxxxxxxxxxx",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "base_token",
                                            "label": "飞书 Base Token",
                                            "placeholder": "从飞书多维表格URL获取",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "table_id",
                                            "label": "飞书 Table ID",
                                            "placeholder": "从飞书多维表格URL获取",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期 (cron)",
                                            "placeholder": "如 0 6 * * * 表示每天6点",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "send_notify",
                                            "label": "发送通知",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "delete_missing",
                                            "label": "删除飞书多余记录",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_sync_on_subscribe_change",
                                            "label": "订阅变更自动同步",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "auto_sync_delay",
                                            "label": "订阅变更后自动同步延迟（秒）",
                                            "placeholder": "默认60，单位：秒，最小5",
                                            "type": "number",
                                            "suffix": "秒",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "app_id": "",
            "app_secret": "",
            "base_token": "",
            "table_id": "",
            "cron": "",
            "run_once": False,
            "send_notify": False,
            "delete_missing": False,
            "auto_sync_on_subscribe_change": True,
            "auto_sync_delay": 60,
            "last_sync_time": "",
            "last_sync_result": "",
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面"""
        if not self._enabled:
            return None
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "text": f"上次同步: {self._last_sync_time or '未执行'}"
                    + (f"\n结果: {self._last_sync_result}" if self._last_sync_result else ""),
                },
            }
        ]

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        timer = getattr(self, "_auto_sync_timer", None)
        if timer:
            try:
                timer.cancel()
            except Exception:
                pass
        self._auto_sync_timer = None

    # ============================================================
    # 站点ID映射
    # ============================================================

    def _build_site_id_map(self) -> None:
        """从系统配置构建站点ID→名称映射"""
        self._site_id_map = {-1: "115网盘"}
        try:
            for site in SiteOper().list() or []:
                sid = getattr(site, "id", None)
                name = getattr(site, "name", "") or ""
                if sid is not None and name:
                    self._site_id_map[sid] = name
        except Exception as e:
            logger.warning(f"获取站点列表失败: {e}")

    # ============================================================
    # 飞书 API
    # ============================================================

    def _feishu_get_token(self) -> str:
        """获取飞书 tenant_access_token"""
        if time.time() < self._feishu_token_expires:
            return self._feishu_token

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        data = json.dumps({
            "app_id": self._app_id,
            "app_secret": self._app_secret,
        }).encode("utf-8")

        try:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json; charset=utf-8")
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("code") == 0:
                    self._feishu_token = result["tenant_access_token"]
                    self._feishu_token_expires = time.time() + result.get("expire", 7200) - 60
                    return self._feishu_token
                else:
                    raise Exception(f"获取飞书Token失败: {result}")
        except Exception as e:
            logger.error(f"飞书认证失败: {e}")
            raise

    def _feishu_request(self, method: str, path: str, data: bytes = None) -> dict:
        """通用飞书 API 请求"""
        token = self._feishu_get_token()
        url = f"https://open.feishu.cn/open-apis{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            logger.error(f"飞书API请求失败 [{e.code}]: {body}")
            raise Exception(f"HTTP {e.code}: {body}") from e

    def _list_records(self) -> list:
        """列出多维表格现有记录"""
        path = f"/bitable/v1/apps/{self._base_token}/tables/{self._table_id}/records?page_size=500"
        result = self._feishu_request("GET", path)
        if result.get("code") != 0:
            raise Exception(f"查询记录失败: {result}")
        return result.get("data", {}).get("items", [])

    def _batch_create(self, records: list) -> None:
        """批量创建记录（每次最多10条）"""
        logger.info(f"准备批量新增飞书记录: {len(records)} 条")
        for i in range(0, len(records), 10):
            batch = records[i:i+10]
            path = f"/bitable/v1/apps/{self._base_token}/tables/{self._table_id}/records/batch_create"
            data = json.dumps({"records": batch}).encode("utf-8")
            try:
                result = self._feishu_request("POST", path, data)
            except Exception as e:
                raise Exception(f"批量创建飞书记录失败(batch_create): {e}") from e
            if result.get("code") != 0:
                raise Exception(f"批量创建飞书记录失败(batch_create): {result}")
            time.sleep(0.5)

    def _batch_update(self, records: list) -> None:
        """批量更新记录（每次最多10条）"""
        logger.info(f"准备批量更新飞书记录: {len(records)} 条")
        for i in range(0, len(records), 10):
            batch = records[i:i+10]
            path = f"/bitable/v1/apps/{self._base_token}/tables/{self._table_id}/records/batch_update"
            data = json.dumps({"records": batch}).encode("utf-8")
            try:
                result = self._feishu_request("POST", path, data)
            except Exception as e:
                raise Exception(f"批量更新飞书记录失败(batch_update): {e}") from e
            if result.get("code") != 0:
                raise Exception(f"批量更新飞书记录失败(batch_update): {result}")
            time.sleep(0.5)

    def _batch_delete(self, record_ids: list) -> None:
        """批量删除记录"""
        logger.info(f"准备批量删除飞书记录: {len(record_ids)} 条")
        for i in range(0, len(record_ids), 10):
            batch = record_ids[i:i+10]
            path = f"/bitable/v1/apps/{self._base_token}/tables/{self._table_id}/records/batch_delete"
            data = json.dumps({"records": batch}).encode("utf-8")
            try:
                result = self._feishu_request("POST", path, data)
            except Exception as e:
                raise Exception(f"批量删除飞书记录失败(batch_delete): {e}") from e
            if result.get("code") != 0:
                raise Exception(f"批量删除飞书记录失败(batch_delete): {result}")
            time.sleep(0.5)

    # ============================================================
    # MoviePilot 数据获取
    # ============================================================

    def _fetch_subscriptions(self) -> list:
        """从 MoviePilot API 获取所有订阅数据"""
        plugin_config = self.get_config() or {}
        domain = plugin_config.get("moviepilot_domain") or "http://127.0.0.1:3001"
        api_key = plugin_config.get("moviepilot_api_token") or getattr(settings, "API_TOKEN", None) or ""
        if not api_key:
            raise Exception("未获取到 MoviePilot API令牌，请检查系统配置")

        domain = str(domain).rstrip("/")
        url = f"{domain}/api/v1/subscribe/list?token={api_key}"
        req = urllib.request.Request(url)
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            logger.error(f"获取订阅数据失败: {e}")
            raise

    # ============================================================
    # 数据转换
    # ============================================================



    def _is_movie_subscribe(self, sub: dict) -> bool:
        """判断订阅是否为电影。"""
        media_type = str(sub.get("type") or "").lower()
        return media_type in {"电影", "movie"}

    def _format_season(self, sub: dict) -> str:
        """将季号格式化为 S00/S01/S02，电影显示为横线。"""
        if self._is_movie_subscribe(sub):
            return "-"
        try:
            season_number = int(sub.get("season") if sub.get("season") is not None else 0)
        except Exception:
            season_number = 0
        return f"S{season_number:02d}"

    def _format_episode_count(self, sub: dict, field: str) -> str:
        """格式化集数字段，电影显示为横线。"""
        if self._is_movie_subscribe(sub):
            return "-"
        value = sub.get(field)
        return str(value) if value is not None else ""

    def _format_subscribe_name(self, sub: dict) -> str:
        """格式化订阅名称，区分特别篇等特殊季。"""
        name = str(sub.get("name") or "")
        if self._is_movie_subscribe(sub):
            return name
        try:
            season_number = int(sub.get("season") if sub.get("season") is not None else 0)
        except Exception:
            season_number = 0
        if season_number == 0:
            return f"{name} 特别篇" if name and "特别篇" not in name else name
        return name

    def _sort_subscriptions_like_mp(self, subscriptions: list) -> list:
        """按 MoviePilot 订阅页常见展示顺序排序。"""
        def sort_key(sub: dict):
            """生成订阅排序键。"""
            date_value = str(sub.get("date") or sub.get("last_update") or "")
            try:
                sid = int(sub.get("id") or 0)
            except Exception:
                sid = 0
            return date_value, sid

        return sorted(subscriptions or [], key=sort_key, reverse=True)

    def _build_records(self, subscriptions: list) -> list:
        """将订阅数据转换为飞书多维表格记录格式"""
        records = []
        state_map = {"R": "运行中", "S": "已暂停", "P": "待处理"}
        sorted_subscriptions = self._sort_subscriptions_like_mp(subscriptions)
        for index, sub in enumerate(sorted_subscriptions, start=1):
            state = state_map.get(sub.get("state", ""), sub.get("state", ""))
            sites = sub.get("sites") or []
            site_names = [self._site_id_map.get(s, f"未知({s})") for s in sites]
            sites_str = ", ".join(site_names) if site_names else ""
            best = "是" if sub.get("best_version") else "否"
            fields = {
                "排序": index,
                "订阅名称": self._format_subscribe_name(sub),
                "年份": str(sub.get("year") or ""),
                "类型": str(sub.get("type") or ""),
                "季数": self._format_season(sub),
                "总集数": self._format_episode_count(sub, "total_episode"),
                "缺少集数": self._format_episode_count(sub, "lack_episode"),
                "状态": str(state or ""),
                "最后更新": str(sub.get("last_update") or ""),
                "最佳版本": str(best or ""),
                "站点ID": str(sites_str or ""),
                "订阅ID": str(sub.get("id") or ""),
            }
            records.append({"fields": fields})
        return records

    # ============================================================
    # 同步逻辑
    # ============================================================

    def sync(self) -> None:
        """主同步流程"""
        logger.info("开始同步订阅数据到飞书...")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            # 1. 获取订阅数据
            subscriptions = self._fetch_subscriptions()
            logger.info(f"获取到 {len(subscriptions)} 条订阅记录")

            if not subscriptions:
                msg = "无订阅数据，同步结束"
                logger.info(msg)
                self._update_sync_status(now_str, msg)
                if self._send_notify:
                    self.post_message(channel=None, title="飞书订阅同步", text=msg)
                return

            # 2. 转换数据
            new_records = self._build_records(subscriptions)
            logger.info(f"已转换 {len(new_records)} 条记录")

            # 3. 同步到飞书
            existing_records = self._list_records()
            logger.info(f"飞书现有 {len(existing_records)} 条记录")

            # 建立订阅ID→飞书record_id映射
            existing_map = {}
            for rec in existing_records:
                sub_id = rec.get("fields", {}).get("订阅ID")
                if sub_id is not None:
                    existing_map[str(sub_id)] = rec["record_id"]

            # 分类
            to_create = []
            to_update = []
            for rec in new_records:
                sub_id = str(rec["fields"].get("订阅ID") or "")
                if sub_id in existing_map:
                    to_update.append({
                        "record_id": existing_map[sub_id],
                        "fields": rec["fields"],
                    })
                else:
                    to_create.append(rec)

            # 需要删除的
            mp_ids = {str(r["fields"].get("订阅ID") or "") for r in new_records}
            to_delete = [
                rec["record_id"] for rec in existing_records
                if str(rec.get("fields", {}).get("订阅ID") or "") not in mp_ids
            ] if self._delete_missing else []
            if not self._delete_missing:
                logger.info("已关闭删除飞书多余记录，跳过删除检测")

            logger.info(f"需新增: {len(to_create)}, 需更新: {len(to_update)}, 需删除: {len(to_delete)}")

            # 执行操作
            if to_create:
                self._batch_create(to_create)
            if to_update:
                self._batch_update(to_update)
            if to_delete:
                self._batch_delete(to_delete)

            result_msg = (
                f"新增 {len(to_create)} 条，更新 {len(to_update)} 条"
                + (f"，删除 {len(to_delete)} 条" if to_delete else "")
            )
            logger.info(f"同步完成: {result_msg}")
            self._update_sync_status(now_str, result_msg)

            if self._send_notify:
                self.post_message(
                    channel=None,
                    title="飞书订阅同步完成",
                    text=f"执行时间: {now_str}\n{result_msg}",
                )

        except Exception as e:
            err_msg = f"同步失败: {str(e)}"
            logger.error(err_msg)
            self._update_sync_status(now_str, err_msg)
            if self._send_notify:
                self.post_message(channel=None, title="飞书订阅同步失败", text=err_msg)

    def _update_sync_status(self, sync_time: str, result: str) -> None:
        """更新同步状态到配置"""
        self._last_sync_time = sync_time
        self._last_sync_result = result
        config = self.get_config() or {}
        config.update({
            "last_sync_time": sync_time,
            "last_sync_result": result,
        })
        self.update_config(config)


    # ============================================================
    # 订阅变更自动同步
    # ============================================================

    def _schedule_auto_sync(self, reason: str) -> None:
        """订阅变更后防抖触发自动同步。"""
        if not self._enabled or not self._auto_sync_on_subscribe_change:
            return
        timer = getattr(self, "_auto_sync_timer", None)
        if timer:
            try:
                timer.cancel()
            except Exception:
                pass
        logger.info(f"检测到订阅变更，{self._auto_sync_delay}秒后自动同步飞书：{reason}")
        self._auto_sync_timer = threading.Timer(self._auto_sync_delay, self._run_auto_sync, args=(reason,))
        self._auto_sync_timer.daemon = True
        self._auto_sync_timer.start()

    def _run_auto_sync(self, reason: str) -> None:
        """执行订阅变更触发的自动同步。"""
        self._auto_sync_timer = None
        if not self._enabled or not self._auto_sync_on_subscribe_change:
            return
        logger.info(f"开始执行订阅变更自动同步：{reason}")
        self.sync()

    @eventmanager.register(EventType.SubscribeAdded)
    def on_subscribe_added(self, event: Event) -> None:
        """监听新增订阅事件并安排飞书同步。"""
        self._schedule_auto_sync("新增订阅")

    @eventmanager.register(EventType.SubscribeModified)
    def on_subscribe_modified(self, event: Event) -> None:
        """监听订阅修改事件并安排飞书同步。"""
        self._schedule_auto_sync("订阅已调整")

    @eventmanager.register(EventType.SubscribeDeleted)
    def on_subscribe_deleted(self, event: Event) -> None:
        """监听订阅删除事件并安排飞书同步。"""
        self._schedule_auto_sync("订阅已删除")

    @eventmanager.register(EventType.SubscribeComplete)
    def on_subscribe_complete(self, event: Event) -> None:
        """监听订阅完成事件并安排飞书同步。"""
        self._schedule_auto_sync("订阅已完成")

    # ============================================================
    # API 端点
    # ============================================================

    def api_sync(self, **kwargs) -> dict:
        """API: 手动触发同步"""
        self.sync()
        return {"success": True, "message": "同步任务已触发"}

    def api_status(self, **kwargs) -> dict:
        """API: 查询同步状态"""
        return {
            "success": True,
            "data": {
                "last_sync_time": self._last_sync_time,
                "last_sync_result": self._last_sync_result,
            },
        }

    # ============================================================
    # 事件处理
    # ============================================================

    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时服务"""
        if not self._enabled or not self._cron:
            return []
        return [
            {
                "id": "feishu_sync",
                "name": "飞书订阅同步",
                "trigger": "cron",
                "func": self.sync,
                "kwargs": {"cron": self._cron},
            }
        ]

    def get_actions(self) -> List[Dict[str, Any]]:
        """返回插件动作列表"""
        return []

    def get_agent_tools(self) -> List[Dict[str, Any]]:
        """返回插件代理工具列表"""
        return []