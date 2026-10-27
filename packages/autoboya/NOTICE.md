# 第三方来源与许可证

普通课程签到模块 `autoboya/iclass.py` 及 WebVPN 反向 URL 转换参考：

- 项目：[BUAASubnet/UBAA](https://github.com/BUAASubnet/UBAA)。
- 参考提交：`155e8b56ae76348e67c70414e3a9ab7fa54f2dda`。
- 参考文件：`server/src/main/kotlin/cn/edu/ubaa/signin/SigninClient.kt`、`shared/src/commonMain/kotlin/cn/edu/ubaa/api/SigninLoginNameSupport.kt`、`server/src/main/kotlin/cn/edu/ubaa/utils/VpnCipher.kt`。
- 参考范围：SSO MyCenter 跳转、loginName 编码保留、iClass 登录、今日排课、服务器时间与签到提交协议，以及 WebVPN 端口／路径映射。
- 本地使用 Python 实现，直接访问学校服务，不依赖 UBAA 托管服务器。

以下保留 UBAA 的 MIT 许可全文：

```text
MIT License

Copyright (c) 2026 BUAASubnet

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
