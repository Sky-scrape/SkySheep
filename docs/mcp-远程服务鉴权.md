# MCP 远程服务鉴权说明

远程 MCP 服务（通过 `url` 接入的 Streamable HTTP 服务）大多需要鉴权。
SkySheep 目前支持**静态请求头**方式：在服务的 `headers` 里带上服务方要求的凭证。

## 配置方法

在「设置 · MCP · 手动添加」的 **headers** 里每行填一条，例如：

```
Authorization: Bearer eyJhbGciOi...（服务方发给你的 token）
X-Api-Key: sk-xxxx
```

也可以直接编辑 `~/.skysheep/mcp.json`（项目级在 `<项目>/.skysheep/mcp.json`）：

```json
{ "mcpServers": {
    "notion": {
        "url": "https://mcp.notion.com/mcp",
        "headers": { "Authorization": "Bearer xxxxx" }
    }
} }
```

改完保存即生效，不用重启（设置页里的改动会自动重连）。

## Token 过期了怎么办

托管服务（Notion / Linear / GitHub 官方 Remote MCP 等）签发的 token 通常**有时效**，
过期后连接会报「鉴权失败（401/403）」。处理方式：

1. 回到服务方页面重新生成一个 token；
2. 在设置页删掉该服务重新添加，或直接改 `mcp.json` 里的 `headers`；
3. 保存后即自动重连。

## 为什么没有「登录授权」按钮

MCP 协议的完整 OAuth 2.1 授权流程（动态客户端注册 + 浏览器回调）需要为每个
服务维护回调端口与令牌刷新逻辑。SkySheep 当前版本选择显式粘贴 token 的方式：
更繁琐，但凭证完全存在本机 `mcp.json`、行为可预期，也不会在后台静默刷新令牌。
对个人桌面应用而言这是更透明、更可控的折中。
