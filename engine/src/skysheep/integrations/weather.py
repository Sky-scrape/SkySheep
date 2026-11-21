"""现实天气：免密钥的 Open-Meteo 接口 + 进程内 30 分钟缓存。

数据流只有两段，且都由用户显式开启：

1. 用户填写的城市名 → 地理编码接口，拿经纬度；
2. 经纬度 → 当前天气 / 昼夜 / 日出日落。

不做 IP 定位、不读系统位置、不保存城市以外的信息；关闭 ``[factory] weather``
时这个模块一次网络请求都不会发。天气是纯展示信息，失败只回落晴天效果，
绝不能影响工厂的调度或运行账本。
"""

from __future__ import annotations

import asyncio
import time

import httpx

from ..network import NetworkConfig, make_async_httpx_client

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "SkySheep-Weather/1.0 (+https://sheepsky.com)"

CACHE_TTL_S = 30 * 60
ERROR_TTL_S = 2 * 60
STALE_TTL_S = 5 * 60
MAX_CACHE_ENTRIES = 8

# WMO weather code → 场景只认的几种天气。阈值按 Open-Meteo 文档：
# https://open-meteo.com/en/docs（weather_code 表）
_KIND_BY_CODE = {
    0: "clear",
    1: "partly", 2: "partly",
    3: "cloudy",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle", 56: "drizzle", 57: "drizzle",
    61: "rain", 63: "rain", 65: "rain", 66: "rain", 67: "rain",
    71: "snow", 73: "snow", 75: "snow", 77: "snow",
    80: "rain", 81: "rain", 82: "rain",
    85: "snow", 86: "snow",
    95: "thunder", 96: "thunder", 99: "thunder",
}
KIND_LABELS = {
    "clear": "晴",
    "partly": "多云",
    "cloudy": "阴",
    "fog": "雾",
    "drizzle": "毛毛雨",
    "rain": "雨",
    "snow": "雪",
    "thunder": "雷雨",
    "unknown": "未知",
}


def weather_kind(code) -> str:
    """WMO code → 场景天气种类；未知 code 归到 unknown。"""
    try:
        return _KIND_BY_CODE.get(int(code), "unknown")
    except (TypeError, ValueError):
        return "unknown"


def _clock(value) -> str:
    """把 Open-Meteo 的本地 ISO 时间裁成 HH:MM；解析不出返回空串。"""
    text = str(value or "")
    if "T" in text and len(text) >= 16:
        return text[11:16]
    return ""


def _error(city: str, error: str) -> dict:
    return {
        "ok": False,
        "kind": "unknown",
        "label": KIND_LABELS["unknown"],
        "code": None,
        "temperature_c": None,
        "is_day": None,
        "sunrise": "",
        "sunset": "",
        "location": {"name": city, "country": "", "latitude": None,
                     "longitude": None, "timezone": ""},
        "fetched_at": time.time(),
        "cached": False,
        "stale": False,
        "error": error,
    }


