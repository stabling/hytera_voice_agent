# FlowLingo · 无感英语浏览器扩展（自用版本）

这是一个根据 **Ries.ai 公开功能描述**独立开发的 Chrome/Edge 扩展，不是 Ries 官方版本，也不包含其内部算法、商标素材或源代码。插件源代码都在此目录，可离线阅读和修改。

## 一分钟安装

1. 解压下载的 `flowlingo-extension.zip`，确保目录内能看到 `manifest.json`。
2. Chrome 地址栏进入 `chrome://extensions`（Edge 为 `edge://extensions`）。
3. 打开“开发者模式”。
4. 点击“加载已解压的扩展程序”，**选择含 `manifest.json` 的 flowlingo 目录**。
5. 刷新一个普通 HTTP/HTTPS 网页；浏览中文文章或英文资料时看到点缀的英文标注。点击单词进行互动。
6. 点击工具栏 FlowLingo 图标，设置英语接触强度与当前难度。

> Chrome 内置页面、扩展商店、某些 iframe 和无法读取的 Canvas 字幕无法注入。本插件无法修改非浏览器桌面应用。视频网站必须已启用可见的原生字幕。

## 已实现

- 中文网站：逐渐将中文词、短语改为英文（默认本地词典 277 条，AI 模式可识别更多短语/句式）
- 英文网站：高价值生词注解，中文释义
- 难度层级 A1～C1 的近似分层、接触强度、每段改造数量上限
- 动态网页 / SPA 内容监听、可见区域懒处理，暂停恢复和原文切换
- 点击词语 / 划词语境解释、浏览器 TTS 发音、收藏
- 复习中心、熟悉状态、简单 SM-2 式间隔复习与记录统计
- YouTube / B站 / Netflix 页面已显示的原生字幕的增强叠层（尽力适配，依赖播放器 DOM）
- Chrome MV3 扩展、离线工作，无需用户账号
- 可选 OpenAI-Compatible API：智能语境替换、选词讲解、字幕中英翻译
- 个人数据 JSON 导入、导出（不包含密钥）
- 按网站禁用、敏感网站默认跳过、扩展快捷键

## AI 高级功能（可选）

1. 点“学习中心” → “偏好设置” → “AI 语境引擎”。
2. 输入兼容 `POST /v1/chat/completions` 的完整 HTTPS 接口 URL、模型名和自己的 API Key。
3. 勾选开启 AI，点击“保存全部设置”。
4. 点击“授权 AI 接口域名”，Chrome 会请求该域名的网络权限。仅请求所填 API 域名。
5. 点击“测试 AI 连通性”验证，刷新网页。

也允许 `http://localhost:端口/v1/chat/completions` 的本地模型端点。服务必须兼容 Chat Completions JSON；部分模型不支持 `response_format: {"type":"json_object"}`，本版本遇到明确指向该选项的 HTTP 400 时会自动重试一次。**AI 会发送精选网页段落给你的接口服务。按需使用，不要在处理私人信息的网站开启。**

AI 用量限制：每页最多约 5 次批量段落请求、12 次视频字幕请求，并设置每日请求数上限。字幕频繁变化时，达到限额便自动使用本地模式。

## 插件如何工作

`manifest.json` → `scripts/content.js` 在普通网页读取可见的 DOM 文本 → `scripts/core.js` 和 `dictionary.js` 根据等级、密度挑选替换 → 局部包裹文本片段成标记 → 用户点击后展示隔离在 Shadow DOM 的学习卡片 → `background.js` 管理本地状态与可选 AI 网络请求。

默认没有账号、遥测、服务端，也没有外部 JS、CDN 或跟踪脚本。

## 已知局限（与 Ries 不可能完全一致的部分）

- Ries 的内部专有词汇图谱、AI 提示与学习模型并未公开；此项目使用独立词典与启发式难度排序。
- 离线词典属于**静态匹配**，部分多义词只能显示近似释义；推荐启用 AI 处理上下文复杂的句子。
- 离线模式不能给任意英文字幕提供完整中文翻译：只有关键词注释；**完整双语字幕需要 AI**。
- 视频字幕必须已作为普通 DOM 文本呈现；无法自动转录无字幕视频，也无法读取受保护或 Canvas 渲染的字幕。
- Netflix / YouTube / B站改变播放器 DOM 时可能导致字幕失效，需要调整 `collectCaptions()` 选择器。
- 大型虚拟滚动的框架页面、强约束的输入组件可能不允许改写部分文本。涉及邮箱、账户页默认禁用。
- 使用宽泛 `content_scripts.matches` 以覆盖普通网页，但所有 AI 服务跨域权限是可选、单独授权。
- 尚无云端账户同步、移动系统全局文本改写或真实 CEFR 测试。

## 文件结构

- `manifest.json` 扩展描述、权限、快捷键
- `scripts/dictionary.js` 本地中英文词组
- `scripts/core.js` 纯函数匹配、难度和记忆算法
- `scripts/content.js` DOM 替换、交互、视频字幕
- `scripts/content.css` 小范围页面内标记
- `scripts/background.js` 数据和可选 AI
- `ui/popup.*` 工具栏弹窗
- `ui/dashboard.*` 学习中心
- `assets` 原创图标
- `tests` 测试材料

## 快捷键

- `Alt+Shift+L`：暂停或恢复**当前网页**
- `Alt+Shift+E`：解释当前选中的文字

快捷键被其他插件或网页占用时可在 `chrome://extensions/shortcuts` 中修改。

## 数据安全

- `chrome.storage.local`：设置、学习数据、可选的密钥，存储于此浏览器档案。不做云同步。
- `chrome.storage.local` **不是加密的秘密保险箱**。尽管内容脚本直接访问被限制，请不要在多人共用设备中保存敏感 API Key。
- 默认不会向任何云端发送访问网页内容。
- 启用 AI 后，会向你自己指定的域名发送**有限量**的正文、字幕或已选文本。你应遵守对应服务的隐私条款。
- 不收集密码字段、编辑器文本，也不绕过跨域 iframe / 网站保护机制。

## 测试

在 `tests/` 下附带静态夹具和核心算法检查脚本。用 Node 运行：

```bash
node tests/test_core.js
```

可运行 `python tests/test_browser.py` 测试真实 Chromium 的 DOM、弹窗与学习中心交互（使用模拟的扩展运行时）。由于制作环境的浏览器策略限制，**尚未在真实安装的扩展中完成端到端测试**；首次自用请加载解压目录后测试。

## 版权

本项目是自主实现的自用工具。非商业使用时仍应尊重 Ries.ai 及目标网站的合法权益。
