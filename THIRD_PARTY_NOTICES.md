# 原始代码来源与授权边界

| 来源 | 版本或提交 | 范围 | 许可证 |
| --- | --- | --- | --- |
| [DeNeRATe-cool/AutoBoya](https://github.com/DeNeRATe-cool/AutoBoya) | `c8fc785bbc07178bfbd79d99b89b5358e021b23b` | `packages/autoboya` 的基础实现 | MIT，保留 [原文](packages/autoboya/LICENSE) |
| [BUAASubnet/UBAA](https://github.com/BUAASubnet/UBAA) | `155e8b56ae76348e67c70414e3a9ab7fa54f2dda` | iClass 协议、登录及 WebVPN 转换的参考实现 | MIT，原文和参考文件见 [NOTICE](packages/autoboya/NOTICE.md) |
| [DeNeRATe-cool/AutoTD](https://github.com/DeNeRATe-cool/AutoTD) / [PyPI 发布包](https://pypi.org/project/autotd-buaa/0.1.15/) | `autotd-buaa 0.1.15` | `packages/autotd` 的基础实现 | 未发现明确许可证；不按本仓库 MIT 转授原始代码权限 |

AutoTD 原始 wheel：`autotd_buaa-0.1.15-py3-none-any.whl`，SHA256：`7eb8471a732403754c578f11a339fbb46e3f674388040d2add25d4187fff08d4`。基础源码来自该分发包，不能把 GitHub 说明页误写为完整源码下载来源。标注来源和仓库公开均不改变上游代码的权利或许可状态；本仓库不据此授予 AutoTD 原始代码的复制、修改或再分发权限，相关授权需向原作者核实。目录内另有 [AutoTD 来源说明](packages/autotd/NOTICE.md)。

本地修改包括：博雅类别筛选、分页和超时处理、动作落盘、会话与连接清理；iClass 本地 Python 接入；TD 遥测禁用、严格响应判断、原子写入、锁和阶段恢复保护、当前学期计次、休眠跨窗保护；独立 TD 进程、统一状态和异常去重检查；安装、配置脚本和离线测试。

本整合项目不冒充学校官方服务，也不表示原作者对本地修改提供维护或保证。原作者保留各自代码的权利。
