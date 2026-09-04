# SM / TS Windows 打包说明

## 1. 打包入口

在仓库根目录执行：

```bat
build_servers.bat
```

也可以单独执行 `build_sm.bat` 或 `build_ts.bat`。组合入口会让 SM/TS 共用同一个北京时间版本戳。默认流程会检查 Python 3.11 x64、安装锁定版本的依赖、运行专项测试、构建 PyInstaller `onedir`、执行冻结自检和本机 HTTP 探活，再生成 ZIP、Inno Setup 安装器、逐文件清单和 SHA-256 校验文件。

正式发布默认要求 Git 工作树干净。`ALLOW_DIRTY_BUILD=1` 只用于本地验证；此时 `BUILD_INFO.json` 和 `MANIFEST.json` 会记录 `git_dirty=true`，不得作为正式发布包。

## 2. 环境要求

- Windows x64。
- Python 3.11 x64，可通过 `py -3.11` 启动。
- Git 可用，仓库目录可读取提交信息。
- 首次构建可访问 Python 依赖源和 IB API 下载地址。
- Caddy 二进制必须与 `deploy\caddy\CADDY_VERSION.txt` 中的 SHA-256 一致。
- Inno Setup 6。脚本会自动查找 `.tools\Inno Setup 6`、系统安装目录或 `SERVER_ISCC_EXE`。
- 正式签名需要 Windows SDK `signtool.exe` 和位于 Windows 证书存储区、带私钥的代码签名证书。

脚本默认分别复用 `%LOCALAPPDATA%\SCServerBuild\sm-venv` 和 `%LOCALAPPDATA%\SCServerBuild\ts-venv`。可通过 `SERVER_BUILD_PY` 指定统一的 Python，也可分别使用 `SM_BUILD_PY`、`TS_BUILD_PY`。

首次准备打包工具可执行：

```bat
bootstrap_packaging_tools.bat
```

该脚本按照 `packaging\windows_tools.lock.json` 下载固定版本的 Inno Setup 和 Microsoft Windows SDK Build Tools，校验官方包哈希、工具哈希及 Authenticode 发布者后安装到项目本地 `.tools`。构建脚本会自动发现这些工具。

## 3. 常用开关

| 变量 | 用途 | 发布要求 |
| --- | --- | --- |
| `ALLOW_DIRTY_BUILD=1` | 允许未提交工作树构建 | 仅验证 |
| `ALLOW_ARCHIVE_ONLY=1` | Inno 不可用时只生成 ZIP | 仅验证 |
| `SKIP_PIP=1` | 跳过依赖安装 | 仅限已验证环境 |
| `SKIP_TESTS=1` | 跳过专项测试 | 禁止 |
| `SKIP_SMOKE_TESTS=1` | 跳过冻结服务探活 | 禁止 |
| `RUN_FULL_TESTS=1` | 在专项测试外运行完整测试集 | 建议发布时启用 |
| `SERVER_BUILD_TIMESTAMP=yyyyMMddHHmmss` | 固定版本时间戳 | 自动化流水线可用 |
| `NO_PAUSE=1` | 批处理结束时不暂停 | 自动化流水线可用 |
| `SERVER_ISCC_EXE` | 指定 `ISCC.exe` | 可选 |
| `INNO_COMMERCIAL_LICENSE_CONFIRMED=1` | 确认当前 Inno 编译器许可覆盖本次商业发布 | 正式发布必需 |
| `SERVER_SIGNTOOL_EXE` | 指定 Windows SDK `signtool.exe` | 可选 |
| `SERVER_SIGN_CERT_THUMBPRINT` | 指定证书存储区中的签名证书指纹 | 签名必需 |
| `SERVER_TIMESTAMP_URL` | RFC3161 时间戳地址 | 默认 DigiCert |
| `REQUIRE_CODE_SIGNING=1` | 无有效签名配置时立即失败 | 正式单包构建必需 |
| `SERVER_RELEASE_BUILD=1` | 强制干净工作树、安装器、测试、探活和签名 | 正式组合发布必需 |

正式发布示例：

```bat
set "SERVER_RELEASE_BUILD=1"
set "INNO_COMMERCIAL_LICENSE_CONFIRMED=1"
set "SERVER_SIGN_CERT_THUMBPRINT=<证书指纹>"
build_servers.bat
```

