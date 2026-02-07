# Git Worktree 最佳实践

> **核心理念**：多个独立工作区，共享同一个 Git 仓库

## 核心概念

### Git Branch vs Git Worktree

| 维度 | Git Branch | Git Worktree |
|------|-----------|--------------|
| **本质** | 指针（commit 引用） | 文件目录（工作副本） |
| **数量** | 一个仓库可有无限分支 | 可有多个工作目录 |
| **共享** | 所有分支共享 `.git/` | 所有 worktree 共享 `.git/` |
| **切换成本** | `git checkout` 需要暂存未提交修改 | 无需切换，直接访问不同目录 |
| **修改隔离** | 切换时需处理未提交修改 | 每个工作区独立修改 |
| **适用场景** | 顺序开发，单个任务 | 并行开发，多任务 |

### 架构图

```
┌─────────────────────────────────────────────────┐
│              Git Repository                     │
│  ┌─────────────────────────────────────────┐   │
│  │  .git/ (共享对象数据库)                 │   │
│  │  ├── objects/ (所有 commit、blob)      │   │
│  │  ├── refs/heads/ (分支指针)             │   │
│  │  │   ├── main → commit A                │   │
│  │  │   ├── feature/login → commit B      │   │
│  │  │   └── fix/bug → commit C            │   │
│  │  └── HEAD                              │   │
│  └─────────────────────────────────────────┘   │
│                                                  │
│  Worktree 1: ./ (main 分支的工作文件)          │
│  Worktree 2: ../feature-login/ (feature 分支)   │
│  Worktree 3: ../fix-bug/ (fix 分支)             │
└─────────────────────────────────────────────────┘
```

## 核心使用场景

### 1. 并行开发

**需求**：同时开发多个 feature 或处理多个 PR，无需频繁切换分支

```bash
# 创建第一个功能分支
git checkout -b feature/login

# 创建第二个功能分支（不切换当前工作区）
git branch feature/payment

# 为第二个分支创建独立工作树
git worktree add ../feature-payment feature/payment

# 进入新工作区开发
cd ../feature-payment
```

### 2. 紧急修复

**需求**：在当前 feature 未完成时，需要紧急修复 main 分支的 bug

```bash
# 当前状态：在 feature/login 分支开发中，有未提交的修改

# 不保存当前修改，直接为 main 分支创建工作树
git worktree add ../main-hotfix main

# 进入 hotfix 工作区
cd ../main-hotfix

# 创建修复分支并修复
git checkout -b fix/critical-bug
# ... 修复 bug ...
git commit -m "Fix critical bug"

# 合并回 main
git checkout main
git merge fix/critical-bug

# 清理工作树
cd ..
git worktree remove main-hotfix
git branch -d fix/critical-bug
```

### 3. 代码审查

**需求**：同时审查多个 PR，无需反复 checkout

```bash
# 审查 PR #123 和 #456
git worktree add ../review-pr-123 origin/pr/123
git worktree add ../review-pr-456 origin/pr/456

# 并行审查
cd ../review-pr-123  # 审查第一个 PR
cd ../review-pr-456  # 审查第二个 PR

# 审查完成后清理
git worktree remove ../review-pr-123
git worktree remove ../review-pr-456
```

### 4. CI/CD 多环境

**需求**：为不同环境部署准备独立工作区

```bash
git worktree add ../deploy-dev dev
git worktree add ../deploy-staging staging
git worktree add ../deploy-prod main
```

## 最佳实践

### 1. 命名规范

```bash
# ✅ 好的命名：清晰表达用途
git worktree add ../feature-login feature/login
git worktree add ../fix-auth-bug fix/auth-bug
git worktree add ../review-pr-123 review/pr-123
git worktree add ../release-2.0 release/v2.0

# ❌ 避免的命名：模糊不清
git worktree add ../worktree1 temp-branch
git worktree add ../temp123 feature/some-feature
```

### 2. 目录结构组织

```bash
# 推荐结构：与主仓库同级
project/
├── main/              # 主工作区
├── feature-login/     # 功能分支工作树
├── fix-bug-456/       # 修复分支工作树
└── review-pr-789/     # 代码审查工作树

# 创建命令示例
cd project/main
git worktree add ../feature-login feature/login
```

### 3. 生命周期管理

```bash
# 创建
git worktree add ../feature-login feature/login

# 列出所有工作树
git worktree list

# 详细信息
git worktree list --porcelain

# 清理：删除已合并的分支工作树
git worktree remove ../feature-login
git branch -d feature/login

# 清理孤儿工作树（分支已删除但工作树残留）
git worktree prune
```

### 4. 与 .gitignore 配合

```bash
# 在主仓库的 .gitignore 中添加
# 防止误提交工作树目录
../feature-*/
../fix-*/
../review-*/
../deploy-*
```

### 5. 自动化清理脚本

```bash
#!/bin/bash
# clean-worktrees.sh - 定期清理无用工作树

for worktree_path in $(git worktree list --porcelain | grep worktree | awk '{print $2}'); do
    branch_name=$(git worktree list --porcelain | grep branch | awk '{print $2}')

    # 检查分支是否已合并到 main
    if git branch --merged main | grep -q "$branch_name"; then
        echo "Removing merged branch: $branch_name"
        git worktree remove "$worktree_path"
        git branch -d "$branch_name"
    fi
done
```

### 6. 查看分支与工作树关系

```bash
# 列出所有分支（不管是否在工作树中）
git branch -a

# 列出所有工作树及其关联分支
git worktree list

# 查看某个分支在哪个工作树中
git worktree list | grep "feature/login"

# 查找所有未关联工作树的分支
comm -23 <(git branch | sort) <(git worktree list | grep "refs/heads" | awk '{print $3}' | sort)
```

