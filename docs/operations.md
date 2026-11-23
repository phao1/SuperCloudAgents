# 租户运营

升级数据库到包含 `0020_tenancy_billing` 的版本。用与 Worker 相同的数据库和对象存储配置运行
`sca admin usage-backfill`，补算已有终态任务，包括已归档的事件。命令可以重跑。配置独立的
`SCA_PLATFORM_ADMIN_TOKEN`，用它访问 `/v1/admin/*`。租户管理员令牌只用于本租户。

## 新建租户和邀请成员

用平台管理员令牌创建租户并邀请首位所有者。租户标识使用小写字母、数字和连字符。

```bash
curl -X POST "$SCA_API_URL/v1/admin/tenants" \
  -H "Authorization: Bearer $SCA_PLATFORM_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"id":"acme","display_name":"Acme","plan":"team","owner_email":"owner@example.org"}'
```

未接入 OIDC 时，平台管理员可直接为租户签发首个管理员令牌，响应只返回一次原始令牌：

```bash
curl -X POST "$SCA_API_URL/v1/admin/tenants/acme/tokens" \
  -H "Authorization: Bearer $SCA_PLATFORM_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"Acme owner","days":365}'
```

所有者首次通过 OIDC 登录时，以已验证邮箱领取待激活的邀请。后续按 OIDC `sub` 识别成员。
OIDC 服务应在令牌中提供 `email` 和 `email_verified=true`。用户有多个租户的邀请或成员身份时，
OIDC 服务需按 `SCA_OIDC_TENANT_CLAIM` 提供目标租户标识。已有租户继续接受部署方配置的
OIDC 租户声明和 `SCA_API_TOKENS`，已邀请成员的角色以成员记录为准。

所有者和租户管理员可以查看或修改租户显示名称（`GET/PATCH /v1/tenant`），并用以下接口
邀请和列出成员。创建记录后，运营人员可将登录地址交给受邀者。

```bash
curl -X POST "$SCA_API_URL/v1/tenant/members" \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"email":"reader@example.org","role":"viewer"}'

curl "$SCA_API_URL/v1/tenant/members" -H "Authorization: Bearer $TENANT_TOKEN"
```

平台管理员也可以用 `POST /v1/admin/tenants/acme/members` 补邀所有者。成员角色有
`owner`、`admin`、`member`、`viewer`。租户管理员用 `POST /v1/tenant/tokens`
创建令牌，提交 `name` 和可选的 `role`（`admin`、`member`、`viewer`），有效期默认 90 天。
创建响应只返回一次原始令牌。用 `GET /v1/tenant/tokens` 查看令牌，用
`DELETE /v1/tenant/tokens/{token_id}` 吊销。

平台管理员用 `GET /v1/admin/tenants` 列出租户，用 `GET/PATCH /v1/admin/tenants/{id}`
查看或修改显示名称及套餐。用 `POST /v1/admin/tenants/{id}/suspend`
和 `POST /v1/admin/tenants/{id}/resume` 暂停和恢复提交。暂停期间提交返回 HTTP 403，
已受理的任务可继续。删除使用 `DELETE /v1/admin/tenants/{id}`，会关闭租户访问并吊销其
数据库令牌，保留历史任务、用量和租户标识。

## 本机 Portal 访问令牌

源码和 Docker 演示启动脚本默认允许本机浏览器以 `eval` 管理员身份免令牌访问。打开
`http://127.0.0.1:8000/portal`，在「设置 → 访问与安全」打开「需要访问令牌」后，
新请求立即需要令牌才能以管理员身份操作。用 Bash 的 `grep SCA_EVAL_API_TOKEN .data/portal.env`
或 PowerShell 的 `Select-String SCA_EVAL_API_TOKEN .\.data\portal.env` 在本机私下查看令牌。

启动默认值是 `SCA_PORTAL_REQUIRE_TOKEN=0`。启动脚本优先使用进程环境中的值，其次是
`.data/portal.env` 已保存的值，再其次是 `.env`，并将结果保存到 `.data/portal.env`。
运行时开关保存在 `.data/portal-access.json`，优先于启动默认值。配置文件损坏时要求令牌。

