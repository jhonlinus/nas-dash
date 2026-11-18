# NAS Dash · 飞牛 fnOS 应用（fpk）

把仓库根目录的 `nasdash.py` + `index.html` 打成飞牛私有云（fnOS）可安装的原生应用。

- 应用名：`nasdash`，显示名「NAS 状态面板」
- 类型：`native`（不依赖 Docker，只用 NAS 自带的 `python3`）
- 入口：走飞牛统一网关 `/app/nasdash/`，**不占端口**，外网经 FN Connect 也能打开
- 产物：`dist/nasdash-1.0.1.fpk`（约 41 KB）

## 安装

1. 把 `.fpk` 传到 NAS（任意目录）
2. 桌面 →「应用中心」→ 右上角「设置」→「手动安装应用」→ 选文件
3. 安装完成后桌面出现「NAS 状态面板」，点开直接在桌面内打开（同源 iframe）

**默认不启用登录认证。** 需要鉴权就在 `etc/nasdash.env` 里写 `NASDASH_AUTH=user:pass`，或者在前面加一层带 Basic 认证的 nginx。

## 目录结构

```
fnos/
├── manifest              # 应用元信息（appname / version / 桌面入口）
├── config/
│   ├── privilege         # run-as: root + 安装器创建的系统用户 nasdash
│   └── resource          # CPU / 内存限制
├── cmd/
│   ├── main              # 生命周期：start / stop / status
│   ├── install_init, install_callback
│   ├── upgrade_init, upgrade_callback
│   ├── config_init, config_callback
│   └── uninstall_init, uninstall_callback
├── app/
│   ├── server/           # 运行时代码（nasdash.py + index.html 的副本）
│   └── ui/
│       ├── config        # 桌面图标入口（关键：Socket 型网关）
│       └── images/       # 桌面图标
├── ICON.PNG, ICON_256.PNG
└── dist/                 # 打包产物
```

`app/server/` 里的两个文件是根目录同名文件的副本。改完核心代码要同步过去再打包：

```bash
cp ../nasdash.py ../index.html app/server/
```

## 为什么用 Socket 型入口

`app/ui/config` 长这样：

```json
{
  ".url": {
    "nasdash.Application": {
      "title": "NAS 状态面板",
      "icon": "images/icon_{0}.png",
      "type": "iframe",
      "protocol": "",
      "gatewayPrefix": "/app/nasdash",
      "gatewaySocket": "app.sock",
      "url": "/app/nasdash/"
    }
  }
}
```

飞牛有三种入口写法，只有第三种会生成 `/app/<appname>/` 路径：

| 写法 | 结果 | 外网 |
|---|---|---|
| `type: url` + 端口 | `http://NAS-IP:8066` | 打不开 |
| `type: iframe` + `port` + `url` | 「访问端口 8066 / 访问路径 /」 | 打不开 |
| `type: iframe` + `gatewaySocket` + `gatewayPrefix` | 「访问端口（空）/ 访问路径 /app/nasdash」 | 能打开 |

Socket 型的机制是：应用在 `$TRIM_APPDEST/app.sock`（即 `/vol1/@appcenter/nasdash/app.sock`）上监听 HTTP，飞牛网关 `trim_http_cgi` 先校验登录态，再把 `/app/nasdash/` 的请求转发进来。

因为请求路径带前缀，**应用必须自己剥掉它**。`nasdash.py` 里做了两件事：

- `_strip_gateway_prefix()`：把 `/app/nasdash/api/snapshot` 还原成 `/api/snapshot`；访问 `/app/nasdash`（无尾斜杠）时 302 到 `/app/nasdash/`
- 前端 `BASE` 推导：从 `location.pathname` 里提取 `/app/nasdash`，所有 `fetch` 都带上这个前缀，否则会打到飞牛首页上

网关会注入 `X-Trim-Userid` / `X-Trim-Username` / `X-Trim-Isadmin`。`nasdash.py` **只信任来自 127.0.0.1 的来源**，所以如果外面再套一层公网反代，必须把这三个头清空（见 `deploy/nginx.conf.example`）。

## 运行参数

配置文件 `/vol1/@appconf/nasdash/nasdash.env`（软链到 `/var/apps/nasdash/etc/nasdash.env`），改完重启应用生效：

```bash
NASDASH_PORT=8066                    # TCP 端口；0 = 只走 socket
NASDASH_BIND=127.0.0.1               # 默认只给本机反代；局域网直连改 0.0.0.0
NASDASH_SOCK=$TRIM_APPDEST/app.sock  # 网关 socket；置空则关闭
NASDASH_AUTH=                        # 空 = 不认证；要密码写 user:pass
NASDASH_INTERVAL=5                   # 采样间隔秒
NASDASH_DEBUG=0                      # 1 = HTTP 请求写进 var/data/requests.log
```

- 日志：`/var/apps/nasdash/var/nasdash.log`
- 历史数据：`/var/apps/nasdash/var/data/`（升级不丢）
- 程序目录：`/vol1/@appcenter/nasdash/`

## 为什么是 native 而不是 docker

程序就是一个 Python 文件加一个 HTML，零第三方依赖，也不需要公网镜像仓库；native 体积小、启动快，还能直接读宿主机的 `/sys`、cgroup v2 和 `smartctl`。

`config/privilege` 用 `run-as: root`，因为要读 `/sys/fs/cgroup`、执行 `docker ps` 和 `smartctl`：

```json
{
  "defaults": { "run-as": "root" },
  "username": "nasdash",
  "groupname": "nasdash"
}
```

`username` / `groupname` 是安装器要创建的系统用户，**不能写 `root`**，否则安装直接失败：

```
APP_INSTALL_FAILED_USER_ALREADY_EXISTS
```

## 重新打包

fpk 本质是一个 USTAR tar，成员顺序固定：

```
app.tgz → cmd/… → config/… → ICON.PNG → ICON_256.PNG → manifest
```

先同步代码，再用飞牛官方打包工具 `fnpack`（或任何能产出上面结构的工具）在 `fnos/` 下打包，产物丢进 `dist/`：

```bash
cp ../nasdash.py ../index.html app/server/
fnpack build            # 具体参数以你的 fnpack 版本为准
```

手写打包器的话，有三点必须注意，否则应用中心会拒装：

1. `manifest` 是 **CRLF** 换行，且 `checksum = md5(app.tgz)`
2. `app.tgz` 是 gzip，**MTIME 写 0**（否则同样内容每次校验值都不同）
3. tar 成员顺序不能变，`manifest` 必须最后

## 升级

改 `manifest` 里的 `version` 后重新打包，直接覆盖安装即可。`cmd/upgrade_*` 只做重启，数据目录 `var/data` 不受影响。
