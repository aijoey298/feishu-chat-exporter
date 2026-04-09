# 环境配置

## 安装 lark-cli

### macOS

使用 Homebrew 安装：

```bash
brew install larksuite-official/lark-cli/lark-cli
```

### Linux

下载官方二进制文件：

```bash
# 下载最新版本（替换 VERSION 为实际版本号，如 v1.0.0）
curl -fsSL https://github.com/larksuite/lark-cli/releases/download/VERSION/lark-cli-linux-amd64 -o lark-cli
chmod +x lark-cli
sudo mv lark-cli /usr/local/bin/lark-cli
```

> 完整版本列表请访问 [lark-cli GitHub Releases](https://github.com/larksuite/lark-cli/releases)。

### Windows

**方式一：使用 Scoop**

```powershell
scoop bucket add lark-cli https://github.com/larksuite/scoop-bucket
scoop install lark-cli
```

**方式二：手动安装**

1. 从 [GitHub Releases](https://github.com/larksuite/lark-cli/releases) 下载 Windows 二进制文件（`.exe`）
2. 将其加入 `PATH` 环境变量

---

## 授权登录

lark-cli 使用 **Device Code Flow** 进行身份授权，无需在命令行中输入用户名和密码。

### 授权步骤

1. 在终端运行以下命令：

```bash
lark-cli auth login --recommend
```

2. 命令会输出一个 URL 和一段设备码（device code），类似：

```
Open the URL below in your browser to authorize:
https://authen.feishu.cn/device/authorize?user_code=XXXX-XXXX
Device code: XXXX-XXXX-XXXX
```

3. 在浏览器中打开该 URL，输入设备码完成授权
4. 授权成功后，终端会自动确认并显示登录状态

### 验证登录状态

```bash
lark-cli auth status
```

输出示例：

```
✅ Logged in as: your.name@company.com (User)
```

---

## 常见问题

### Token 过期

lark-cli 的访问令牌（access token）会过期，过期后需要重新授权：

```bash
lark-cli auth login --recommend
```

重新执行授权流程即可。

### 权限不足

如果遇到 `Permission denied` 或 `insufficient scope` 错误：

1. 确认当前登录账号具有访问对应资源的权限
2. 检查应用（App）已申请了所需的 [权限范围（Scopes）](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/scope-illustration)
3. 如使用企业自建应用，联系管理员在管理后台授权

### Linux/macOS 上找不到命令

确认 `lark-cli` 已在 `PATH` 中：

```bash
which lark-cli
# 如果没有输出，手动添加到 PATH
export PATH="$PATH:/usr/local/bin"
```

---

## Token 刷新

lark-cli 会自动管理 Token 刷新，通常无需手动操作。

- **Access Token**：有效期约 2 小时，过期后自动刷新
- **Refresh Token**：有效期约 30 天，刷新 Token 失效后需重新 `auth login`

如需强制刷新，可清除缓存后重新登录：

```bash
lark-cli auth logout
lark-cli auth login --recommend
```
