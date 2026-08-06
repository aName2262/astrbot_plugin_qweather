"""
AstrBot 天气查询插件 v3.1.1
基于高德地图地理编码 + 和风天气 API (API Key 模式)
支持命令调用和 LLM Tool Calling

v3.1.1 变更：
- 插件更名为 astrbot_plugin_qweather
- 修复插件配置面板中配置项描述不显示的问题（_conf_schema.json 对齐标准格式）
- 统一版本号为 3.1.1，正式发布

v3.0.0 变更：
- 将单一的 get_weather 工具拆分为三个独立 LLM 工具：
  get_current_weather（实时天气）/ get_daily_forecast（每日预报）/ get_hourly_forecast（逐小时预报）

v3.0.2 变更：
- 地理编码缓存改为纯文件长期保存（data/plugin_data/astrbot_plugin_qweather/geo_cache.json），不常驻内存
- 每次查询直接读写文件：先查文件，命中直接用；未命中才请求高德，成功后写入文件
- 条数可配置（geo_cache_max_entries，默认 20 条，0 表示不限制），超出自动覆盖最早保存的记录
- 原子写入 + 异步锁，损坏文件自动备份重建，不影响插件运行
"""

import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class WeatherPlugin(Star):
    """天气查询插件，整合高德地图地理编码与和风天气数据。"""

    def __init__(self, context: Context, **kwargs):
        super().__init__(context)

        if "config" in kwargs:
            self._plugin_config = kwargs["config"]
        elif hasattr(self, "config"):
            self._plugin_config = self.config
        else:
            self._plugin_config = None

        self._config_loaded = False

        # 地理编码缓存：纯文件长期保存（不占用内存）
        plugin_name = getattr(self, "name", None) or "astrbot_plugin_qweather"
        self._cache_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        self._cache_file = self._cache_dir / "geo_cache.json"
        self._cache_lock = asyncio.Lock()

        self._try_load_config()

    def _try_load_config(self):
        """安全地从可用来源加载插件配置。"""
        cfg = None

        if self._plugin_config is not None:
            cfg = self._plugin_config
        elif hasattr(self, "config") and self.config is not None:
            cfg = self.config

        if cfg is None:
            logger.debug("[Weather] 配置暂不可用，将在首次调用时重新加载。")
            return

        def _get(key, default=""):
            if isinstance(cfg, dict):
                return cfg.get(key, default)
            return getattr(cfg, key, default)

        self.qweather_api_key = _get("qweather_api_key", "")
        self.qweather_api_host = _get("qweather_api_host", "")
        self.amap_api_key = _get("amap_api_key", "")
        self.default_forecast_days = _get("default_forecast_days", 3)
        self.default_hourly_hours = _get("default_hourly_hours", 12)
        self.request_timeout = _get("request_timeout", 15)
        self.persona_prompt = _get("persona_prompt", "")

        # 地理编码缓存条数上限（0 表示不限制）
        self.geo_cache_max_entries = _get("geo_cache_max_entries", 20)
        if not isinstance(self.geo_cache_max_entries, int) or self.geo_cache_max_entries < 0:
            self.geo_cache_max_entries = 20

        self._config_loaded = True

        if not self.qweather_api_key:
            logger.warning("[Weather] 和风天气 API Key 未配置。")
        if not self.qweather_api_host:
            logger.warning("[Weather] 和风天气 API Host 未配置。")
        if not self.amap_api_key:
            logger.warning("[Weather] 高德地图 API Key 未配置。")

        if self.qweather_api_key and self.qweather_api_host and self.amap_api_key:
            logger.info("[Weather] 天气查询插件初始化成功（API Key 模式）。")
        else:
            logger.error("[Weather] 插件配置不完整，请检查配置项。")

    def _ensure_config(self) -> bool:
        """确保配置已加载，返回是否就绪。"""
        if not self._config_loaded:
            self._try_load_config()
        return self._config_loaded

    # ==================== 命令调用 ====================

    @filter.command("weather", alias={"天气"})
    async def weather_command(self, event: AstrMessageEvent, location: str = ""):
        """查询指定城市的天气信息。

        用法：/weather 北京 或 /weather 北京市朝阳区
        """
        logger.info(f"[Weather] 收到天气查询命令，用户输入: '{location}'")

        if not self._ensure_config():
            yield event.plain_result("❌ 插件配置未加载，请检查插件设置或重启插件。")
            return

        if not location:
            yield event.plain_result(
                "❌ 请提供查询地点。\n用法：/weather <城市名>\n例如：/weather 北京"
            )
            return

        config_check = self._check_config()
        if config_check:
            yield event.plain_result(f"❌ {config_check}")
            return

        try:
            geo_result = await self._geocode(location)
            if not geo_result:
                yield event.plain_result(f"❌ 无法找到地点「{location}」，请检查输入是否正确。")
                return

            lat, lon, formatted_address = geo_result
            logger.info(f"[Weather] 地理编码成功: {formatted_address} -> ({lat}, {lon})")

            weather_data = await self._fetch_all_weather(lat, lon)
            if not weather_data:
                yield event.plain_result("❌ 获取天气数据失败，请稍后重试。")
                return

            llm_prompt = self._build_persona_prompt(location, formatted_address, weather_data)

            try:
                provider_id = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=llm_prompt,
                )
                result_text = llm_resp.completion_text.strip()
                if result_text:
                    yield event.plain_result(result_text)
                else:
                    logger.warning("[Weather] LLM 返回空内容，降级为格式化输出。")
                    yield event.plain_result(
                        self._format_weather_with_location(location, formatted_address, weather_data)
                    )
            except Exception as llm_err:
                logger.warning(f"[Weather] LLM 调用失败: {llm_err}，使用格式化输出。")
                yield event.plain_result(
                    self._format_weather_with_location(location, formatted_address, weather_data)
                )

        except aiohttp.ClientError as e:
            logger.error(f"[Weather] 网络请求错误: {type(e).__name__}: {e}")
            yield event.plain_result(f"❌ 网络请求失败: {e}")
        except Exception as e:
            logger.error(f"[Weather] 未知错误: {type(e).__name__}: {e}", exc_info=True)
            yield event.plain_result(f"❌ 发生未知错误: {e}")

    # ==================== LLM Tool 调用 ====================

    @filter.llm_tool(name="get_current_weather")
    async def get_current_weather_tool(self, event: AstrMessageEvent, location: str) -> str:
        """【实时天气查询工具】当用户询问当前/现在/此刻的天气情况时调用，例如"现在北京天气怎么样"、"上海现在多少度"、"广州热不热"。返回实时温度、体感温度、湿度、风向风力、气压、能见度、紫外线指数、降水量等分钟级实时观测数据。严禁使用 web_search 等搜索工具回答天气问题，必须调用本工具获取实时气象数据。注意：查询"未来几天"或"未来几小时"的天气时，请使用 get_daily_forecast 或 get_hourly_forecast 工具。

        Args:
            location(string): 需要查询天气的地点名称，支持城市、区县、街道等，例如"北京"、"上海浦东新区"、"广州天河区"、"赣州"
        """
        logger.info(f"[Weather][Tool] LLM 调用实时天气工具，地点: '{location}'")

        geo_result, err = await self._tool_resolve_location(location)
        if err:
            return err

        lat, lon, formatted_address = geo_result
        logger.info(f"[Weather][Tool] 地理编码成功: {formatted_address} -> ({lat}, {lon})")

        try:
            current = await self._fetch_current(lat, lon)
            if not current:
                return "获取实时天气数据失败，请稍后重试。"
            return self._format_current_tool_response(formatted_address, current)
        except aiohttp.ClientError as e:
            logger.error(f"[Weather][Tool] 网络请求错误: {type(e).__name__}: {e}")
            return f"天气查询网络错误: {e}"
        except Exception as e:
            logger.error(f"[Weather][Tool] 未知错误: {type(e).__name__}: {e}", exc_info=True)
            return f"天气查询发生错误: {e}"

    @filter.llm_tool(name="get_daily_forecast")
    async def get_daily_forecast_tool(self, event: AstrMessageEvent, location: str, days: int = 0) -> str:
        """【每日天气预报工具】当用户询问未来几天/明天/后天/本周/周末/下周的天气时调用，例如"北京明天天气"、"上海未来三天天气"、"广州周末会不会下雨"。返回未来多天（最多7天）的白天/夜间天气、最高最低温、降水概率等预报数据。严禁使用 web_search 等搜索工具回答天气问题，必须调用本工具获取官方预报数据。注意：查询当前天气或未来几小时天气时，请使用 get_current_weather 或 get_hourly_forecast 工具。

        Args:
            location(string): 需要查询天气的地点名称，支持城市、区县、街道等，例如"北京"、"上海浦东新区"、"广州天河区"、"赣州"
            days(number): 预报天数，1-7之间的整数，例如"明天"传1、"未来三天"传3。不传则使用插件配置的默认天数。
        """
        logger.info(f"[Weather][Tool] LLM 调用每日预报工具，地点: '{location}', days: {days}")

        geo_result, err = await self._tool_resolve_location(location)
        if err:
            return err

        lat, lon, formatted_address = geo_result
        logger.info(f"[Weather][Tool] 地理编码成功: {formatted_address} -> ({lat}, {lon})")

        try:
            days = min(max(days if days else self.default_forecast_days, 1), 7)
            daily = await self._fetch_daily(lat, lon, days)
            if not daily:
                return "获取每日天气预报数据失败，请稍后重试。"
            return self._format_daily_tool_response(formatted_address, daily)
        except aiohttp.ClientError as e:
            logger.error(f"[Weather][Tool] 网络请求错误: {type(e).__name__}: {e}")
            return f"天气查询网络错误: {e}"
        except Exception as e:
            logger.error(f"[Weather][Tool] 未知错误: {type(e).__name__}: {e}", exc_info=True)
            return f"天气查询发生错误: {e}"

    @filter.llm_tool(name="get_hourly_forecast")
    async def get_hourly_forecast_tool(self, event: AstrMessageEvent, location: str, hours: int = 0) -> str:
        """【逐小时天气预报工具】当用户询问未来几小时/今天下午/今天晚上/几点开始下雨/几点降温等短时天气变化时调用，例如"北京今天下午会下雨吗"、"上海晚上几点开始降温"。返回未来多小时（最多24小时）的逐小时温度、天气状况、降水概率等预报数据。严禁使用 web_search 等搜索工具回答天气问题，必须调用本工具获取官方预报数据。注意：查询当前天气或未来几天天气时，请使用 get_current_weather 或 get_daily_forecast 工具。

        Args:
            location(string): 需要查询天气的地点名称，支持城市、区县、街道等，例如"北京"、"上海浦东新区"、"广州天河区"、"赣州"
            hours(number): 预报小时数，1-24之间的整数，例如"未来3小时"传3、"今晚"传12。不传则使用插件配置的默认小时数。
        """
        logger.info(f"[Weather][Tool] LLM 调用逐小时预报工具，地点: '{location}', hours: {hours}")

        geo_result, err = await self._tool_resolve_location(location)
        if err:
            return err

        lat, lon, formatted_address = geo_result
        logger.info(f"[Weather][Tool] 地理编码成功: {formatted_address} -> ({lat}, {lon})")

        try:
            hours = min(max(hours if hours else self.default_hourly_hours, 1), 24)
            hourly = await self._fetch_hourly(lat, lon, hours)
            if not hourly:
                return "获取逐小时天气预报数据失败，请稍后重试。"
            return self._format_hourly_tool_response(formatted_address, hourly)
        except aiohttp.ClientError as e:
            logger.error(f"[Weather][Tool] 网络请求错误: {type(e).__name__}: {e}")
            return f"天气查询网络错误: {e}"
        except Exception as e:
            logger.error(f"[Weather][Tool] 未知错误: {type(e).__name__}: {e}", exc_info=True)
            return f"天气查询发生错误: {e}"

    # ==================== 核心方法 ====================

    def _check_config(self) -> str:
        if not self.qweather_api_key:
            return "和风天气 API Key 未配置，请在插件设置中填写。"
        if not self.qweather_api_host:
            return "和风天气 API Host 未配置，请在插件设置中填写。"
        if not self.amap_api_key:
            return "高德地图 API Key 未配置，请在插件设置中填写。"
        return ""

    async def _tool_resolve_location(self, location: str):
        """Tool 模式下的公共前置步骤：配置校验 + 地理编码（带缓存）。

        返回 (geo_result, error_str)，geo_result 为 (lat, lon, formatted_address) 或 None。
        """
        if not self._ensure_config():
            return None, "错误：插件配置未加载，请检查插件设置。"
        config_check = self._check_config()
        if config_check:
            return None, f"错误：{config_check}"

        geo_result = await self._geocode(location)
        if not geo_result:
            return None, f"无法找到地点「{location}」的天气信息，请确认地名是否正确。"
        return geo_result, ""

    # ==================== 地理编码缓存（纯文件长期保存） ====================

    async def _geocode(self, location: str) -> tuple | None:
        """地理编码：先查缓存文件，命中直接用；未命中才请求高德，成功后写入文件。

        返回 (lat, lon, formatted_address) 或 None。
        """
        async with self._cache_lock:
            cache = self._read_cache_file()
            entry = cache.get(location)
            if entry:
                return entry["lat"], entry["lon"], entry["formatted_address"]

        result = await self._amap_geocode(location)
        if result:
            async with self._cache_lock:
                cache = self._read_cache_file()
                cache[location] = {
                    "lat": result[0],
                    "lon": result[1],
                    "formatted_address": result[2],
                    "updated_at": time.time(),
                }
                self._trim_cache(cache)
                self._write_cache_file(cache)
        return result

    def _read_cache_file(self) -> dict:
        """读取缓存文件，返回 {location: {...}}。

        文件不存在或损坏时返回空 dict；损坏文件自动备份为 .bak 后重建。
        """
        try:
            if self._cache_file.exists():
                with open(self._cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {
                        k: v for k, v in data.items()
                        if isinstance(v, dict) and "lat" in v and "lon" in v
                    }
        except Exception as e:
            logger.warning(f"[Weather] 地理编码缓存文件读取失败，已备份并重建缓存: {e}")
            try:
                if self._cache_file.exists():
                    backup = self._cache_file.with_name(self._cache_file.name + ".bak")
                    self._cache_file.replace(backup)
            except Exception as be:
                logger.error(f"[Weather] 备份损坏的缓存文件失败: {be}")
        return {}

    def _write_cache_file(self, cache: dict):
        """原子写入缓存文件（临时文件 + 替换）。"""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = self._cache_file.with_name(self._cache_file.name + ".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, self._cache_file)
        except Exception as e:
            logger.warning(f"[Weather] 地理编码缓存写入失败（不影响本次查询）: {e}")

    def _trim_cache(self, cache: dict):
        """按 geo_cache_max_entries 覆盖最早的记录（0 表示不限制）。"""
        max_entries = self.geo_cache_max_entries
        if not max_entries:
            return
        if len(cache) <= max_entries:
            return
        # 按 updated_at 从小到大排序，覆盖最旧的
        for key in sorted(
            cache,
            key=lambda k: cache[k].get("updated_at", 0),
        )[: len(cache) - max_entries]:
            del cache[key]

    async def _amap_geocode(self, address: str) -> tuple | None:
        """使用高德地图 API 将地址转换为经纬度坐标。"""
        url = "https://restapi.amap.com/v3/geocode/geo"
        params = {"key": self.amap_api_key, "address": address, "output": "JSON"}

        try:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.error(f"[Weather] 高德API HTTP错误: {resp.status}")
                        return None

                    data = await resp.json()
                    if data.get("status") != "1":
                        logger.error(f"[Weather] 高德API返回错误: {data.get('info', '未知')}")
                        return None

                    geocodes = data.get("geocodes", [])
                    if not geocodes:
                        return None

                    first = geocodes[0]
                    location_str = first.get("location", "")
                    if not location_str:
                        return None

                    lon_str, lat_str = location_str.split(",")
                    lat, lon = float(lat_str), float(lon_str)

                    parts = []
                    for field in ("province", "city", "district", "street", "number"):
                        val = first.get(field, "")
                        if val and val != "[]":
                            parts.append(val)
                    formatted_address = "".join(parts) if parts else address

                    return (lat, lon, formatted_address)

        except Exception as e:
            logger.error(f"[Weather] 高德地理编码异常: {type(e).__name__}: {e}")
            return None

    async def _fetch_all_weather(self, lat: float, lon: float) -> dict | None:
        """命令模式：并发获取实时 + 每日 + 逐小时天气数据。"""
        results = await asyncio.gather(
            self._fetch_current(lat, lon),
            self._fetch_daily(lat, lon, self.default_forecast_days),
            self._fetch_hourly(lat, lon, self.default_hourly_hours),
            return_exceptions=True,
        )

        data = {}
        labels = ["current", "daily", "hourly"]
        for label, result in zip(labels, results):
            if isinstance(result, Exception):
                logger.error(f"[Weather] {label} 请求异常: {result}")
            elif result is not None:
                data[label] = result

        return data if data else None

    async def _fetch_current(self, lat: float, lon: float) -> dict | None:
        """获取实时天气。"""
        lat_str, lon_str = f"{lat:.2f}", f"{lon:.2f}"
        return await self._qweather_request(f"/weather/v1/current/{lat_str}/{lon_str}")

    async def _fetch_daily(self, lat: float, lon: float, days: int) -> dict | None:
        """获取每日预报。"""
        lat_str, lon_str = f"{lat:.2f}", f"{lon:.2f}"
        return await self._qweather_request(
            f"/weather/v1/daily/{lat_str}/{lon_str}",
            {"days": str(days), "localTime": "true"},
        )

    async def _fetch_hourly(self, lat: float, lon: float, hours: int) -> dict | None:
        """获取逐小时预报。"""
        lat_str, lon_str = f"{lat:.2f}", f"{lon:.2f}"
        return await self._qweather_request(
            f"/weather/v1/hourly/{lat_str}/{lon_str}",
            {"hours": str(hours), "localTime": "true"},
        )

    async def _qweather_request(self, path: str, extra_params: dict | None = None) -> dict | None:
        """统一的和风天气 API 请求方法（API Key 模式）。"""
        url = f"https://{self.qweather_api_host}{path}"
        params = {"key": self.qweather_api_key, "lang": "zh"}
        if extra_params:
            params.update(extra_params)

        try:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"[Weather] 和风API错误 {resp.status}: {body[:300]}")
                        return None
                    return await resp.json()
        except aiohttp.ClientError as e:
            logger.error(f"[Weather] 和风API网络错误: {type(e).__name__}: {e}")
            return None

    # ==================== 数据格式化 ====================

    def _extract_current_summary(self, current: dict) -> str:
        """从实时天气数据中提取结构化摘要文本。"""
        condition = current.get("condition", {})
        temp = current.get("temperature", {})
        feels = current.get("feelsLike", {})
        humidity = current.get("humidity", "N/A")
        wind = current.get("wind", {})
        pressure = current.get("pressure", {})
        visibility = current.get("visibility", {})
        uv = current.get("uvIndex", "N/A")

        lines = [
            f"[实时] 天气:{condition.get('text', 'N/A')}, "
            f"温度:{temp.get('value', 'N/A')}{temp.get('unit', '°C')}, "
            f"体感:{feels.get('value', 'N/A')}{feels.get('unit', '°C')}"
        ]

        if isinstance(humidity, (int, float)):
            lines.append(f"  湿度:{int(humidity * 100)}%")

        wind_dir = wind.get("direction", {})
        wind_speed = wind.get("speed", {})
        wind_scale = wind.get("scale", "")
        lines.append(
            f"  风向:{wind_dir.get('compass', 'N/A')}, "
            f"风速:{wind_speed.get('value', 'N/A')}{wind_speed.get('unit', 'm/s')}, "
            f"风力:{wind_scale}级"
        )
        lines.append(
            f"  气压:{pressure.get('value', 'N/A')}{pressure.get('unit', 'hPa')}, "
            f"能见度:{visibility.get('value', 'N/A')}{visibility.get('unit', 'm')}, "
            f"紫外线指数:{uv}"
        )

        return "\n".join(lines)

    def _extract_daily_summary(self, daily: dict) -> str:
        """从每日预报数据中提取结构化摘要文本。"""
        lines = ["[每日预报]"]
        for day in daily.get("days", []):
            date = day.get("forecastStartTime", "")[:10]
            tmax = day.get("temperatureMax", {})
            tmin = day.get("temperatureMin", {})
            dt = day.get("daytime", {})
            nt = day.get("nighttime", {})
            d_text = dt.get("condition", {}).get("text", "N/A")
            n_text = nt.get("condition", {}).get("text", "N/A")
            pop = dt.get("precipitation", {}).get("probability", 0)
            pop_str = f", 降水概率{int(pop * 100)}%" if pop else ""
            lines.append(
                f"  {date}: 白天{d_text}/夜间{n_text}, "
                f"{tmin.get('value', '?')}~{tmax.get('value', '?')}{tmax.get('unit', '°C')}"
                f"{pop_str}"
            )
        return "\n".join(lines)

    def _extract_hourly_summary(self, hourly: dict, limit: int | None = None) -> str:
        """从逐小时预报数据中提取结构化摘要文本。limit 控制最多显示的小时数。"""
        hours = hourly.get("hours", [])
        if limit:
            hours = hours[:limit]

        lines = ["[逐小时预报]"]
        for h in hours:
            ft = h.get("forecastTime", "")
            ht = h.get("temperature", {})
            hc = h.get("condition", {})
            hpop = h.get("precipitation", {}).get("probability", 0)
            time_display = ft[11:16] if len(ft) > 16 else ft
            pop_str = f", 降水概率{int(hpop * 100)}%" if hpop else ""
            lines.append(
                f"  {time_display}: {hc.get('text', 'N/A')}, "
                f"{ht.get('value', '?')}{ht.get('unit', '°C')}{pop_str}"
            )
        return "\n".join(lines)

    def _extract_weather_summary(self, weather_data: dict) -> str:
        """从原始天气数据中提取完整摘要（实时 + 每日 + 逐小时前6小时）。"""
        parts = []

        current = weather_data.get("current")
        if current:
            parts.append(self._extract_current_summary(current))

        daily = weather_data.get("daily")
        if daily:
            parts.append(self._extract_daily_summary(daily))

        hourly = weather_data.get("hourly")
        if hourly:
            parts.append(self._extract_hourly_summary(hourly, limit=6))

        return "\n".join(parts)

    def _build_persona_prompt(self, location: str, formatted_address: str, weather_data: dict) -> str:
        """构建人格化 LLM prompt（命令模式使用）。"""
        weather_summary = self._extract_weather_summary(weather_data)

        persona_section = ""
        if self.persona_prompt:
            persona_section = (
                f"\n\n【你的人设】\n{self.persona_prompt}\n"
                f"请严格保持上述人设的语气、口癖和性格特征来回复。"
            )
        else:
            persona_section = (
                "\n\n【回复风格要求】\n"
                "请用亲切自然、像朋友聊天一样的语气回复。可以适当加入emoji表情，"
                "根据天气情况给出贴心的生活建议（穿衣、出行、防晒、带伞等）。"
                "不要机械地罗列数据，要把关键信息自然地融入对话中。"
                "回复要简洁，控制在3-5句话以内。"
            )

        return (
            f"以下是「{location}」（{formatted_address}）的实时气象数据：\n\n"
            f"{weather_summary}\n"
            f"{persona_section}\n\n"
            f"请基于以上真实数据，用符合你人设的语气向用户汇报天气。"
            f"所有温度、天气状况等信息必须来自上述数据，不得编造。"
            f"直接输出回复内容，不要加任何前缀说明。"
        )

    def _format_weather_with_location(self, location: str, formatted_address: str, weather_data: dict) -> str:
        """命令模式 LLM 失败时的降级输出（含地点）。"""
        weather_summary = self._extract_weather_summary(weather_data)
        return f"📍 {formatted_address}\n\n{weather_summary}"

    def _format_current_tool_response(self, formatted_address: str, current: dict) -> str:
        """实时天气工具返回给 LLM 的结构化数据。"""
        return (
            f"📍 {formatted_address} 的实时天气数据如下：\n\n"
            f"{self._extract_current_summary(current)}\n\n"
            f"请基于以上真实气象数据，用你的人设语气向用户回复天气信息。"
            f"所有数据必须来自上述内容，不得编造或推测。"
        )

    def _format_daily_tool_response(self, formatted_address: str, daily: dict) -> str:
        """每日预报工具返回给 LLM 的结构化数据。"""
        days = daily.get("days", [])
        return (
            f"📍 {formatted_address} 未来{len(days)}天天气预报数据如下：\n\n"
            f"{self._extract_daily_summary(daily)}\n\n"
            f"请基于以上真实气象数据，用你的人设语气向用户回复天气信息。"
            f"所有数据必须来自上述内容，不得编造或推测。"
        )

    def _format_hourly_tool_response(self, formatted_address: str, hourly: dict) -> str:
        """逐小时预报工具返回给 LLM 的结构化数据。"""
        return (
            f"📍 {formatted_address} 逐小时天气预报数据如下：\n\n"
            f"{self._extract_hourly_summary(hourly)}\n\n"
            f"请基于以上真实气象数据，用你的人设语气向用户回复天气信息。"
            f"所有数据必须来自上述内容，不得编造或推测。"
        )

    # ==================== 生命周期 ====================

    async def terminate(self):
        logger.info("[Weather] 天气查询插件已卸载。")
