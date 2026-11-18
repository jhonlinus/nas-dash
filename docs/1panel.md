# 在 1Panel 里挂 NAS Dash

1Panel 的「网站」列表是**读数据库**（`agent.db` 的 `websites` / `website_domains` 表）渲染的，不扫 `conf.d` 目录。手写一个 conf 文件 nginx 能生效，但面板里看不到，续证书、看日志、改配置都得登机器。所以正确做法是按面板自己的结构落文件。

本文以 `dash.example.com` 为例。

## 目录结构

```
/opt/1panel/apps/openresty/openresty/www/sites/
├── conf.d/dash.example.com.conf                 # 主配置（面板原生模板格式）
└── dash.example.com/
    ├── proxy/
    │   ├── root.conf                            # 反代到 127.0.0.1:8066
    │   └── 00_hardening.conf                    # 认证 / 安全头 / 405 / robots
    ├── ssl/                                     # 证书（可复用泛域名证书）
    ├── auth_basic/auth.pass                     # htpasswd 文件
    ├── log/
    └── index/
```

主配置里由面板模板固定带一行：

```nginx
include /www/sites/dash.example.com/proxy/*.conf;
```

**所以加固一定要写在 `proxy/00_hardening.conf`**，不要直接改主配置：面板只要重新生成一次配置（改域名、切证书、点保存），手改的部分就没了，而 `proxy/*.conf` 会被重新 include。

## 三个坑

**1. `auth_basic` 不要开两次。**
在 `00_hardening.conf` 里写了 `auth_basic` 之后，别再去面板「网站设置 → 安全」里开一次 Basic 认证，nginx 会直接报：

```
"auth_basic" directive is duplicate
```

**2. `limit_except` 必须写在 `location` 里。** 写在 `server` 层是语法错误。

**3. 配置文件必须是 LF 换行。** 在 Windows 上编辑后直接 `scp` 上去，CRLF 会让 htpasswd 文件带上 `\r`，表现为「密码明明对，一直 401」。传之前 `dos2unix` 一下。

## 证书复用

如果面板里已经有一张 `*.example.com` 的泛域名证书，新站点可以直接指向它，不用重新申请：`websites.website_ssl_id` 填那张证书在 `website_ssls` 表里的 `id`。之后泛域名证书续期会自动覆盖到这个站点。

## 最少改动的手工流程

不想碰数据库的话，用「面板建站 + 手写 proxy 文件」的组合就够了：

1. 面板 → 网站 → 创建网站（反向代理类型），域名填 `dash.example.com`，上游填 `http://127.0.0.1:8066`
2. 面板会自动生成 `conf.d/dash.example.com.conf` 和 `sites/.../proxy/root.conf`，并登记数据库
3. 把仓库里的 [`deploy/nginx.conf.example`](../deploy/nginx.conf.example) 里的 location 段改写成 `proxy/00_hardening.conf` 放进去
4. `nginx -t && nginx -s reload`（或在面板里点保存，让面板 reload）

这样面板里能看见站点、能续证书、能看访问日志，加固也不会因为面板重新生成主配置而丢失。
