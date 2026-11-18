# kind TLS 出口

先在有构建能力的机器构建 `deploy/egress/Dockerfile`，标记 `sca-egress:tls-v1`。
在创建集群后、启动受限沙箱前执行：

```bash
timeout 120 kind load docker-image sca-egress:tls-v1 --name sca
```

也可运行 `timeout 130 bash deploy/kind/load-egress.sh sca`。
若本机 kind 的 docker-image 加载失败，可先导出镜像，再用 image-archive 加载。
网络隔离测试需要 Calico；普通 kindnet 不验证节点隔离。
CA 在 Worker 宿主生成，测试使用独立临时目录，不需手工创建集群 CA。
生产多副本 Worker 的持久卷与权限要求见 `../k8s/worker/README.md`。