## 配合使用流程

### 场景 1：完整开发 → 修复 → 清理流程

```bash
# ========== 阶段 1：正常开发 ==========
git checkout -b feature/new-ui
# (开发中，有未提交的修改...)

# ========== 阶段 2：接到紧急修复任务 ==========
git worktree add ../hotfix main
cd ../hotfix
git checkout -b fix/urgent-bug
# (修复 bug，提交)
git commit -am "Fix urgent bug"

# 合并回 main
git checkout main
git merge fix/urgent-bug

# ========== 阶段 3：回到原功能开发 ==========
cd ../main  # 回到主工作区
# (继续 feature/new-ui 开发，修改保持不变)

# ========== 阶段 4：功能完成 ==========
git commit -am "Complete new UI feature"

# ========== 阶段 5：清理 ==========
git worktree remove ../hotfix
git branch -d fix/urgent-bug
```

### 场景 2：多 PR 并行审查

```bash
# 创建多个 review 工作树
for pr in 123 456 789; do
    git worktree add "../review-pr-$pr" "origin/pr/$pr"
done

# 并行审查
cd ../review-pr-123
# 审查 PR #123...

cd ../review-pr-456
# 审查 PR #456...

# 批量清理
git worktree remove ../review-pr-123
git worktree remove ../review-pr-456
git worktree remove ../review-pr-789
```

## 常见陷阱与规避

| 问题 | 规避方法 |
|------|----------|
| **忘记删除工作树** | 设置定期清理脚本，或使用 `git worktree prune` 清理孤儿工作树 |
| **路径冲突** | 统一使用 `../prefix-` 命名规范 |
| **分支删除但工作树残留** | `git worktree prune` 清理孤儿工作树 |
| **IDE 配置冲突** | 每个 worktree 使用独立的 IDE workspace |
| **同一分支多个工作树** | 避免此情况，会导致文件冲突和状态混乱 |

## 高级技巧

### 1. 从指定 commit 创建工作树

```bash
git worktree add ../hotfix abc1234  # abc1234 是 commit hash
```

### 2. 强制删除工作树

```bash
git worktree remove -f ../feature-login
```

### 3. 移动工作树

```bash
git worktree move ../old-path ../new-path
```

### 4. 锁定工作树（防止误删）

```bash
git worktree lock ../feature-login
git worktree unlock ../feature-login
```

### 5. 查看工作树详细信息

```bash
git worktree list --porcelain
```

输出示例：
```
worktree /Users/user/project
HEAD 6b120d1
branch refs/heads/main
detached

worktree /Users/user/feature-login
HEAD 6b120d1
branch refs/heads/feature/login
```

## 适用场景决策树

```
需要同时工作在不同分支？
├─ 是 → 需要暂存未提交的切换？
│   ├─ 是 → git stash + checkout（简单场景）
│   └─ 否 → git worktree add（推荐）
└─ 否 → 直接 checkout

多个任务需要频繁切换？
├─ 是 → 使用 worktree 并行工作
└─ 否 → 直接 checkout
```

## 常见问题（FAQ）

**Q: 在 worktree A 修改，worktree B 能看到吗？**

A: 不能。每个 worktree 的文件是独立的，需要通过 `git push`/`git pull` 同步。

**Q: 删除 worktree 会删除分支吗？**

A: 不会。`git worktree remove` 只删除工作目录，分支需要手动 `git branch -d` 删除。

**Q: 可以在同一个分支创建多个 worktree 吗？**

A: 可以，但不推荐。会导致文件冲突和状态混乱。

**Q: worktree 会占用额外磁盘空间吗？**

A: 会的。每个 worktree 包含完整的工作文件副本，但共享同一个 `.git/` 对象数据库。

**Q: 如何避免 worktree 分支冲突？**

A:
1. 遵循一对一原则：一个 worktree 对应一个分支
2. 使用 `git worktree list` 查看当前状态
3. 定期清理已完成的工作树

**Q: worktree 和 git clone 有什么区别？**

A:
- `worktree`: 共享同一个 git 仓库，`.git/` 不重复，节省空间
- `clone`: 创建完整的 git 仓库副本，`.git/` 完全独立

## 常用命令速查表

```bash
# 创建工作树
git worktree add <路径> [分支名]
git worktree add ../feature-login feature/login

# 列出所有工作树
git worktree list
git worktree list --porcelain

# 删除工作树
git worktree remove <路径>
git worktree remove -f ../feature-login  # 强制删除

# 移动工作树
git worktree move <旧路径> <新路径>

# 清理孤儿工作树
git worktree prune

# 锁定/解锁工作树
git worktree lock <路径>
git worktree unlock <路径>

# 查找分支所在工作树
git worktree list | grep "分支名"
```

## 工作流建议

### 开发团队最佳实践

1. **功能开发**
   - 每个 feature 分支使用独立 worktree
   - 完成后立即清理

2. **代码审查**
   - 为每个 PR 创建临时 worktree
   - 审查通过后清理

3. **热修复**
   - 为 hotfix 创建临时 worktree
   - 修复完成后清理

4. **环境部署**
   - 为不同环境（dev/staging/prod）创建持久 worktree
   - 避免频繁创建删除

## 总结

**Git Worktree 核心优势**：
- ✅ 并行开发，无需频繁切换分支
- ✅ 无需暂存未提交修改
- ✅ 共享 git history，节省磁盘空间
- ✅ 独立工作区，互不干扰

**关键原则**：
1. 遵循一对一：一个 worktree 对应一个分支
2. 统一命名规范
3. 及时清理无用工作树
4. 避免同一分支多个 worktree

**适用场景**：
- 并行开发多个功能
- 紧急修复 bug
- 代码审查多个 PR
- 多环境部署准备
