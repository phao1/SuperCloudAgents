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
