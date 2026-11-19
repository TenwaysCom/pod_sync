# 通过 Issue 跟踪需求改动

## 建立需求记录

1. 在 [仓库 Issues](https://github.com/TenwaysCom/pod_sync/issues) 搜索已有需求。
2. 如果已有同一需求的 Issue，继续使用原 Issue。独立需求另建 Issue，并互相链接。
3. 如果没有对应 Issue，在修改代码前通过 [需求改动模板](https://github.com/TenwaysCom/pod_sync/issues/new?template=change_request.md) 创建 Issue。命令行建单时，按 [.github/ISSUE_TEMPLATE/change_request.md](.github/ISSUE_TEMPLATE/change_request.md) 的章节填写正文。
4. 在“问题与目标”中写出谁在什么场景下遇到问题，以及改动后的预期行为。缺陷需补充复现步骤和实际症状。
5. 在“范围与决策”中写清本次包含和不包含的内容。记录已确认的决策及原因，未决定的事项写为待确认。
6. 按“前置条件、操作、可观察结果”填写验收条件。保留尚未验证的验收项。
7. 在需求演变表中记录初始需求和日期。

如果 GitHub 不可用，先把正文保存为本地 Markdown 草稿。继续本地工作，并记录未同步状态。恢复访问后补建或更新 Issue。

不要在 Issue、提交消息或附件中写入真实配置、token、签名 URL 或客户文件。

## 记录需求演变和研发进度

1. 开发开始时，将当前阶段改为开发中。每个任务写出一个可独立演示或验证的交付结果，并附验证方法。存在依赖时写明前置 Issue。
2. 需求变化时，在需求演变表追加日期、变更内容、原因和影响。同步修改“问题与目标”和验收条件，保留旧的演变记录。
3. 验证一个交付结果后，勾选对应研发任务。用“下一步”记录后续动作。
4. 遇到阻塞时，记录阻塞原因、影响和恢复条件。
5. 在“验证与交付”中逐项记录环境、命令或页面操作、实际结果和证据链接。验证通过后勾选对应验收条件。未执行的检查写为未验证，并说明原因。

将最新状态保留在 Issue 正文。需要讨论或说明决策时，追加评论，并将结论更新到正文。

使用 GitHub CLI 时，先将完整正文写入本地文件，再更新 Issue。保留已有需求演变记录。以下命令中的 `123` 是示例编号，执行前替换为实际编号。

```bash
gh issue view 123 --repo TenwaysCom/pod_sync --json body --jq .body > /tmp/pod-sync-issue-123.md
# 编辑 /tmp/pod-sync-issue-123.md，保留历史后再执行下一条命令。
gh issue edit 123 --repo TenwaysCom/pod_sync --body-file /tmp/pod-sync-issue-123.md
```

## 关联 commit

1. 在每个相关提交的消息中写入 `Refs #<编号>`。一个提交涉及多个 Issue 时，逐个引用。
2. 提交前检查暂存文件，只提交本次需求涉及的内容。
3. 推送提交后，将完整 commit 链接添加到 Issue 的“关联 commit”中。每个链接附上一句改动说明。
4. 如果使用 PR，在 PR 正文引用 Issue，并将 PR 链接补到 Issue。需要保留部署或业务验收记录时，使用 `Refs #<编号>`。
5. 如果后续提交、变基或 squash 合并替换了提交，补充最终 commit 链接。

提交消息示例，执行前替换 `123`：

```text
fix: 修复 POD 本地链接生成

Refs #123
```

commit 链接格式为 `https://github.com/TenwaysCom/pod_sync/commit/<完整 SHA>`。只有本地提交时，将状态写为待推送，推送后再补链接。

仅在合并即满足全部验收条件时使用 `Closes #<编号>`。该关键词会在提交进入默认分支时关闭 Issue，见 [GitHub 的 Issue 关联规则](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue)。

## 补齐使用方法并交付

1. 在“使用方法”中写明环境、配置、权限、操作步骤和预期结果。涉及配置切换或数据变更时，补充回退步骤。
2. 对照验收条件逐项记录验证结果。不能验证的场景写明原因和后续处理。
3. 分别记录本地验证、合并和部署状态。需要部署的改动，记录环境和部署 commit；尚未部署时明确写为未部署。
4. 核对“关联 commit”中的链接能打开对应提交。
5. 满足全部验收条件后关闭 Issue。如果取消需求，记录原因后以未计划完成的状态关闭。

## 按 technical-writing 风格填写记录

1. 先写结果或当前状态，再写证据和下一步。
2. 使用短句、明确主语和动作。一句话表达一个意思。
3. 写出真实路径、配置键和命令。命令中的凭据使用占位符。
4. 操作说明按执行顺序编号。每个步骤写出预期结果。
5. 区分计划、已实现、已验证和已部署。将“正常”“优化完成”替换为实际行为及验证证据。
6. 发布 Issue 前替换模板中的占位内容。使用 HTML 注释中的提示填写正文，将详细操作规则保留在本指南中。

验收条件示例：

```text
在已有 POD 文件和唯一匹配出库单的环境下，执行推送后，stock.tracking 中的 POD URL 指向本地文件。
```

进度记录示例：

```text
已在测试环境验证本地 POD 链接可打开。下一步验证无匹配出库单时的处理结果。
```

模板结构参考 [Matt Pocock 的 skills](https://github.com/mattpocock/skills) 中的 `to-spec`、`to-tickets` 和 `writing-for-agents`。使用问题与目标描述用户需要，按可验证结果组织任务，并将填写规则集中在本指南中。
