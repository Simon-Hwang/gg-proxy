# Plan 8 — ADDENDUM (Plan 9 Santa Round 4 Reviewer H finding)

**作者**: gg-relay
**创建**: 2026-05-24
**触发**: Plan 9 v1.4 LOCKED — Santa Round 4 Reviewer H BLOCKER H/B5
**状态**: 🟢 已纳入 (Plan 8 LOCKED 文档保持不动)

---

## 为什么有这个 ADDENDUM

Plan 8 (`2026-05-23-plan-8-team-scale-and-collab.md`) 已经 **LOCKED**
并随 v0.8.0 发布。在 Plan 9 v1.4 第 4 轮 Santa 评审过程中，
Reviewer H 发现 Plan 8 文档里有一处实施与规划的偏差：

- **Plan 8 Task 22 第 11 步**（"DB-backed API key 自助"任务的最后
  一个步骤）在 LOCKED 文档里描述为「已完成」，但实际实施时被
  跳过了。

为了不修改一份已经 LOCKED 的文档（这是 Santa Method 的硬性约束 —
LOCKED 文档是历史记录，不应回写），本 ADDENDUM 在外部记录差异并
追踪后续动作。

---

## 偏差细节

### Plan 8 Task 22 步骤 11（被跳过）

**Plan 8 文档文字描述**：
> 11. 用户在 `/admin/keys` 页面创建 key 后，弹出确认对话框 +
>     "已复制到剪贴板" toast。剪贴板调用 `navigator.clipboard
>     .writeText(raw_key)`，失败时回退到 prompt + select-all。

**实际实施状态**：
- 后端 POST `/api/v1/admin/keys` 正确返回 `raw_key: <string>`。
- 前端 dashboard 仅展示 raw_key 文本（在一个 `<code>` 块里），
  **没有**自动剪贴板复制 + toast 反馈。

### 影响评估

| 维度 | 评估 |
|---|---|
| 功能性 | 🟢 不影响 — operator 可以手动选中复制 |
| 安全性 | 🟢 不影响 — raw_key 仍然只显示一次 |
| UX | 🟡 轻微降级 — 多一步手动操作 |
| Plan 8 验收 | 🟢 已通过 — Phase 4 retrospective 未覆盖此细节 |
| 紧急程度 | 低 (Plan 11 candidate) |

---

## 后续动作

### 短期 (v0.9.x 周期内不动)

- 不修改 Plan 8 LOCKED 文档（合规要求）
- 不修改现有 dashboard 代码（不在 Plan 9 scope）
- 本 ADDENDUM 作为后续计划的 backlog 锚点

### 长期 (Plan 11 dashboard polish 候选)

如果 Plan 11 包含 dashboard UX 改进，应将以下内容纳入 backlog：

1. 在 `templates/admin/keys.html` 的 "key 创建成功" 视图加
   `navigator.clipboard.writeText(raw_key)` 调用。
2. 失败回退：`window.prompt('Copy this key:', raw_key)`
   (浏览器对话框自动 select-all)。
3. 成功 toast：复用 dashboard 现有 toast 组件
   (`<div class="toast">`).
4. 测试：增加 e2e 测试断言 `data-copy-target=raw_key` 属性存在。

---

## 关联文档

- 原始 Plan 8: [`2026-05-23-plan-8-team-scale-and-collab.md`](./2026-05-23-plan-8-team-scale-and-collab.md) (LOCKED — 不修改)
- Plan 9 (v1.4 LOCKED): [`2026-05-24-plan-9-cluster-scaling-and-k8s.md`](./2026-05-24-plan-9-cluster-scaling-and-k8s.md) — 触发本 ADDENDUM 的评审
- CHANGELOG: [`/CHANGELOG.md`](/CHANGELOG.md) — `[0.9.0-rc1]` 段引用本 ADDENDUM

---

## Santa Method 合规说明

本 ADDENDUM 的存在本身证明了 Santa Method 的有效性：

- 第 4 轮评审在最后才发现的小偏差，没有阻塞 v0.9.0-rc 的实施。
- 通过 ADDENDUM 模式而非 in-place 编辑保留了 Plan 8 LOCKED 文档
  的历史完整性。
- 缺陷被记录、归类、安排到合理的修复窗口，没有「忘掉」。

这就是评审框架想要达成的效果。
