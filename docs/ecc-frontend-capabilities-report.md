# ECC 前端能力适配评估报告

*Generated: 2026-05-25 20:16 (UTC+8)*
*Scope: `/data/workspace/github/everything-claude-code` → `gg-proxy` dashboard*

> 调研目标：从 ECC (`everything-claude-code`) 仓库的 skills/agents/commands 中，挑选**直接可用于 `gg-proxy` 前端（dashboard）开发**的能力，并给出落地路径。
>
> 关联代码：`src/gg_relay/dashboard/`、`src/gg_relay/api/`

---

## 1. gg-proxy 当前前端技术栈

经现网验证（非推断）：

| 维度 | 现状 |
|---|---|
| 后端 | FastAPI（`api/main.py`，`api/routers/`） |
| 模板引擎 | Jinja2 ≥ 3.1（`pyproject.toml`） |
| 交互层 | **HTMX 1.9.12** + `json-enc` 扩展（`templates/base.html` L7、L15） |
| 样式 | 原生 CSS，单文件 `static/app.css`（**831 行**），已初步 token 化 |
| 主题 | `data-theme="dark" | "light"`，localStorage 持久化（`base.html` L17-26、L60-66） |
| JS | 极少量原生 JS（`static/batch_toolbar.js`，277 行） |
| 构建工具链 | **无**（无 Vite/Webpack/Tailwind 等） |
| 模板数量 | 27 个 `.html`，含 partial（`_kanban_card.html` 等） |

**结论**：服务端渲染 + HTML over the wire，**不存在 React/Vue 运行时**。

### 1.1 当前 design tokens（`app.css` L17-66）

已声明的 token 体系（部分截取）：

```17:66:src/gg_relay/dashboard/static/app.css
    /* ─── design tokens (multica-aligned) ──────────────────────────────
       Layered on top of the legacy palette above so existing rules and
       test assertions keep their colors. New components below consume
       these tokens directly. */
    --bg-0: #0b0f14;
    --bg-1: #0f1419;
    /* ...色板 / 间距 / 圆角 / 字号 / 阴影 / transition 全套 ... */
    --transition-fast: 120ms ease;
    --transition-base: 200ms ease;
}
```

意味着 design system 已存在雏形，但**未文档化**，且 legacy palette（`--bg / --panel / --accent`）和新 token（`--bg-1 / --accent-1`）双轨并存，存在一致性风险。

---

## 2. ECC 能力清单与适配度

按"对 gg-proxy 是否可直接使用"分三档。

### 2.1 ✅ 一档：直接可用（强推荐）

| 能力 | 类型 | ECC 路径 | gg-proxy 落地点 |
|---|---|---|---|
| **accessibility** | skill | `skills/accessibility/SKILL.md` | 所有 `dashboard/templates/*.html`（语义化 + ARIA + 键盘可达性） |
| **a11y-architect** | agent | `agents/a11y-architect.md` | 配合上面 skill 做架构级评审 |
| **design-system** | skill | `skills/design-system/SKILL.md` | `app.css` 831 行 token 审计 + 双轨 palette 收敛 |
| **brand-voice** | skill | `skills/brand-voice/SKILL.md` | 模板内按钮/提示/错误文案的语调统一 |
| **ui-demo** | skill | `skills/ui-demo/SKILL.md` | Playwright 录制 dashboard 演示（HTMX 页面照样可录） |
| **gan-style-harness** | skill | `skills/gan-style-harness/SKILL.md` | Generator + Evaluator 迭代单页视觉，约束栈即可 |
| **/gan-design** | command | `commands/gan-design.md` | 同上，提供评分 rubric 与阈值化迭代 |
| **/multi-frontend** | command | `commands/multi-frontend.md` | 多模型协同的前端工作流（Research → Plan → Execute → Optimize → Review） |
| **dashboard-builder** | skill | `skills/dashboard-builder/SKILL.md` | ⚠️ Grafana/SigNoz 用，**不用于** Jinja 页面；但你已集成 OTel，做外部观测面板时直接用 |

