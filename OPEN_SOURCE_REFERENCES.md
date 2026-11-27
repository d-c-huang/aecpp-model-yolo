# 与 AECPP 素材动画相关的开源参考

用户指定发现来源：[GitHubDaily/GitHubDaily](https://github.com/GitHubDaily/GitHubDaily)。2026-11-27 查看其 README 和 2024 年分类清单，并到下列项目自身仓库核对描述与许可证元数据。GitHubDaily 是项目索引，接入时以原项目源码、许可证和实际验证为准。

当前主流程：小说 / 剧本 → 人物与分镜 → 匹配真实角色、已保存动作、表情和背景 → 原生可编辑 AECPP 工程。参考项目必须服务于这条流程；生成图片和拼接生成视频不属于本应用的素材动画实现。

| 当前需求 | 参考项目 | 适合借鉴的方向 | 当前决定 |
| --- | --- | --- | --- |
| 匿名图片缺少可检索信息 | [ai-renamer](https://github.com/ozgrozer/ai-renamer) | 本地模型理解文件内容；可考虑转化为独立的素材描述索引，以文件摘要绑定描述，不改原素材名或引用 | 候选，尚未集成。该项目是 Node.js 工具，仓库标记 GPL-3.0；已有 Python / Ollama 视觉服务可用于后续独立实现索引 |
| 字幕和多角色配音 | [pyVideoTrans](https://github.com/jianchang512/pyvideotrans) | 参考转写、配音、字幕处理的分阶段任务组织；未来输出 AECPP 可编辑音轨 | 候选，尚未集成。Python 项目，GPL-3.0；需要验证声音模型、时长对齐及实际依赖，当前一键工程仍由 AECPP 继续配音 |
| 视频站公开页面发现与进度展示 | [Crawlee for Python](https://github.com/apify/crawlee-python) | HTTP 与浏览器页面抓取分工、请求队列和结构化结果；可作为现有站点发现器的可选实现 | 候选，尚未集成。Apache-2.0；浏览器模式额外依赖 Playwright，与已有任务取消、站点限制和来源记录需要适配 |

本次自动匹配由项目自身 `aecpp_auto_match.py` 实现，不引入上述项目的新运行时依赖，也未复制其源码。现有名称、显式标签、同义动作和替代规则已经覆盖不需要视觉索引的素材；匿名图片仍明确作为未确认匹配处理。

后续确需引入代码时，记录具体版本 / 提交、复制或使用的文件、许可证和署名；分别验证 Windows 安装、离线运行、取消与重试、资源引用稳定性以及 AECPP 原生打开 / 保存。先用最小样例确认收益，再扩大接入范围。

原始来源：[GitHubDaily 当前清单](https://github.com/GitHubDaily/GitHubDaily/blob/master/README.md)、[2024 年清单](https://github.com/GitHubDaily/GitHubDaily/blob/master/2024.md)、[ai-renamer 仓库元数据](https://api.github.com/repos/ozgrozer/ai-renamer)、[pyVideoTrans README](https://github.com/jianchang512/pyvideotrans/blob/main/README.md)、[Crawlee README](https://github.com/apify/crawlee-python/blob/master/README.md)。
