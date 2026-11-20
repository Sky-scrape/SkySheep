# 安装时遇到「Windows 已保护你的电脑」怎么办？

SkySheep 的安装包（setup.exe）目前**没有购买代码签名证书**。Windows SmartScreen
看到"不认识的发行者 + 从网络下载的文件"，就会弹出蓝色拦截页——这是未签名程序的
默认待遇，**不代表检测到了病毒**。本页解释怎么放行，以及签名方案的现状。

> 发版侧注：发版脚本 `engine/tools/release.py` 已内置 signtool 签名阶段——配置
> `SKYSHEEP_SIGN_PFX`（或 `SKYSHEEP_SIGN_CERT_SHA1`）环境变量后，打包产物与安装包
> 会自动签名并回验；签名发布后拦截会逐步减少（信誉积累期见分发指南 §3.1）。
> 未配置证书时自动跳过、维持现状。见 `docs/发布清单.md` §3 与 `docs/版本支持与更新渠道.md`。

## 拦截页长什么样

安装时弹出全屏蓝色警告：

> Windows 已保护你的电脑
> Microsoft Defender SmartScreen 阻止了无法识别的应用启动。运行此应用可能会危及你的电脑。

## 放行步骤（Win10 / Win11 相同）

1. 拦截页上点 **「更多信息」**（More info）——四个小字，在警告文字下方，很容易漏看。
2. 页面底部会多出一行"应用: SkySheep-…-setup.exe · 发布者: 未知发布者"，右侧出现
   **「仍要运行」** 按钮。
3. 点「仍要运行」→ 弹出正常的安装向导 → 正常安装。

只需要第一次放行；安装完成后运行 SkySheep.exe 不会再拦。

## 浏览器下载时被拦？

Chrome / Edge 偶尔会在下载阶段就标记 setup.exe（左下角文件上出现 ⚠）：
鼠标悬停 → 点右侧「⋯」→「保留」→「仍然保留」。之后的安装步骤同上。

## 为什么不直接买个签名证书？

签名方案有三条路：Azure Artifact Signing（原 Trusted Signing，按月付费）、自购 EV 证书、
SignPath（开源项目可申请）。费用、适用主体与开通步骤见 [分发上架指南.md](分发上架指南.md) §3.1，
本页不重复；现状是还没买，证书采购是用户动作，到位前安装包保持未签名、拦截照旧。
未签名期靠这些事实建立信任：

- 安装包由 GitHub Actions 或本地 PyInstaller 构建，构建脚本全部开源可审计
  （`SkySheep.spec` + `tools/installer.iss`）；
- 安装后所有数据只落在 `~/.skysheep/` 与工作目录，不发任何遥测；
- 协议流量只有本机 127.0.0.1（局域网访问默认关闭）。

另外，签名不是「买了就立刻放行」——发行者信誉要积累一段时间（见分发指南 §3.1），
所以本页的放行步骤在签名落地初期仍然用得上。

## 校验安装包（进阶）

发布 Release 时会附带 SHA-256 校验值：Release 正文末尾有一份可读哈希，
同目录还有同名校验附件 `SkySheep-<版本>-setup.exe.sha256`（应用内更新也按它自动核对安装包）。
PowerShell 里执行：

```powershell
Get-FileHash .\SkySheep-<版本>-setup.exe -Algorithm SHA256
```

对比值一致即可确认文件在传输中未被篡改。