适配度说明：
- `accessibility` 明确支持 Web (HTML/ARIA)：`skills/accessibility/SKILL.md` L4、L14、L61、L81。
- `design-system` 框架无关，明确"扫描 CSS / Tailwind / styled-components"（L24）。
- `gan-style-harness` 仅约束 G/E 角色，stack 由 brief 指定。

### 2.2 ⚠️ 二档：思路可借鉴，需自行翻译到 HTMX/CSS

| 能力 | 不能直接用的原因 | 可借鉴的部分 |
|---|---|---|
| **motion-foundations** | 基于 `motion/react`（"Use `motion/react` only"，L49），强依赖 React/`"use client"` | **motion tokens、spring 预设、`prefers-reduced-motion` 强制规则** → 翻译为 CSS 变量 + CSS transitions/keyframes，并入 `app.css` |
| **motion-patterns / motion-advanced / motion-ui** | 全部 React/Framer Motion | 仅借鉴节奏与时序原则；按钮按下、Modal 进入、Toast 等 HTMX 已能通过 `hx-swap` + CSS 过渡实现 |
| **frontend-patterns** | 主体 React + Hooks + SWR/React Query | **组件组合、加载态、错误边界、可访问表单**思想 → 映射成 Jinja partial（你已经在用 `_kanban_card.html` 等）+ HTMX 的 `hx-trigger / hx-swap / hx-target` |
| **liquid-glass-design** | iOS 26 SwiftUI/UIKit | 仅在做"玻璃拟态"风格时借鉴 CSS `backdrop-filter` + `border` |

### 2.3 ❌ 三档：完全不适用（栈不匹配）

| 能力 | 排除原因 |
|---|---|
| `ui-to-vue` | Vue 3 + Vant/Element Plus/Ant Design Vue |
| `swiftui-patterns` | iOS 原生 |
| `frontend-slides` | 演示稿生成（除非用于项目分享） |
| `remotion-video-creation` | Remotion = React 视频生成 |
| `nextjs-turbopack` / `nuxt4-patterns` / `vite-patterns` | 构建工具/框架不存在 |
| `nestjs-patterns` / `fastapi-patterns`（属于后端） | 与"前端页面"主题无关 |

---

## 3. gg-proxy dashboard 当前可被改进的具体问题

基于对 `base.html` + `app.css` 的现场检查（**未经审计 skill 跑过，仅人工抽查**）：

### 3.1 一致性 / Design System
- ✋ **双轨色板**：`--bg / --panel / --text` 与 `--bg-1 / --bg-2 / --fg-1` 并存（`app.css` L1-31）。新组件用新 token，老组件用旧 token，PR 容易产生不一致。
- ✋ **Legacy topbar 残留**：`base.html` L41-52 `class="topbar"` 被 `hidden + aria-hidden="true"` 保留以兼容旧测试，长期会成腐烂资产。
- ✋ **无组件清单文档**：27 个模板（含 `_kanban_card.html` 等 partial）没有索引，新增页面时复用率低。

### 3.2 可访问性（accessibility）
- ✋ `<header class="topbar" hidden aria-hidden="true">` 嵌套在 `<header class="app-topbar">` 内（`base.html` L29、L41）→ **嵌套 header**，违反 HTML 语义。
- ✋ Theme toggle 按钮内联 onclick 6 行 JS（L60-66），缺少 `aria-pressed` 标示当前主题状态。
- ✋ Menu toggle 按钮 `☰` 用 unicode 字符替代图标（L31-34），屏幕阅读器读"汉堡符号"语义弱；好在有 `aria-label="Toggle navigation"`，但状态切换（开/合）未通过 `aria-expanded` 暴露。
- ✋ Search input 通过 inline `onkeydown` 跳转（L54-58），无 `<form>` 包装 → 无障碍上无明确的 form landmark。