class WeatherService:
    """天气查询服务。一个实例在进程内缓存最近几次城市结果。"""

    def __init__(self, network: NetworkConfig | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.network = network or NetworkConfig()
        self._transport = transport
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    def set_network(self, network: NetworkConfig | None) -> None:
        """设置页改代理 / 证书后热更新；缓存不受影响（数据与出口无关）。"""
        self.network = network or NetworkConfig()

    def _client(self, url: str) -> httpx.AsyncClient:
        return make_async_httpx_client(
            self.network,
            url=url,
            transport=self._transport,
            timeout=8.0,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
        )

    async def _get_json(self, url: str, params: dict) -> dict:
        async with self._client(url) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("天气服务返回了非对象响应")
            return data

    async def _fetch(self, city: str) -> dict:
        try:
            geo = await self._get_json(
                GEOCODE_URL,
                {"name": city, "count": 1, "language": "zh", "format": "json"},
            )
            hits = geo.get("results") or []
            if not hits:
                return _error(city, "找不到这个城市，换一个更常见的写法试试")
            place = hits[0]
            try:
                lat = float(place["latitude"])
                lon = float(place["longitude"])
            except (KeyError, TypeError, ValueError):
                return _error(city, "城市坐标解析失败")
            name = str(place.get("name") or city)
            country = str(place.get("country") or "")
            admin = str(place.get("admin1") or "")
            timezone = str(place.get("timezone") or "")

            forecast = await self._get_json(
                FORECAST_URL,
                {
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,weather_code,is_day",
                    "daily": "sunrise,sunset",
                    "timezone": "auto",
                    "forecast_days": 1,
                },
            )
            current = forecast.get("current") or {}
            daily = forecast.get("daily") or {}
            code = current.get("weather_code")
            kind = weather_kind(code)
            temp = current.get("temperature_2m")
            try:
                temp = round(float(temp), 1) if temp is not None else None
            except (TypeError, ValueError):
                temp = None
            is_day = current.get("is_day")
            if is_day is not None:
                try:
                    is_day = bool(int(is_day))
                except (TypeError, ValueError):
                    is_day = None
            sunrise = _clock((daily.get("sunrise") or [""])[0])
            sunset = _clock((daily.get("sunset") or [""])[0])
            return {
                "ok": True,
                "kind": kind,
                "label": KIND_LABELS.get(kind, KIND_LABELS["unknown"]),
                "code": int(code) if code is not None else None,
                "temperature_c": temp,
                "is_day": is_day,
                "sunrise": sunrise,
                "sunset": sunset,
                "location": {
                    "name": name,
                    "country": country,
                    "admin1": admin,
                    "latitude": lat,
                    "longitude": lon,
                    "timezone": timezone,
                },
                "fetched_at": time.time(),
                "cached": False,
                "stale": False,
                "error": "",
            }
        except httpx.HTTPStatusError as e:
            return _error(city, f"天气服务返回 HTTP {e.response.status_code}")
        except httpx.RequestError as e:
            return _error(city, f"天气服务连接失败（检查网络 / 代理）：{str(e)[:80]}")
        except Exception as e:  # noqa: BLE001 - 天气失败不能反噬工厂
            return _error(city, f"天气数据解析失败：{str(e)[:80]}")

    async def get(self, city: str, *, force: bool = False) -> dict:
        """查询某城市的当前天气；命中缓存时不再出网。

        失败时如果还有上一份可用数据，会返回 ``stale=True`` 的旧数据，
        让场景保持原天气而不是突然切回晴天；没有旧数据才返回 ``ok=False``。
        """
        city = " ".join(str(city or "").split())[:80]
        if not city:
            return _error("", "请先在工厂设置里填写天气城市")
        now = time.time()
        entry = self._cache.get(city)
        if entry is not None and not force:
            age = now - entry[0]
            result = entry[1]
            ttl = STALE_TTL_S if result.get("stale") else (
                CACHE_TTL_S if result.get("ok") else ERROR_TTL_S
            )
            if age < ttl:
                out = dict(result)
                out["cached"] = True
                out["age_s"] = int(age)
                return out

        async with self._lock:
            entry = self._cache.get(city)
            if entry is not None and not force:
                age = now - entry[0]
                result = entry[1]
                ttl = STALE_TTL_S if result.get("stale") else (
                    CACHE_TTL_S if result.get("ok") else ERROR_TTL_S
                )
                if age < ttl:
                    out = dict(result)
                    out["cached"] = True
                    out["age_s"] = int(age)
                    return out
            result = await self._fetch(city)
            if not result["ok"] and entry is not None and entry[1].get("ok"):
                stale = dict(entry[1])
                stale["stale"] = True
                stale["cached"] = True
                stale["age_s"] = int(max(0, now - entry[0]))
                stale["error"] = result.get("error") or "天气更新失败"
                self._cache[city] = (now, stale)
                return stale
            self._cache[city] = (now, result)
            self._trim()
            return result

    def _trim(self) -> None:
        if len(self._cache) <= MAX_CACHE_ENTRIES:
            return
        old = sorted(self._cache.items(), key=lambda item: item[1][0])[:-MAX_CACHE_ENTRIES]
        for city, _ in old:
            self._cache.pop(city, None)