免令牌只对启动脚本验证过的回环监听生效，API 使用 `--no-proxy-headers`。Host 须为
`127.0.0.1:8000`、`localhost:8000` 或 `[::1]:8000`。有代理转发头的请求要求令牌；
对外反向代理请使用普通 API 启动方式。`ssh -L 8000:127.0.0.1:8000 host` 按本机访问处理。
浏览器写请求须同源。本机脚本发起写请求时继续使用令牌，免令牌开启时普通 GET 可以省略令牌。

```bash
API=http://127.0.0.1:8000
curl -fsS "$API/v1/portal/access"
curl -fsS -X PUT "$API/v1/portal/access" \
  -H "Authorization: Bearer $SCA_EVAL_API_TOKEN" \
  -H 'Content-Type: application/json' -d '{"require_token":true}'
```

`GET /v1/portal/access` 返回 `require_token` 和 `local_no_token_active`，后者表示当前连接是否可用
免令牌方式。现有只读访客配置仍独立生效。若希望开启令牌后连历史浏览也要求令牌，启动前设置
`SCA_GUEST_VIEW=off`。[访客范围](#只读访客模式)

## 只读访客模式

本机免令牌管理员权限未生效时，`SCA_GUEST_VIEW` 决定是否允许无令牌读取访客历史。
默认 `local` 允许从回环地址直接连接的 `GET` 请求，设为 `off` 可关闭访客历史。
`SCA_GUEST_TENANT` 指定访客可访问的唯一租户，独立部署默认是
`default`，通过 `portal_demo.sh` 启动时默认是保存演示任务、评测和演进记录的 `eval`。
已有演示安装用 `bash scripts/portal_demo.sh up --mode docker` 或 `--mode source` 启动即可使用该默认值。
运行中的 Portal 先执行 `bash scripts/portal_demo.sh down`。显式配置的 `SCA_GUEST_TENANT` 优先。

访客可查看任务、执行图、插话记录、事件（含流式更新）、文本制品和补丁、评测及演进历史（`eval`），
以及租户用量。访客历史会遮盖邮箱、常见凭证和账单标识，插话记录中的 `actor` 显示为 `[redacted]`。
项目工作区及其 API、知识文档、二进制制品、原始终端及所有写操作需要工作空间身份。

**危险：`SCA_GUEST_VIEW=all` 会把指定租户的历史对所有能连接 API 的人开放，包括无令牌的远程用户。**
任务指令、轨迹和补丁可能包含业务数据。只对允许公开的历史使用此模式，并显式设置
`SCA_GUEST_VIEW_ACKNOWLEDGE_RISK=1`，否则服务拒绝启动。

`local` 和归档查看器要求回环 TCP 对端、唯一的回环 `Host`（`127.0.0.1`、`localhost` 或 `[::1]`，端口不限）且无代理转发头，并拒绝非回环 `Origin` 和 `Sec-Fetch-Site` 为 `cross-site` 或 `same-site` 的请求。可通过 `ssh -L` 转发后使用本机回环地址访问。
脚本和 `make api` 使用 Uvicorn 的 `--no-proxy-headers`。自定义启动命令也应保留这一参数。
**若反向代理通过回环地址连接 API，所有经该代理转发的访问都会被视为本地连接。** 对外服务请设置
`SCA_GUEST_VIEW=off`，或让代理通过非回环地址连接 API。

```bash
API=http://127.0.0.1:8000
curl -fsS "$API/v1/portal/config"
curl -fsS "$API/v1/tasks"
curl -fsS "$API/v1/usage?group_by=day"
# 从任务列表取得 TASK_ID
curl -fsS "$API/v1/tasks/$TASK_ID/dag"
curl -fsS "$API/v1/tasks/$TASK_ID/messages"
```

访客的配置响应包含 `guest_view: true`、`guest_tenant` 和 `model_configured`。
演示环境没有 DeepSeek 密钥时，Docker 和源码模式均以空模型列表启动 LiteLLM。此时用令牌提交
任务、评测或演进会得到 HTTP 409 和 `error.code: "model_not_configured"`。在本机 Portal 的
「设置 → 模型」保存密钥会重启网关。手工更新 `.env` 后可用
`bash scripts/portal_demo.sh restart-gateway` 应用变更。

## 套餐与用量

用 `GET /v1/admin/plans` 查询套餐。平台新建租户默认使用 `free`，已有租户沿用
`enterprise`，继续适用原有租户和全局运行槽位上限。

| 套餐 | 每月模型成本上限（美元） | 并发任务上限 | 单任务模型成本上限（美元） |
| --- | ---: | ---: | ---: |
| free | 10 | 1 | 1 |
| team | 100 | 4 | 10 |
| enterprise | 由部署方配置 | 沿用租户原有上限 | 由部署方配置 |

模型费用由 LiteLLM 网关结算。任务终态后，以 UTC 完成日期累计任务数、Agent 轮次、Token、
模型费用和沙箱时间，子任务与复用父任务 ID 的 Best-of 尝试一起计入父任务。只有汇总费用的
历史多模型任务，其未归因部分计到 `unknown`。按模型分组时，同一个任务可出现在多个模型中。

提交时自动为任务设置适用的套餐成本上限及当月剩余额度。请求的预算超过单任务上限，或当月
额度已经用完时，提交返回 HTTP 402 和 `quota_exceeded`。当月额度按已完成任务的网关成本
计算。已有排队上限仍适用。并发限制由准入控制按运行槽位执行。

```bash
curl "$SCA_API_URL/v1/usage?from=2026-11-01&to=2026-11-30&group_by=model" \
  -H "Authorization: Bearer $TENANT_TOKEN"
curl "$SCA_API_URL/v1/admin/usage?from=2026-11-01&to=2026-11-30" \
  -H "Authorization: Bearer $SCA_PLATFORM_ADMIN_TOKEN"
curl "$SCA_API_URL/v1/usage/export.csv?from=2026-11-01&to=2026-11-30" \
  -H "Authorization: Bearer $TENANT_TOKEN" -o usage.csv
```

日期区间包含首末两天。省略时默认本月第一天至今天（UTC）。JSON 用量默认按日分组，也可
指定 `group_by=model`。CSV 每个所选自然月一行，费用为网关成本乘以 `SCA_BILLING_MARKUP`，
再加该租户当前套餐的月基础费，四舍五入到美分。`SCA_BILLING_BASE_FEES_USD` 接受 JSON
映射，例如 `{"free":0,"team":29,"enterprise":99}`。默认倍率为 1，基础费为 0。

## Stripe 订阅与付款

1. 将数据库升级到 `0022_payments`。在 Stripe 测试模式下创建按月收费的 USD Price。用数据库管理账号把 Price ID 写入对应套餐。`included_usage_usd` 是月费已包含的网关成本额度，可低于提交额度。省略时沿用 `monthly_cost_limit_usd`，两者均为空时按 0 美元计算。

   ```sql
   UPDATE plans SET stripe_price_id = 'price_REPLACE_WITH_TEST_PRICE_ID',
                    included_usage_usd = 20 WHERE name = 'team';
   ```

2. 在 API 环境中配置 `SCA_STRIPE_SECRET_KEY` 和 `SCA_STRIPE_WEBHOOK_SECRET`。在 Stripe Workbench 创建接收快照事件的 webhook，地址为 `https://<API 域名>/v1/billing/stripe/webhook`。订阅 `checkout.session.completed`、`customer.subscription.created`、`customer.subscription.updated`、`customer.subscription.deleted`、`invoice.paid` 和 `invoice.payment_failed`。本地可用 Stripe CLI 转发测试事件，使用监听命令给出的签名密钥。[Stripe webhook 测试说明](https://docs.stripe.com/webhooks#test-webhook)

   ```bash
   stripe listen --forward-to "$SCA_API_URL/v1/billing/stripe/webhook" \
     --events checkout.session.completed,customer.subscription.created,customer.subscription.updated,customer.subscription.deleted,invoice.paid,invoice.payment_failed
   ```

3. 在 Stripe 启用客户门户的付款方式更新和发票历史。若允许租户自助更换套餐或取消订阅，在客户门户启用相应功能，套餐仅选已映射的月度 Price。结账成功及取消后的默认地址为 `$SCA_API_URL/portal#/team`，可分别用 `SCA_BILLING_SUCCESS_URL` 和 `SCA_BILLING_CANCEL_URL` 覆盖。删除租户前先在 Stripe 取消该租户的订阅。[Stripe 客户门户配置](https://docs.stripe.com/customer-management/configure-portal)

租户管理员在 Portal 的 **团队与用量 → 订阅与付款** 选择套餐。已有订阅的租户通过 **管理付款方式** 打开 Stripe 客户门户。订阅状态、当前套餐、计费周期结束时间和最近 10 张发票来自本地数据库，由 webhook 更新。以下两个 POST 返回 `{"url":"…"}`。

```bash
curl "$SCA_API_URL/v1/tenant/billing" -H "Authorization: Bearer $TENANT_TOKEN"
curl -X POST "$SCA_API_URL/v1/tenant/billing/checkout" \
  -H "Authorization: Bearer $TENANT_TOKEN" -H 'Content-Type: application/json' \
  -d '{"plan":"team"}'
curl -X POST "$SCA_API_URL/v1/tenant/billing/portal" -H "Authorization: Bearer $TENANT_TOKEN"
curl "$SCA_API_URL/v1/admin/billing" -H "Authorization: Bearer $SCA_PLATFORM_ADMIN_TOKEN"
curl "$SCA_API_URL/v1/admin/billing?view=invoices&tenant_id=acme" \
  -H "Authorization: Bearer $SCA_PLATFORM_ADMIN_TOKEN"
```

`GET /v1/admin/billing` 列出各租户的订阅状态。`view=events` 可查看 webhook 事件的编号、类型和接收时间。发票金额使用币种的最小单位。订阅欠费或 Stripe 发票的付款失败次数达到 `SCA_BILLING_PAYMENT_FAILURE_ATTEMPTS`（默认 3）会暂停提交，相关欠费结清后可自动恢复。订阅取消会将套餐切回 `free` 并暂停提交，租户可以重新订阅并付款。平台管理员设置的暂停由平台管理员恢复。

每个 UTC 自然月结束后，使用 `SCA_DATABASE_URL` 和 `SCA_STRIPE_SECRET_KEY` 运行月度用量命令。可由定时任务每月调用一次。

```bash
sca admin report-usage-to-stripe --month 2026-10
```

命令使用已有用量数据和当前订阅套餐的包含额度，把超出额度的网关成本乘以 `SCA_BILLING_MARKUP` 并四舍五入到美分，作为 USD 项目加入对应订阅的下一张发票。月订阅费由 Stripe Price 收取。金额大于 0 时，每个租户每月登记一项，登记后金额固定，重跑会跳过已报送的项目。返回的 `failed_tenants` 是可重跑的租户，任一报送失败时命令以非零状态退出。

## Stripe 模拟器

在已完成 Portal `setup` 和 `0022_payments` 迁移的演示数据库上启用模拟付款。启用前如有正在运行的
Portal，先执行 `down`。脚本将模拟密钥和 `team`、`enterprise` 的占位 Price ID 保存到
`.data/portal.env`，对本次运行设置 `SCA_STRIPE_API_URL=http://127.0.0.1:12111`。启动时为尚未配置
Price 的套餐写入占位映射，已有映射继续可用。`down` 停止模拟器并移除本次使用的占位映射，保留生成的配置供下次启动。

```bash
SCA_STRIPE_SIMULATOR=1 bash scripts/portal_demo.sh up
bash scripts/portal_demo.sh down
```

以新演示租户的管理员身份打开 Portal 的 **团队与用量 → 订阅与付款**，选择套餐，打开结账链接，点击
**完成付款**。模拟器将结账和订阅事件签名发送到 SCA，收到确认后跳回结账会话的成功地址。
「管理付款方式」打开模拟账单页，可查看模拟发票。状态保存在内存中，每次重启模拟器后请用新演示租户。
远程浏览器访问时通过 SSH 转发 `8000` 和 `12111` 两个端口。

也可单独启动。为 API 和模拟器注入同一个 `SCA_STRIPE_WEBHOOK_SECRET`，为 API 配置占位的
`SCA_STRIPE_SECRET_KEY`、模拟器的 `SCA_STRIPE_API_URL` 和演示套餐的 Price 映射。

```bash
.venv/bin/python -m sca.billing.stripe_sim --port 12111 \
  --webhook-url http://127.0.0.1:8000/v1/billing/stripe/webhook
```

`CHECKOUT_URL` 是 `POST /v1/tenant/billing/checkout` 返回的链接，`SUBSCRIPTION_ID` 取自模拟完成接口的
`subscription` 字段。付款失败会重试同一张发票，默认第三次暂停提交。模拟付款成功后可在租户账单里核对
发票与恢复状态。发票包含 29 美元的模拟月费和该订阅尚未入账的超额用量项目。

```bash
curl -fsS -X POST "$CHECKOUT_URL/complete"
for attempt in 1 2 3; do
  curl -fsS -X POST "http://127.0.0.1:12111/_sim/subscriptions/$SUBSCRIPTION_ID/payment_failed"
done
curl -fsS -X POST "http://127.0.0.1:12111/_sim/subscriptions/$SUBSCRIPTION_ID/payment_succeeded"
curl -fsS "$SCA_API_URL/v1/tenant/billing" -H "Authorization: Bearer $TENANT_TOKEN"
```

同一订阅支持 `POST /_sim/subscriptions/{id}/update`（JSON 含 `price_id`）与
`POST /_sim/subscriptions/{id}/cancel`，分别模拟换套餐和取消订阅。回调如返回失败，可重试同一个模拟操作。
自动验收命令见 [基础商用验收](commercial-readiness.md#运行烟测)。

## 源码模式

**本地源码模式：任务在本机直接运行，无隔离，仅限个人试用。** Agent、准备命令和验证命令
都以当前用户身份在宿主机运行。只提交信任的仓库和命令。任务的 `image`、容器资源限制与
`SCA_EGRESS_ALLOW` 在此模式下不生效。

- **Linux x86_64 或 arm64（glibc）**：准备 Git、Python 3.12（含 `venv`）和 OpenSSL。Debian/Ubuntu
  可运行 `sudo apt install postgresql nodejs npm`。使用普通用户启动。
- **macOS Intel 或 Apple Silicon**：运行 `brew install python@3.12 postgresql@16 node git`，
  确保 `python3.12`、`node` 和 `npm` 在 `PATH` 中。源码模式支持系统 Bash，无需 GNU timeout。
- **Windows**：使用 PowerShell 运行 `.\scripts\portal_source.ps1 up`。
  [Windows 安装与使用](windows.md)

在仓库根目录运行下列命令。首次启动会从 PyPI、GitHub 和 Prisma 的发布服务下载依赖。

```bash
bash scripts/portal_demo.sh up --mode source
bash scripts/portal_demo.sh status
bash scripts/portal_demo.sh logs
bash scripts/portal_demo.sh down
```

脚本用 `initdb` 和 `pg_ctl` 管理 `.data/pg`，优先监听本机 5432，端口占用时选择空闲端口。
它为 API 和 Worker 设置 `SCA_DATABASE_URL`、执行 `alembic upgrade head`，并为网关创建独立的
`litellm` 数据库。原生 Postgres 可选安装 pgvector，缺少扩展时记忆检索使用近期记录。
Temporal CLI 1.5.0 的开发服务监听 7233，数据库保存在 `.data/temporal/dev.db`。LiteLLM 1.110.2 在
`.data/litellm-venv` 中运行，监听 `127.0.0.1:4000`。可以在 Portal 的「设置 → 模型」保存 DeepSeek API Key，
保存会重启本机网关。 [Temporal 发布包](https://github.com/temporalio/cli/releases/tag/v1.5.0) [LiteLLM 版本](https://pypi.org/project/litellm/1.110.2/)

脚本把当前模式保存在 `.data/portal.env`，并把 PID 和日志保存在 `.data/run/`。
源码模式默认本机免令牌，登录所需的令牌位于 `.data/portal.env` 的 `SCA_EVAL_API_TOKEN`。
停止源码模式会保留本机数据库。切换到源码模式前，可在 Docker 模式执行
`bash scripts/portal_demo.sh down --all` 释放网关和 Temporal 的端口，两种模式分别使用各自的数据库。

在 Portal 的「任务 → 新建任务」选择 OpenCode。源码模式把上游 OpenCode v2.3.0 注册为
`2.3.0-sca.0`，支持该版本和模型对应的运行时冠军覆盖。OpenCode 源码修复、构建和冠军固化
需要 Docker。Codex 和 Pi 仅在找到本机可执行文件时提供。Pi 使用兼容 1.5.2 的 CLI。
选择 DeepSeek Harness 可运行本机 `dsh`。启动脚本用 npm 安装 `SCA_DSH_VERSION` 指定的
版本到 `.data/tools/dsh`，当前默认版本需要 Node.js 24.9 或更高。各 Agent 使用对应接口
支持的网关模型。
验证和准备命令使用本机的 Git、Python 和其他项目依赖，文件路径可使用当前工作目录、
`$HOME` 和 `$TMPDIR`。临时任务目录在 `.data/sandboxes/`。使用 MCP `tools` 的任务仍需受限出口的
Docker 沙箱。视觉 Intent 的 Playwright 检查依赖预设的浏览器和工具路径。

使用 Portal 登录令牌查询运行信息，也可以直接提交任务。以下示例中的仓库为本机路径。

```bash
curl -fsS http://127.0.0.1:8000/v1/portal/config -H "Authorization: Bearer $PORTAL_TOKEN"
curl -fsS http://127.0.0.1:8000/v1/tasks -H "Authorization: Bearer $PORTAL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"coding","spec":{"repo_url":"/path/to/repo","instruction":"修复失败的测试","verify_command":"python -m pytest -q","use_champion":true}}'
```

配置接口返回 `sandbox_driver: "local"`、`warning` 和 `unavailable_agent_backends`。

## 工作目录和缓存清理

`SCA_TASK_RETENTION_DAYS` 默认 14 天。定时维护清理超过保留期的终态任务目录
`data_dir/tasks/<id>`，任务记录、事件与已发布的制品仍可查询。默认开启
`SCA_CACHE_CLEANUP`。任务结束并销毁沙箱后会清理 `agent_home/.cache/opencode/bin`，
定时维护也会补清终态任务的该缓存。Docker 工作目录遇到沙箱属主权限问题时，使用隔离的
清理容器处理。回收字节数记录于 `maintenance.disk_reclaimed` 事件，已归档任务记入维护日志。

### 现场验证（2026-11-21）

本机以真实 API/Worker 进程对接本地假 Stripe 服务验证：结账返回付款链接；伪造签名的 webhook 返回 400，
合法签名的 `checkout.session.completed` 将套餐切到 `team` 并记录订阅；连续 3 次 `invoice.payment_failed`
暂停提交（提交返回 403 `tenant_suspended`），`invoice.paid` 后自动恢复并记录发票；平台管理员视图正确。
真实 Stripe 测试模式尚未接入。

## 运行中插话

升级数据库到 `0023_task_messages`，然后重启 API 与 Worker。在 Portal 打开任务页，在事件记录下方的
「插话」输入框补充要求。按回车发送，按 Shift + 回车换行。租户成员与管理员可以发送，查看者可以查看
记录。终态任务只保留历史。

`queue`（默认）把消息按发送顺序放到下一轮指令开头。若本轮已完成并通过验证，待处理的插话会触发
一轮续跑。`interrupt` 会停止当前单代理轮次，保存已完成的文件编辑，再沿用可恢复的会话开启下一轮。
审核中的插话会发起修改并重新提交审核。并行子任务或 Best-of-N 尝试期间收到的消息，会在合并或选定
候选后交给主任务。恢复执行仍受任务的用量及费用上限约束。
升级前已经开始的轮次会先执行到边界，再接收插话。

```bash
curl -fsS -X POST "$SCA_API_URL/v1/tasks/$TASK_ID/messages" \
  -H "Authorization: Bearer $SCA_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"先补充空输入的回归测试，再继续。","mode":"queue"}'

curl -fsS "$SCA_API_URL/v1/tasks/$TASK_ID/messages" \
  -H "Authorization: Bearer $SCA_API_TOKEN"
```

每条消息最多 4000 字符。发送成功返回 201，终态任务返回 409。列表按发送顺序返回，登录后可通过
`actor` 查看发送者。事件记录中的状态与列表一致，依次为待送达（`pending`）、已加入轮次指令（`delivered`）、
该轮已完成（`applied`）。被打断轮次中的插话会带入下一轮，直到有一轮完成。

### 源码模式下立即打断

在源码模式的 Portal 中勾选「立即打断当前轮」，或通过 API 发送 `mode: "interrupt"`。
即使 Agent 命令还在启动，Worker 也会先停止该命令的进程组，再保存已完成的工作区编辑并继续。

```bash
curl -fsS -X POST "$SCA_API_URL/v1/tasks/$TASK_ID/messages" \
  -H "Authorization: Bearer $SCA_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"先修复空输入处理。","mode":"interrupt"}'
```