### 3.3 动效与性能
- ✋ 已声明 `--transition-fast / --transition-base`（L64-65），但全文搜索 `app.css` 仅个位数处使用，**未形成规范**。
- ✋ 无 `@media (prefers-reduced-motion: reduce)` 兜底。
- ✋ HTMX 1.9.12 通过 unpkg CDN 加载（`base.html` L7、L15），无 SRI（Subresource Integrity）哈希，无本地 fallback。

### 3.4 文案（brand-voice）
- ✋ 各模板按钮文案随作者风格（"Accept" vs "Save" vs "确认"），无统一 voice profile。

---

## 4. 推荐落地路径（4 步）

### Step 1：体检（1-2 次会话，零代码改动）
- 跑 **`design-system`** skill：输出 `app.css` 的 token 一致性报告 + 双轨 palette 收敛建议
- 跑 **`accessibility`** skill + **`a11y-architect`** agent：对 `base.html / sessions_list.html / kanban.html / hitl_form.html` 做 WCAG 2.2 AA 审计
- 产出物：`docs/dashboard-audit-2026-05.md`

### Step 2：建立规范（沉淀到代码）
- 把 audit 结果落地为：
  - `app.css` 顶部 design tokens 注释化文档块（保留单文件结构，零迁移成本）
  - 新增 `docs/dashboard-components.md`：列出现有 27 个模板 + partial 用途、复用方式、HTMX 触发约定
  - `app.css` 末尾追加 `@media (prefers-reduced-motion: reduce) { *, *::before, *::after { transition: none !important; animation: none !important; } }`

### Step 3：演进具体页面
- 用 **`/gan-design`** 或 **`/multi-frontend`** 迭代单页（建议从 `overview.html` 或 `kanban.html` 开始）
- Prompt 必须显式约束：
  > Stack: FastAPI + Jinja2 + HTMX 1.9.12 + 原生 CSS（消费 `app.css` 中已有 token）。**禁止引入** React/Vue/Tailwind/构建工具。

### Step 4：交付演示
- 用 **`ui-demo`** skill 录 1-2 段 walkthrough（sessions list → detail → HITL 交互），放进 README

---

## 5. 不推荐做的事

- ❌ 不要为了用 motion-* 系列引入 React，stack 切换成本远大于收益
- ❌ 不要把 `dashboard-builder` 用于 Jinja 页面 —— 它服务于 Grafana/SigNoz
- ❌ 不要让 `/gan-design` 自由发挥 stack —— 必须在 brief 里约束栈，否则它会输出 React 代码

---

## 6. 附：ECC 仓库前端类能力索引（速查）

| 类别 | Skills（`skills/<name>/SKILL.md`） |
|---|---|
| 设计系统 / 视觉 | `design-system`、`brand-voice`、`liquid-glass-design` |
| Web 实现 | `frontend-patterns`（React）、`ui-to-vue`（Vue）、`swiftui-patterns` |
| 演示与交付 | `ui-demo`、`frontend-slides` |
| 动效 | `motion-foundations`、`motion-patterns`、`motion-advanced`、`motion-ui` |
| 可访问性 | `accessibility` |
| 监控可视化 | `dashboard-builder` |
| 设计探索 | `gan-style-harness` |

| Agents / Commands | 路径 |
|---|---|
| `a11y-architect` | `agents/a11y-architect.md` |
| `/gan-design` | `commands/gan-design.md` |
| `/multi-frontend` | `commands/multi-frontend.md` |

---

## 7. 下一步建议（待你确认）

1. **是否要我立刻执行 Step 1 体检**（对 `app.css` + 4 个核心模板跑 `design-system` + `accessibility`），输出 `docs/dashboard-audit-2026-05.md`？
2. 或者**先收敛双轨 palette**（把 legacy `--bg / --accent` 全量替换为新 token，删除冗余声明）？
3. 或者**先做 Step 4 录一段 ui-demo** 用于对外演示？

任选其一即可启动。
