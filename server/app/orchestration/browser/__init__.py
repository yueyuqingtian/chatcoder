"""AI 驱动内置浏览器（plan-20261015-browser-agent）。

- bridge：桌面端宿主登记与请求/响应（经 /ws/global）
- host：宿主抽象与选择（桌面端优先，Playwright 兜底）
- kit：注入页面的感知/定位脚本（两种宿主共用）
- driver：动作编排与结果文案（工具层只做参数声明）
"""
