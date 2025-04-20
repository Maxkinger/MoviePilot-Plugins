import random
import time
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas.types import EventType
from app.utils.http import RequestUtils
from app.schemas import Notification, NotificationType, MessageChannel


class HDHiveSignin(_PluginBase):
    # 插件元数据
    plugin_name = "HDHive自动签到"
    plugin_desc = "HDHive站点自动签到，支持随机延迟和通知"
    plugin_icon = "https://hdhive.online/favicon.ico"
    plugin_version = "1.0"
    plugin_author = "YourName"
    author_url = "https://github.com/yourname"
    plugin_config_prefix = "hdhivesignin_"
    plugin_order = 2
    auth_level = 1

    # 私有属性
    _enabled = False
    _cron = None
    _cookie = None
    _onlyonce = False
    _notify = False
    _random_delay = None
    _history_days = 30
    _clear = False
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._cookie = config.get("cookie")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._random_delay = config.get("random_delay")
            self._history_days = config.get("history_days")
            self._clear = config.get("clear")

        if self._clear:
            self.del_data('history')
            self._clear = False
            self.__update_config()

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("HDHive签到服务启动，立即运行一次")
            self._scheduler.add_job(func=self.__signin, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=5),
                                    name="HDHive签到")
            self._onlyonce = False
            self.__update_config()

            if self._scheduler.get_jobs():
                self._scheduler.start()

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "cookie": self._cookie,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "random_delay": self._random_delay,
            "history_days": self._history_days,
            "clear": self._clear
        })

    def __signin(self):
        """执行签到核心逻辑"""
        # 获取CSRF Token
        csrf_token = self.__get_csrf_token()
        
        # 请求头
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Cookie": self._cookie,
            "X-CSRF-TOKEN": csrf_token,
            "Referer": "https://hdhive.online/user"
        }

        try:
            # 发送签到请求
            response = RequestUtils(headers=headers).post_res(
                url="https://hdhive.online/api/customer/user/checkin"
            )

            if not response or response.status_code != 200:
                self.__send_fail("签到失败，接口状态码异常")
                return

            result = response.json()
            if result.get("code") == 200:
                self.__process_success(result)
            else:
                self.__send_fail(f"签到失败：{result.get('message')}")

        except Exception as e:
            logger.error(f"签到请求异常：{str(e)}")
            self.__send_fail("签到请求异常")

    def __get_csrf_token(self) -> str:
        """获取CSRF Token"""
        index_res = RequestUtils(headers={"Cookie": self._cookie}).get_res(
            "https://hdhive.online/user")
        if index_res:
            csrf_match = re.search(
                r'<meta name="csrf-token" content="(.*?)"', index_res.text)
            if csrf_match:
                return csrf_match.group(1)
        return ""

    def __process_success(self, result: dict):
        """处理签到成功"""
        data = result.get("data", {})
        sign_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 构建通知消息
        message = (
            f"签到时间：{sign_time}\n"
            f"用户等级：{data.get('level')}\n"
            f"本次获得：{data.get('bonus', 0)} 积分\n"
            f"连续签到：{data.get('continuous', 0)} 天\n"
            f"总积分：{data.get('total', 0)}"
        )
        
        # 保存记录
        history = self.get_data('history') or []
        history.append({
            "date": sign_time,
            "bonus": data.get('bonus'),
            "continuous": data.get('continuous'),
            "total": data.get('total')
        })
        self.save_data('history', history[-30:])  # 保留最近30条
        
        # 发送通知
        if self._notify:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="🎉 HDHive签到成功",
                text=message
            )
        logger.info(message)

    def __send_fail(self, message: str):
        """处理失败通知"""
        logger.error(message)
        if self._notify:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="❌ HDHive签到失败",
                text=message
            )

    # 以下保持原有框架方法（get_form, get_service等）
    # 需要根据实际需求调整配置表单和历史记录展示

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置表单（参考原有结构修改）"""
        return [
            {
                'component': 'VForm',
                'content': [
                    # 启用开关、通知开关、立即执行等配置项
                    # Cookie输入框
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cookie',
                                            'label': '站点Cookie',
                                            'placeholder': '输入PHPSESSID等认证信息'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "cookie": "",
            "notify": True,
            "cron": "0 8 * * *",
            "random_delay": "300-600"
        }

    def stop_service(self):
        """停止服务"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止服务失败：{str(e)}")
