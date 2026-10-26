<!--
  提交前请对照 CONTRIBUTING.md 第 3 节「合并准入清单」逐条自检。
  标题格式：<type>(<scope>): <中文简述>   例：fix(adapter): 撤回冷却只在成功撤回后记录
-->

## 改了什么

<!-- 一两句话说清变更范围。涉及多文件时按模块列点。 -->

## 为什么改

<!-- 问题现象 / 设计动机 / 关联 issue。写清楚"不改会怎样"。 -->

## 怎么验证

<!-- 贴命令与结果，不要只写"本地测试通过"。 -->
- [ ] `cd packages/core-engine && pytest`
- [ ] `cd packages/adapter-qq-agent && pytest`
- [ ] 手工冒烟：`/api/v1/health`、`/api/v1/chat`

## 风险点

<!-- 高危文件是否被触及？老库是否需要迁移？是否需要改 .env？ -->

- [ ] 未提交 `.env` / `*.db` / `.cache/` / `persona.txt` / `backups/`
- [ ] 对外接口或环境变量有变化时，已同步 `.env.example` 与 README
- [ ] 本分支已 rebase 到最新 `main`，无冲突残留

## 冲突取舍（仅合并冲突时填写）

<!-- 解决冲突时做了哪些取舍？删改对方代码的必须在此说明并 @ 对方。 -->

---

<details>
<summary>破坏性变更（如有请展开填写）</summary>

- 影响范围：
- 迁移步骤：
- 回滚方式：

</details>
