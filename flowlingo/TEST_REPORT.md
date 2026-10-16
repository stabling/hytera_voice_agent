# FlowLingo 验证报告

## 已验证
- Node 语法：所有生产 JS 文件通过 `node --check`。
- Node 单测：`tests/test_core.js` 与 `tests/test_background.js` 通过。
- Chromium DOM 模拟测试：`tests/test_browser.py` 通过。
- 中文网页替换、英文词语注解、原文恢复、动态增加段落、避免处理输入框、保留页面 click 监听。
- Shadow DOM 提示卡片与生词收藏、学习中心渲染、间隔复习、偏好设置。
- 模拟 YouTube 页面字幕节点：自动字幕读取、AI 输出叠层，以及字幕显示模式切换。
- 存储安全：内容脚本只能通过后台消息修改少量展示设置，无法直接改变 AI 服务地址或读取密钥。
- AJAX/AI 背景服务由 Node mock 接口验证，包含密钥缺失、消息流程和 JSON 响应。

## 未验证或有限制
- **真实的 Chrome 扩展安装与外部网页自动注入未完成端到端测试**：当前运行环境的 Chromium 被管理员策略阻止访问测试网址并加载扩展 Service Worker。
- 视频站点 DOM 选择器可能随网站版本变化；不能保证 Netflix、B站或 YouTube 的所有播放器版本。
- 未使用真实收费 API Key 进行网络请求，不保证所有 OpenAI-compatible 提供商响应完全一致。
- 音频发音依赖浏览器 speechSynthesis 与本地可用语音。
- Ries 私有语言图谱、模型与完整视觉交互未公开，因此无法逐项核对等同性。

## 使用建议
首次加载后先访问普通中文博客和英文文章确认标注是否出现；再根据自己常用的视频网站测试已打开的原生字幕。AI 开启前请设置接口权限及调用上限。
