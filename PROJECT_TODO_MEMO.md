# EC_project 项目进度备忘录

更新时间：2026-07-13

本文档用于记录当前已完成事项、未完成待办项和生产部署前准备事项，便于后续继续推进。

## 已完成

1. 项目三端结构梳理完成：`Client / Server_manager / Trader_Server`。
2. `Server_economic / SE / TS` 旧命名冲突已大体收束到 `Trader_Server`。
3. `se_address / ts_address` 字段冲突已修复，Client、SM、数据库链路已能正常使用。
4. SM 账户创建/更新参数冲突已修复。
5. Client 登录迁移完成：SM 登录在 Client 端，交易服务登录在 Client 主界面完成。
6. 交易服务登录结构已迁移：券商账户名/密码由 Client 输入，SM 不再管理券商账户密码。
7. TS 交易服务登录 gate 已实现，并保留开发测试后门 `test/test`。
8. Client 与 TS 的 WebSocket 通信链路已梳理并优化。
9. Client 与 TS 断线重连链路已修复：重连失败后释放占用并回登录界面。
10. Client 已增加 TS 延迟显示功能。
11. Client 重复订单拦截已调整为 `0.5s`。
12. Client 日志显示已精简，英文提示已统一为中文风格。
13. Client UI 已迁移到 PySide6，新主界面和登录界面已接入。
14. TS UI 已迁移到 PySide6，旧 GUI 入口和草稿文件已收束。
15. SM Web UI 已完成新版优化：总览、账户状态、节点状态、最近事件、日志分页、夜间模式。
16. SM 日志分页已完成，分页控件已居中。
17. 三端通信链路文档已整理：`Client_通信梳理表.md`、`SM_通信梳理表.md`、`TS_通信梳理表.md`。
18. 数据库结构已整合，SM SQLite schema/migration 基础已完成。
19. SM 审计日志和日志清理机制已增加。
20. tastytrade 官方 API 文档已整理为项目文档。
21. TS API 有效性清单已整理，已区分通过项和需复测项。
22. TS WebSocket 鉴权已接入 SM token 校验。
23. SM 获取 TS 状态的及时性问题已做过优化。
24. 多余/废弃接口和旧文件已做过多轮清理。
25. Client、TS、SM 的主要静态检查和局部自测已多轮执行。

## 待办

1. 自动化回归测试补齐：新增最小冒烟测试，覆盖 SM、TS、Client 服务层。
2. tastytrade 真实凭证联调：需要真实 `token / secret / 交易账号` 后复测登录、账户、持仓、订单、撤单、行情。
3. IB 数据源真实接入：当前只有结构入口，还未做真实 IB 网关/SDK 联调。
4. 生产环境部署改造：
   （1）Client 支持完整 `https://sm.xxx.com` 地址。
   （2）Client 支持完整 `ws://` 或 `wss://` TS 地址。
   （3）Client TS 地址解析从 `host:port` 升级为完整 URL 解析。
   （4）Client 不在本地固定 TS，每次登录后从 SM 获取该账号当前绑定的 TS。
   （5）SM 账号管理支持绑定和修改指定 TS。
   （6）SM 返回账号当前绑定的 TS 地址。
   （7）SM 修改账号绑定时处理在线占用：禁止修改、下次生效或强制下线，建议采用强制下线后重新登录。
   （8）TS 节点记录保存当前可访问地址，例如 `public_ws_url`。
   （9）TS IP 变化时，只更新 TS 节点地址或 DNS，不改 Client。
   （10）生产环境关闭 TS `test/test` 后门。
   （11）SM 默认管理员密码生产检查，禁止默认 `admin / changeme123` 上线。
   （12）增加 Windows 无 GUI 服务启动入口。
   （13）增加 Windows 部署脚本：Caddy、NSSM/WinSW、环境变量、启动命令模板。
   （14）增加 `.env.production.example`。
   （15）增加 Caddyfile 模板。
   （16）增加 SQLite 自动备份脚本和恢复说明。
   （17）增加生产启动前检查：HTTPS/WSS、测试后门、默认密码、目录权限、数据库可写。
   （18）日志脱敏再检查。
   （19）生产环境重新打包 Client、SM、TS。
5. 数据库迁移演练：复制数据库到另一台设备后验证可运行。
6. 三端异常场景手工回归：TS 断开、TS 重启、SM 断开、Client 重连失败、占用释放。
7. 最终真实券商 API 风控参数确认：登录重试、行情订阅、订单频率、封禁风险。

## 生产部署前准备

1. 准备一个主域名。
2. 规划子域名，例如：`sm.yourdomain.com`、`ts01.yourdomain.com`、`ts02.yourdomain.com`。
3. 准备 Windows 云服务器公网 IP。
4. 配置 DNS A 记录。
5. 准备 Caddy / NSSM 或 WinSW 部署工具。
6. 准备 SM 管理员生产账号密码。
7. 准备 TS 节点名称和券商类型。
8. 准备数据库备份目录。
9. 后续准备真实 tastytrade 凭证。