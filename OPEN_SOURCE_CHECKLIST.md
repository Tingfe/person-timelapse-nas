# 开源发布前检查清单

本项目用于处理家庭监控录像。代码可以公开，但录像及其派生数据绝不能进入公开仓库或 Docker 镜像。

## 已由仓库规则排除

- `data/` 中的原始视频；
- `output/` 中的人物缩略图、事件起止时间、延时成片、任务日志与去重台账；
- `models/` 中下载的模型权重；
- 各类视频、图片、日志、环境变量文件和 macOS 元数据。

## 发布前必须人工确认

1. 执行 `git status --ignored`，确认真实视频只出现在 ignored 区域；
2. 执行 `git check-ignore -v data/* output/* models/yolo11n.pt`，确认排除规则生效；
3. 执行 `git ls-files`，确认清单中没有 `.mp4`、`.jpg`、`events-*.json`、`processed.json`、`tasks.json` 或 `*.log`；
4. 在 Docker Hub 构建前执行 `docker build --no-cache .`，构建上下文不应包含 `data/`、`output/`、`models/`；
5. 不要在 README、Issue、截图或日志中粘贴家庭成员画面、精确录像时间、NAS 名称、局域网 IP、挂载路径、账号或令牌；
6. 仅发布空目录占位文件 `models/.gitkeep` 和 `output/.gitkeep`，不要发布模型权重。

## 许可证提醒

本项目依赖 Ultralytics YOLO。公开发布或商业分发前，请核对其当前许可证要求；若直接使用其 AGPL 组件，项目应采用兼容的开源许可证，或取得相应商业许可。
