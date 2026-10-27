# BUAA Automation

将博雅选课、博雅签到签退、iClass 普通课程签到和 TD 调度放在同一套本地后台中，提供类别筛选、状态查看、失败记录与检查入口。

这是对原项目的整合与可靠性修复，来源见 [第三方说明](THIRD_PARTY_NOTICES.md)。**AutoTD 原始分发包没有查到明确许可证，完整副本目前作为私有候选整理，未宣称整个仓库可按 MIT 再分发。**

## 能力与启用条件

- 博雅：只自动选择勾选类别，继续检查自主签到、选课时间、容量、校区及已选记录。未配置或全不选会暂停新选课；已选课程的签到签退继续处理。
- iClass：默认关闭。启用后只处理当天上课窗口内、明确未签到的课程，不补签历史课堂。
- TD：默认未配置。必须提供当前学期标识、启用日期、可信累计次数、有效机位和图片；每日最多三轮，累计达到目标后停止新入口。新的入口避开已知课程冲突，已入口后的出口优先完成。
- TD 仅在北京时间 07:30–10:00、11:30–14:00、15:30–20:00 内运行，并保留完成一轮所需余量。休眠跨窗或出口逾期、提交结果未知时暂停，重启不清除错误重试。
- TD 遥测收集和网络传输已禁用；“刷新次数”因会发送入口打卡而被禁用。次数来自人工确认或真实任务响应。

## 安装

需要 Python 3.11 或 3.12。Windows 提供开机启动安装脚本；类别窗口需要 Python 自带的 Tkinter。先取得本仓库并进入根目录，然后执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".\packages\autoboya[dev]" -e .\packages\autotd
.\.venv\Scripts\python.exe -m autoboya --help
```

Linux/macOS 将虚拟环境命令替换为 `.venv/bin/python`。请使用本地修复包，直接安装 PyPI 原始版本不会包含本仓库修复。

## 配置博雅及普通课程

```powershell
.\.venv\Scripts\python.exe -m autoboya init
$account = Read-Host 'Account'
.\.venv\Scripts\python.exe -m autoboya user add $account
.\.venv\Scripts\python.exe -m autoboya login $account
.\.venv\Scripts\python.exe -m autoboya courses refresh
.\.venv\Scripts\python.exe -m autoboya courses filters
.\.venv\Scripts\python.exe -m autoboya courses auto-preview
```

密码在交互提示中输入，默认保存到系统钥匙串；遇到验证码需要人工完成登录。类别窗口保存后，下次扫描和待执行选课都会读取新限制。`user add --campus` 支持“北京”和“杭州”。

需要普通课程签到时再启用：

```powershell
.\.venv\Scripts\python.exe -m autoboya class-signin enable
.\.venv\Scripts\python.exe -m autoboya class-signin today
```

## 配置 TD

账号必须先加入上述博雅启用账号。准备仓库外的入口和出口图片，确认健康云中的当前学期累计次数，然后运行：

```powershell
.\.venv\Scripts\python.exe .\scripts\configure_td.py --help
```

脚本要求明确提供账号、服务器、学期标识、启用日期、确认次数和两张图片，成功后启用 TD。默认目标 32 次、每日三轮，默认不设置结束日期；如学校有不同要求，配置时明确调整。示例：

```powershell
$server = Read-Host 'TD server host'
$term = Read-Host 'Current term identifier'
$start = Read-Host 'Start date in ISO format'
$count = Read-Host 'Confirmed term count'
$entryPhoto = Read-Host 'Entrance image path outside this repository'
$exitPhoto = Read-Host 'Exit image path outside this repository'
.\.venv\Scripts\python.exe .\scripts\configure_td.py --user $account --server $server --term $term --start-date $start --confirmed-count $count --entrance-image $entryPhoto --exit-image $exitPhoto
```

默认本部入口 2、出口 6；沙河可使用 `--entrance 8 --exit 11`。脚本拒绝覆盖已配置用户或进度。已有任务报错时，先核对实际记录，不能用清空状态或重启来代替核验。旧独立 EXE 的操作记录无法自动合并，不能与新后台同时使用。

## 运行、查看与停止

```powershell
.\.venv\Scripts\python.exe -m autoboya run
.\.venv\Scripts\python.exe -m autoboya status --interactive
.\.venv\Scripts\python.exe -m autoboya td-status
.\.venv\Scripts\python.exe -m autoboya td-alerts
.\.venv\Scripts\python.exe -m autoboya td-disable
.\.venv\Scripts\python.exe -m autoboya stop
```

`run` 使用操作系统互斥锁，TD 使用独立进程和动作锁，不占用博雅请求或等待。`td-enable` 恢复已配置 TD；重新启用不会清除错误。

Windows 开机启动及桌面状态入口：

```powershell
powershell -NoProfile -File .\scripts\install-autostart.ps1 -WhatIf
powershell -NoProfile -File .\scripts\install-autostart.ps1
```

安装脚本仅注册当前用户登录后的任务，不代替账号或 TD 配置。默认不立即触发；需要立即启动时使用 `-StartNow`。已有同名任务会通过 PowerShell 的确认机制提示，先检查预览。关闭状态窗口不影响后台。

## 故障提醒与运行边界

`td-alerts` 是检查及去重入口，会输出新的异常 JSON；它自身不弹窗或发消息。要收到提醒，需要另外配置 [定时检查](docs/MONITORING.md)。仓库不包含任何个人 Codex 任务 ID、账号连接或定时任务文件。

电脑关机、休眠和断网期间无法保证按时执行；验证码仍需要人工处理。请求已受理但响应丢失时无法保证严格仅执行一次，本版会记录阶段并暂停，而非盲目重试。协议成功回执不能替代学校最终的有效次数认定。

## 测试与来源

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试使用临时目录、模拟客户端和合成账号，不会执行真实打卡。验证范围和未完成的真实窗口验收见 [验证说明](docs/VALIDATION.md)。

## 许可证

本仓库新增的整合脚本和测试采用 [MIT](LICENSE)。AutoBoya 保留原 MIT 声明；UBAA 参考部分保留 MIT 声明；AutoTD 原始源码许可未确认，不在本仓库 MIT 转授权范围内。完整来源、版本、校验和与修改范围见 [第三方说明](THIRD_PARTY_NOTICES.md)。
