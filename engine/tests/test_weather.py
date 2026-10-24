"""现实天气服务：WMO 映射、缓存与失败降级（不出真实网络）。"""

from __future__ import annotations

import httpx

from skysheep.core.weather import WeatherService, weather_kind


def _ok_handler(calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "geocoding" in str(request.url):
            return httpx.Response(200, json={"results": [{
                "name": "上海", "country": "中国", "admin1": "上海市",
                "latitude": 31.22, "longitude": 121.46,
                "timezone": "Asia/Shanghai",
            }]})
        return httpx.Response(200, json={
            "current": {
                "time": "2026-10-23T17:30",
                "temperature_2m": 19.56,
                "weather_code": 81,
                "is_day": 0,
            },
            "daily": {
                "sunrise": ["2026-10-23T06:03"],
                "sunset": ["2026-10-23T17:14"],
            },
        })
    return handler


async def test_weather_parses_city_forecast_and_caches():
    calls: list[str] = []
    svc = WeatherService(transport=httpx.MockTransport(_ok_handler(calls)))
    out = await svc.get("上海")
    assert out["ok"] is True
    assert out["kind"] == "rain" and out["label"] == "雨"
    assert out["temperature_c"] == 19.6
    assert out["is_day"] is False
    assert out["sunrise"] == "06:03" and out["sunset"] == "17:14"
    assert out["location"]["name"] == "上海"
    assert out["cached"] is False

    # 30 分钟缓存：第二次不再出网
    again = await svc.get("上海")
    assert again["cached"] is True and again["age_s"] >= 0
    assert len(calls) == 2, "一次地理编码 + 一次天气查询"


async def test_weather_failure_falls_back_to_stale_good_snapshot():
    calls: list[str] = []
    state = {"fail": False}
    base = _ok_handler(calls)

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(503)
        return base(request)

    svc = WeatherService(transport=httpx.MockTransport(handler))
    first = await svc.get("上海")
    assert first["ok"] is True
    state["fail"] = True
    stale = await svc.get("上海", force=True)
    assert stale["ok"] is True
    assert stale["stale"] is True
    assert stale["kind"] == "rain", "失败时沿用上一份天气，不要突然放晴"
    assert "HTTP 503" in stale["error"]


async def test_weather_empty_city_and_unknown_code():
    svc = WeatherService(transport=httpx.MockTransport(
        lambda _request: httpx.Response(500)))
    out = await svc.get("   ")
    assert out["ok"] is False and "城市" in out["error"]

    assert weather_kind(0) == "clear"
    assert weather_kind(3) == "cloudy"
    assert weather_kind(45) == "fog"
    assert weather_kind(65) == "rain"
    assert weather_kind(75) == "snow"
    assert weather_kind(99) == "thunder"
    assert weather_kind("bad") == "unknown"
