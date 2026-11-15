# Worker 伸缩示例

`kubectl kustomize deploy/k8s/worker` 渲染 Deployment、ConfigMap、PDB 和 CPU HPA。
镜像 `sca:latest` 是占位符，需包含 Python、sca 和 kubectl；当前 CLI 没有
`sca worker` 子命令，使用等价的 `python -m sca.orchestration.worker`。

部署前替换 ConfigMap 中的服务地址、S3 桶和区域；在同一命名空间通过密钥管理工具
创建名为 `sca-worker` 的 Secret，键为 `SCA_DATABASE_URL`、`SCA_S3_ACCESS_KEY`、
`SCA_S3_SECRET_KEY`，不要把真实值写入清单。Secret 不应覆盖 ConfigMap 的运行模式。
预先创建 `sca-worker` ServiceAccount、`sca-sandboxes` 命名空间及该账号管理沙箱所需的
最小 RBAC（Pod、exec、网络策略和出口代理等）；此处未提供权限模板。
沙箱镜像、出口限制和网络插件按 B2 配置。默认使用 fake Agent；Kubernetes 尚未接通
模型网关，切换真实 Agent 前需补凭证安全方案，不要向沙箱挂载真实模型密钥。

Worker 使用 `sync` + S3，emptyDir 仅作可重建缓存，不使用共享卷。
`SCA_WORKER_METRICS_PORT=9464` 启动 Temporal HTTP 指标服务，业务指标在 9465；
两者只监听 127.0.0.1，readiness 在容器内请求 9465 的 `/metrics`，仅证明 HTTP 服务存活，
不证明 Temporal 已连接或正在轮询。集群 Prometheus 不能直接抓 Pod IP，需后续增加转发或调整监听地址。
660 秒终止宽限超过当前最长 600 秒活动心跳超时；现有入口没有显式 SIGTERM 排空逻辑，
不能保证等待活动完成，故障恢复仍靠 Temporal 超时重试和对象存储检查点。

CPU HPA 依赖 metrics-server；远端 Agent 执行期间 CPU 可能很低。可选的
[keda.yaml](keda.yaml) 全为注释且不参与渲染，按
[KEDA Temporal scaler](https://keda.sh/docs/2.17/scalers/temporal/) 使用任务队列积压。
启用前移除并删除 CPU HPA，核对 Temporal 地址、命名空间、队列及认证配置，避免两个 HPA 争抢副本数。

已做离线 YAML 解析和客户端 kustomize 渲染；未部署到集群，未验证镜像、RBAC、
探针、排空、实际伸缩或 KEDA。负责人需在 kind 做服务端 dry-run 和运行验证。