签名证书必须位于 `CurrentUser\My` 或 `LocalMachine\My`，具有代码签名用途和可访问私钥。私钥或 PFX 密码不得写入仓库、批处理或环境示例。

项目引导脚本安装的 Inno Setup 6.7.3 编译器会显示 `Non-commercial use only`，只用于当前技术验证。商业发布前必须购买并配置覆盖该用途的 Inno 许可，或将 `SERVER_ISCC_EXE` 指向已获得相应许可的编译器；确认后才可设置 `INNO_COMMERCIAL_LICENSE_CONFIRMED=1`。

## 4. 产物结构

```text
dist/
  ServerManagerPackage/
    app/ServerManager/
    archive/SC_SM_Windows_x64_<version>.zip
    installer/SC_SM_Setup_<version>.exe
    MANIFEST.json
    SHA256SUMS.txt
  TraderServerPackage/
    app/TraderServer/
    archive/SC_TS_Windows_x64_<version>.zip
    installer/SC_TS_Setup_<version>.exe
    MANIFEST.json
    SHA256SUMS.txt
```

`MANIFEST.json` 记录构建提交、脏工作树、安装器和签名状态、Caddy 版本、应用目录逐文件哈希以及 ZIP/安装器哈希。`SHA256SUMS.txt` 使用 `archive/...` 和 `installer/...` 相对路径，可从对应包目录直接校验。

PowerShell 校验示例：

```powershell
Get-Content .\SHA256SUMS.txt | ForEach-Object {
    if ($_ -notmatch '^([A-Fa-f0-9]{64})  (.+)$') { throw "Invalid checksum line: $_" }
    if ((Get-FileHash -Algorithm SHA256 $Matches[2]).Hash -ne $Matches[1]) {
        throw "Checksum mismatch: $($Matches[2])"
    }
}
```

## 5. ZIP 与安装版

SM 的正式 Windows 安装入口是 `SC_SM_Setup_<version>.exe`。运行安装器后选择全新部署或升级部署；升级时选择需要迁移的 `data` 目录。安装器会在替换程序前创建事务备份，迁移失败或 SM 本机 `/ping` 探活失败时尝试回滚。

SM 安装版将程序放在用户选择的安装目录，运行数据固定在：

- `%ProgramData%\SC\ServerManager\data`
- `%ProgramData%\SC\ServerManager\caddy`

安装器只修改 SM 自身，不修改 Client 或 TS。固定生产入口为 `https://scjrdomain.com:4430`，SM 本地应用端口为 `18800`，公网 HTTP/HTTPS 端口为 `8800/4430`。外部路由和第三方防火墙仍需人工确认。

升级迁移使用 SQLite Backup API 和 staging 目录，旧 data 源目录只读处理，不执行其中的脚本。数据库、账号、TS 节点、审批状态、软件包、DNS 配置和证书按迁移规则处理；运行日志、PID、会话和 Caddy 缓存不作为业务数据迁移。

安装器会保留 `%ProgramData%\SC\ServerManager\sm.local.bat`，并将运行目录 ACL 收紧为 `SYSTEM` 和本机管理员组。卸载默认保留 data、证书和事务备份。

## 6. 安全边界

打包脚本会拒绝数据库、日志、注册状态、运行期配置、`.env`、本地覆盖脚本和 Caddy 运行数据进入 ZIP 或安装器的程序目录。冻结程序默认按生产环境校验 HTTPS、回环监听和 Caddy 要求；SM 新数据库还要求非默认管理员密码。

普通验证构建可以不配置证书，但会打印未签名警告，并在 `BUILD_INFO.json`、`MANIFEST.json` 中记录 `code_signing_enabled=false`。正式发布模式会先签名主 EXE，验证后再构建安装器并签名安装器，最后生成 Manifest 和 SHA-256；缺少证书、时间戳或签名验证失败都会终止构建。

所有非 `SERVER_RELEASE_BUILD=1` 的 ZIP 和安装器文件名都会带 `_VALIDATION` 后缀，避免被误当作正式发布包。只有完整通过发布约束的构建才生成无后缀文件名。

SHA-256 只能证明文件与已知校验值一致，不能替代发布者身份认证。对外分发时必须同时发布已签名安装器和独立渠道提供的 SHA-256。
