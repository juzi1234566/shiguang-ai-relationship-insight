# Changelog

## v0.2.0 — Windows 可下载版

- [Releases](https://github.com/juzi1234566/shiguang-ai-relationship-insight/releases) 提供可直接运行的关系版 / 情侣版单文件 EXE；源码层的原生密钥恢复实现仍不公开，含真实数据的赠送版也不公开。
- 产品健壮性：微信装在非默认盘符（如 D 盘、`<安装目录>\<版本号>\Weixin.dll`）也能自动识别；查找微信进程改用系统快照，不再依赖 `tasklist`；检测到旧版微信 3.x 会明确提示升级到 4.0+；被安全软件拦截读内存时给出针对性指引，而不是笼统失败。
- README 增加“下载即用（Windows）”与知情同意使用须知；同步伦理边界说明。

## v0.1.0 — Public Portfolio Edition

- 建立全新、无私人历史的公开作品仓库；
- 加入完全合成的关系档案生成器与 JSON 示例；
- 公开本地 UI、关系档案、聊天库、AI 会话、检索、报告和安全层源码；
- 原生数据适配器替换为明确接口占位；
- 加入产品案例、决策记录、迭代复盘、架构、隐私边界和面试导览；
- 加入合成数据实机截图、公开测试和 GitHub Actions CI。

## Private production milestones

- 十万级消息验证；
- 日/月/年摘要金字塔；
- 普通 AI 对话与 Deep Research PDF；
- 多关系档案与每联系人独立存储；
- Windows 单文件通用版/情侣版；
- 真实后台进度、错误保留与恢复；
- 构建身份、独占端口与浏览器生命周期；
- 63 项生产回归测试。
