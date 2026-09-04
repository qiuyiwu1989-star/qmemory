from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

import qmemory.desktop as desktop
from qmemory.desktop import MainWindow, ProjectLibraryDialog
from qmemory.service import MemoryService


def write_rollout(path: Path, conversation_id: str, project: Path, messages: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "timestamp": "2026-08-13T08:00:00Z",
            "type": "session_meta",
            "payload": {"id": conversation_id, "cwd": str(project)},
        }
    ]
    for index, (role, text) in enumerate(messages, 1):
        events.append(
            {
                "timestamp": "2026-08-13T08:%02d:00Z" % index,
                "type": "event_msg",
                "payload": {
                    "type": "user_message" if role == "user" else "agent_message",
                    "message": text,
                },
            }
        )
    path.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")


def write_claude(path: Path, conversation_id: str, project: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "user",
            "sessionId": conversation_id,
            "cwd": str(project),
            "timestamp": "2026-08-13T08:10:00Z",
            "isSidechain": False,
            "message": {"role": "user", "content": "Claude Code 的原始对话也要进入同一个记忆证据层。"},
        },
        {
            "type": "ai-title",
            "sessionId": conversation_id,
            "aiTitle": "将 Claude Code 纳入统一会话档案",
        },
        {
            "type": "assistant",
            "sessionId": conversation_id,
            "cwd": str(project),
            "timestamp": "2026-08-13T08:11:00Z",
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "会按项目归类，并与 Codex 会话统一检索。"}],
            },
        },
    ]
    path.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")


def main() -> None:
    application = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="qmemory-preview-") as temporary_value:
        temporary = Path(temporary_value)
        project = temporary / "qmemory"
        project.mkdir()
        related = temporary / "qmemory-worktree"
        related.mkdir()
        for path in (project, related):
            subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/qiuyiwu/qmemory.git"],
                cwd=path, check=True,
            )
        codex_home = temporary / "codex"
        write_rollout(
            codex_home / "sessions" / "rollout-archive.jsonl",
            "preview-archive",
            project,
            [
                ("user", "第一步最重要的是，把原来我们跟 Agent 对话的内容都先同步过来，因为原始会话内容很重要。"),
                ("assistant", "同意。QMemory 将先无损归档原始会话，再从可追溯证据中提取候选洞察。"),
                ("user", "候选洞察需要我确认后，才进入给 Agent 默认读取的当前记忆。"),
            ],
        )
        write_rollout(
            codex_home / "sessions" / "rollout-design.jsonl",
            "preview-design",
            project,
            [
                ("user", "界面参考 SkillOps 设计系统，减少卡片、阴影和同时出现的信息层级。"),
                ("assistant", "界面拆为会话档案、候选洞察、当前记忆三个工作区。"),
            ],
        )
        write_rollout(
            codex_home / "sessions" / "rollout-related.jsonl",
            "preview-related",
            related,
            [
                ("user", "同一个 Git remote 的 worktree 和分支应该自动归入同一个项目。"),
                ("assistant", "项目库会保留多个相关目录，并自动选择主目录。"),
            ],
        )
        claude_home = temporary / "claude"
        write_claude(
            claude_home / "projects" / "-qmemory" / "preview-claude.jsonl",
            "preview-claude",
            project,
        )
        service = MemoryService(temporary / "home", device_id="preview-device")
        service.conversation_sources_sync(codex_home, claude_home)
        service.remember(
            project,
            "decision",
            "QMemory 采用原始会话证据层、候选判断层和 Agent 上下文层三层结构。",
            subject="记忆架构",
            holder="user",
            confirmed=False,
            source_ref="codex://preview-archive#message-1",
        )
        service.remember(
            project,
            "constraint",
            "原始会话只保存在本机私有目录，不进入当前明文设备同步目录。",
            subject="原始会话安全边界",
            holder="agent",
            confirmed=True,
            source_ref="codex://preview-archive#message-2",
        )
        settings = QSettings(str(temporary / "desktop.ini"), QSettings.Format.IniFormat)
        desktop.diagnose_codex_connection = lambda codex, expected: {"state": "connected"}
        desktop.diagnose_claude_connection = lambda claude, expected: {"state": "connected"}
        desktop.inspect_protocol = lambda path: {"state": "current"}
        window = MainWindow(service=service, settings=settings, initial_project=project)
        window.resize(1440, 900)
        window.show()
        application.processEvents()
        output = Path(__file__).resolve().parents[1] / "artifacts" / "qmemory-v010-preview.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(output))
        library = ProjectLibraryDialog(service, window, window.current_project_id)
        library.show()
        application.processEvents()
        library_output = output.with_name("qmemory-v010-project-library.png")
        library.grab().save(str(library_output))
        library.close()
        window.allow_close = True
        window.close()
        print(output)
        print(library_output)


if __name__ == "__main__":
    main()
