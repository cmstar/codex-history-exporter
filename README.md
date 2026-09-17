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

### 按创建时间或最后聊天时间筛选

批量导出支持四个时间筛选参数：

| 参数 | 含义 |
| --- | --- |
| `--created-since TIME` | 创建时间的下限 |
| `--created-until TIME` | 创建时间的上限 |
| `--last-chat-since TIME` | 最后聊天时间的下限 |
| `--last-chat-until TIME` | 最后聊天时间的上限 |

`TIME` 只接受以下三种纯数字格式，均按运行脚本的电脑本地时区解释：

| 格式 | 示例 | 精度 |
| --- | --- | --- |
| `yyyyMMdd` | `20260906` | 日 |
| `yyyyMMddHHmm` | `202609061430` | 分钟 |
| `yyyyMMddHHmmss` | `20260906143025` | 秒 |

下限包含指定时刻；上限包含指定的整天、整分钟或整秒。例如 `--last-chat-until 20260906` 包含当天全部消息时间，`--last-chat-until 202609061430` 包含到 `14:30:59.999999`，`--last-chat-until 20260906143025` 包含到 `14:30:25.999999`。

四个参数都可以单独使用，也可以混用不同精度。多个条件之间是“同时满足”的关系；同一组的起止范围不能颠倒。没有传入时间参数时，保持全量导出。可以与 `--ignore-archived` 组合使用。

```powershell
# 导出最后聊天日期在 8 月 31 日至 9 月 6 日这一周内的对话
python ./export_codex_history.py --last-chat-since 20260831 --last-chat-until 20260906

# 导出 9 月 6 日 14:30 至 15:00:25（包含该秒）创建的对话
python ./export_codex_history.py --created-since 202609061430 --created-until 20260906150025

# 导出 9 月以前创建、9 月 1 日及以后仍有聊天的未归档对话
python ./export_codex_history.py --created-until 20260831 --last-chat-since 20260901 --ignore-archived
```

筛选单位是整个对话，命中的对话仍导出完整的有效历史，不会截取日期范围内的消息。“最后聊天时间”是最终有效分支中最后一条用户消息或 Codex 最终回复的时间，不包含工具事件或已回滚的旧消息。如果对话在 9 月 3 日聊过、9 月 8 日又继续聊，那么 `--last-chat-until 20260906` 会排除它。

创建时间来自会话元数据中的 `timestamp`。如果所需的创建时间缺失或无效，或者按最后聊天时间筛选时有可见消息缺失有效时间，脚本会跳过该对话，并在 `conversations missing filter timestamps` 中计数，不使用文件修改时间代替筛选时间。正常超出范围的对话计入 `conversations outside date filters`。

指定单个 session ID 或 deep link 时，四个时间参数全部忽略，包括其取值格式和范围校验，仍导出该会话的完整有效历史。批量导出时，非法时间或颠倒的范围会在覆盖确认前报错，保留已有输出。筛选结果为零时，成功导出空的项目索引；已有非空输出仍遵循下方的覆盖确认规则。

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

脚本会同时扫描 `sessions` 与 `archived_sessions`，同一个 Thread ID 最终只输出一个 Markdown 文件。状态数据库中的 `rollout_path` 优先决定当前日志；数据库没有该字段或记录时，通过日志的历史继承关系寻找唯一的最终版本，相同副本只保留一份。

对于没有历史继承信息且数据库未指定当前路径的旧日志，如果较短副本是完整副本的逐字节前缀，则使用完整副本；存在内容分歧时仍报错，不按文件大小或修改时间猜测当前版本。

对于含有 `history_base` 的日志，脚本会同时校验基础日志的 `end_ordinal_exclusive`（记录边界，不包含该记录）和 `end_byte_offset`（字节边界），恢复边界之前的历史，再接入当前日志。支持通过会话 ID 或文件名中的分段 ID 查找基础日志；日志带有 `ordinal` 时按跨分段连续编号校验，字节边界仍相对于该分段文件，没有 `ordinal` 的旧日志按行数校验。支持递归恢复多段历史；消息提取、回滚处理及日期筛选均在恢复之后进行。同一对话的创建时间沿用基础历史的创建时间，最后聊天时间取最终有效消息的时间。例如，编辑提问后生成一份新日志，导出会保留编辑前的有效前文和修改后的提问、回答，排除被替换的旧分支。

基础日志缺失、边界不匹配或存在无法确定的多个分支时，脚本会报错并保留已有输出，不会拼接猜测出的历史，也不会将一个对话拆成多个文件。复制到其他电脑的数据目录可通过唯一同名日志匹配数据库中已失效的原路径。

使用 `--ignore-archived` 时，归档状态仍优先采用状态数据库，恢复后的归档对话不会写入输出。命令行统计中的 `archived conversations ignored` 显示被忽略的归档对话数量，`duplicate rollouts excluded` 显示按可见内容排除的相同副本数量，`historical rollouts consolidated` 显示通过当前版本选择或历史恢复收拢的额外日志数量；`empty sessions` 与 `invalid rollouts` 说明 `skipped` 分别来自无可见消息的会话还是无有效会话元数据的记录。

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
