# Codex History Exporter

将本机 Codex Desktop / CLI 的主会话导出为按项目归类的 Markdown 文件。只保留用户消息和 Codex 最终回复，不导出 sub-agent、系统提示、推理、工具调用或命令日志。

## 要求

- Python 3.9 或更高版本
- 不需要安装第三方包
- 支持 Windows、macOS 与 Linux

## 使用

在项目根目录运行：

```sh
python ./export_codex_history.py
```

脚本按以下顺序寻找 Codex 数据目录：

1. 命令行参数 `--codex-home`
2. 环境变量 `CODEX_HOME`
3. 当前用户的 `~/.codex`

在另一台电脑或导出备份副本时，可以显式指定：

```sh
python ./export_codex_history.py --codex-home ~/Backup/.codex
```

默认会导出所有主会话。若只想导出一个会话，支持以下两种筛选值：

1. 完整的 session ID
2. Codex 的 **Copy deep link** 结果

通过 session ID 导出：

```powershell
python ./export_codex_history.py "019c1234-5678-7abc-9def-0123456789ab"
```

通过 deep link 导出：

```powershell
python ./export_codex_history.py "codex://threads/019c1234-5678-7abc-9def-0123456789ab"
```

两种值也都支持 `--session-id` 具名参数写法：

```powershell
python ./export_codex_history.py --session-id "019c1234-5678-7abc-9def-0123456789ab"
python ./export_codex_history.py --session-id "codex://threads/019c1234-5678-7abc-9def-0123456789ab"
```

对于 session ID，脚本会直接按 session 元数据中的 ID 进行完全匹配；对于 deep link，脚本会先从 `codex://threads/<session-id>` 中提取 session ID，再执行相同的完全匹配。单个会话导出时不会生成全量导出使用的 `projects.toml`。deep link 格式无效、找不到该 ID、会话属于 sub-agent，或会话没有可导出的消息时，脚本会返回错误且不会替换已有输出。

批量导出默认包含归档对话，与此前行为一致。如果只需要当前未归档的对话，可以使用：

```sh
python ./export_codex_history.py --ignore-archived
```

`--ignore-archived` 仅用于批量导出，不能与位置参数或 `--session-id` 的单会话筛选同时使用。归档状态优先采用 Codex 状态数据库；数据库没有相应记录时，再根据 rollout 是否位于 `archived_sessions` 判断。

如果曾编辑用户消息并生成多个版本，导出结果只保留最终有效分支；被后续编辑回滚的旧问题、旧回答及其后续分支不会写入 Markdown。

默认结果写入运行命令时所在目录的 `output` 子目录：

```text
output/
├── projects.toml
├── chat/
│   └── yyyyMMddHHmmss__会话名.md
├── 项目1/
│   └── yyyyMMddHHmmss__会话名.md
└── 项目2/
    └── yyyyMMddHHmmss__会话名.md
```

使用 `-o` 或 `--output` 可以指定其他输出目录；相对路径以当前工作目录为基准：

```sh
python ./export_codex_history.py --output ~/Backup/codex-history
python ./export_codex_history.py -o ./history
```

如果输出目录已经存在且非空，脚本会在覆盖前询问。输入 `y` 或 `yes` 才会继续；也可以使用 `-f` / `--force` 跳过询问并直接覆盖：

```sh
python ./export_codex_history.py -o ./history -f
```

`projects.toml` 记录各项目对应的工作目录。只有一个目录时使用 `Path`：

```toml
["项目1"]
Path = '''/home/user/Workspace/项目1'''
```

如果同名项目关联到多个目录，则使用 `Paths` 数组。`chat` 可能包含多个互不相关的工作目录，因此不会写入此索引。

确认覆盖后，每次执行都会完整重建目标目录。脚本先在 staging 目录生成全部文件，成功后才替换现有输出；生成阶段失败时会保留上一次的结果。如果输出根目录被占用而无法整体改名，脚本会自动改用带备份和回滚的目录内替换。

脚本会同时扫描 `sessions` 与 `archived_sessions`。如果同一个 Thread ID 在两个目录中留下了可见对话完全相同的 rollout，只导出一份，并优先采用 Codex 状态数据库记录的归档状态；如果同一 Thread ID 的可见对话不同，则保留两份，避免静默丢失历史分支。使用 `--ignore-archived` 时，去重后的归档对话不会写入输出。命令行统计中的 `archived conversations ignored` 会显示被忽略的归档对话数量，`duplicate rollouts excluded` 会显示被排除的相同副本数量；`empty sessions` 与 `invalid rollouts` 会进一步说明 `skipped` 分别来自无可见消息的会话还是无有效会话元数据的记录。

## 项目判断规则

脚本依次使用：

1. Codex Desktop 保存的明确项目分配
2. Codex 保存的无项目会话标记（归入 `chat`）
3. `cwd` 与 Codex 本地项目根目录的最长匹配
4. `cwd` 的末级目录名
5. 无法判断或 `cwd` 是用户主目录时归入 `chat`

## 测试

```sh
python -m unittest discover -s tests -v
```
