# Codex History Exporter

将本机 Codex Desktop / CLI 的主会话导出为按项目归类的 Markdown 文件。只保留用户消息和 Codex 最终回复，不导出 sub-agent、系统提示、推理、工具调用或命令日志。

## 要求

- Python 3.9 或更高版本
- 不需要安装第三方包

## 使用

在项目根目录运行：

```powershell
python .\export_codex_history.py
```

脚本按以下顺序寻找 Codex 数据目录：

1. 命令行参数 `--codex-home`
2. 环境变量 `CODEX_HOME`
3. 当前用户的 `~\.codex`

在另一台电脑或导出备份副本时，可以显式指定：

```powershell
python .\export_codex_history.py --codex-home "D:\Backup\.codex"
```

无论从哪个工作目录启动，结果都固定写入本项目根目录的 `.output`：

```text
.output/
├── chat/
│   └── yyyyMMddHHmmss__会话名.md
├── 项目1/
│   └── yyyyMMddHHmmss__会话名.md
└── 项目2/
    └── yyyyMMddHHmmss__会话名.md
```

每次执行都会完整重建 `.output`。脚本先在 staging 目录生成全部文件，成功后才替换现有输出；生成阶段失败时会保留上一次的 `.output`。如果 Windows 文件监视器锁住 `.output` 根目录、使目录无法整体改名，脚本会自动改用带备份和回滚的目录内替换。

## 项目判断规则

脚本依次使用：

1. Codex Desktop 保存的明确项目分配
2. Codex 保存的无项目会话标记（归入 `chat`）
3. `cwd` 与 Codex 本地项目根目录的最长匹配
4. `cwd` 的末级目录名
5. 无法判断或 `cwd` 是用户主目录时归入 `chat`

## 测试

```powershell
python -m unittest discover -s tests -v
```
