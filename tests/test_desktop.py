from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qmemory.desktop import MainWindow, MemoryDialog
from qmemory.service import MemoryService


@pytest.fixture(scope="module")
def qt_app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


def test_desktop_browse_confirm_and_sync(qt_app, tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    sync = tmp_path / "sync"
    project.mkdir()
    service = MemoryService(home, device_id="desktop-test")
    proposed = service.remember(
        project, "decision", "Desktop memory starts as proposed", confirmed=False
    )
    settings = QSettings(str(tmp_path / "desktop.ini"), QSettings.Format.IniFormat)
    settings.setValue("sync_path", str(sync))
    window = MainWindow(service=service, settings=settings, initial_project=project)
    window.show()
    qt_app.processEvents()

    assert window.memory_list.count() == 1
    assert "1 候选" in window.pipeline_status.text()
    assert not window.auto_extract.isChecked()
    assert window.auto_extract.text() == "自动提炼"
    window.workspace_tabs.setCurrentIndex(1)
    assert window.confirm_button.isEnabled()
    service.confirm(project, proposed["memory_id"])
    window.refresh_all()
    assert not window.confirm_button.isEnabled()
    assert "Desktop memory" in window.detail.toPlainText()

    window.auto_sync()
    for _ in range(300):
        if (sync / "desktop-test.jsonl").exists():
            break
        QTest.qWait(10)
    for _ in range(300):
        if not window.sync_busy:
            break
        QTest.qWait(10)
    assert (sync / "desktop-test.jsonl").exists()
    assert "同步成功" in window.sync_path_label.text()

    window.allow_close = True
    window.close()


def test_desktop_shows_one_task_row_and_chat_detail(
    qt_app, tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    service = MemoryService(tmp_path / "home", device_id="task-view-test")
    task = {
        "task_id": "root-task",
        "title": "把 Agent 会话收入本地档案",
        "updated_at": "2026-08-13T10:00:00Z",
        "project_root": str(project),
        "sources": ["codex", "claude-code"],
        "source_copy_count": 2,
        "subagent_count": 3,
        "readable_message_count": 2,
        "raw_message_count": 12,
        "merge_reason": "同一任务的跨 Agent 会话已合并",
        "primary_conversation_id": "primary",
    }
    monkeypatch.setattr(service, "tasks", lambda *args, **kwargs: [task])
    monkeypatch.setattr(
        service,
        "task",
        lambda *args, **kwargs: {
            **task,
            "primary_conversation": {
                "messages": [
                    {"role": "user", "sequence": 1, "text": "请完整保留原始会话"},
                    {"role": "assistant", "sequence": 2, "text": "已归档，并折叠重复来源"},
                ]
            },
        },
    )
    settings = QSettings(str(tmp_path / "desktop.ini"), QSettings.Format.IniFormat)
    window = MainWindow(service=service, settings=settings, initial_project=project)
    qt_app.processEvents()

    assert window.workspace_tabs.tabText(0) == "任务档案"
    assert window.conversation_list.count() == 1
    assert window.selected_conversation_id() == "root-task"
    assert "2 轮对话" in window.pipeline_status.text()
    assert "完整保留原始会话" in window.detail.toPlainText()
    assert "Codex + Claude Code" in window.detail.toPlainText()

    window.allow_close = True
    window.close()


def test_desktop_exposes_every_task_member_and_warns_on_low_primary_coverage(
    qt_app, tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    service = MemoryService(tmp_path / "home", device_id="coverage-test")
    task = {
        "task_id": "root-task",
        "title": "跨 Agent 任务",
        "updated_at": "2026-08-13T10:00:00Z",
        "project_root": str(project),
        "sources": ["codex", "claude-code"],
        "source_copy_count": 2,
        "subagent_count": 1,
        "readable_message_count": 2,
        "unique_readable_message_count": 5,
        "raw_message_count": 14,
        "merge_reason": "来源与执行分支已归入同一任务",
        "primary_conversation_id": "primary",
        "primary_coverage": 0.4,
        "has_content_gap": True,
    }
    details = {
        **task,
        "primary_conversation": {
            "title": "主会话",
            "messages": [{"role": "user", "sequence": 1, "text": "主会话内容"}],
        },
        "source_copies": [
            {"conversation_id": "primary", "source_kind": "codex", "readable_message_count": 1},
            {"conversation_id": "claude", "source_kind": "claude-code", "readable_message_count": 2},
        ],
        "subagents": [
            {"conversation_id": "branch", "source_kind": "codex", "agent_nickname": "reader", "readable_message_count": 2},
        ],
    }
    monkeypatch.setattr(service, "tasks", lambda *args, **kwargs: [task])
    monkeypatch.setattr(service, "task", lambda *args, **kwargs: details)
    monkeypatch.setattr(
        service,
        "conversation",
        lambda _project, conversation_id, **kwargs: {
            "title": conversation_id,
            "messages": [
                {"role": "assistant", "sequence": 1, "text": "%s 独有内容" % conversation_id}
            ],
        },
    )
    settings = QSettings(str(tmp_path / "desktop.ini"), QSettings.Format.IniFormat)
    window = MainWindow(service=service, settings=settings, initial_project=project)
    qt_app.processEvents()

    assert window.task_source_selector.count() == 3
    assert "完整内容分散在多个来源" in window.detail.toPlainText()
    assert "40%" in window.detail.toPlainText()
    branch_index = window.task_source_selector.findData("branch")
    assert "执行分支" in window.task_source_selector.itemText(branch_index)
    window.task_source_selector.setCurrentIndex(branch_index)
    qt_app.processEvents()
    assert "branch 独有内容" in window.detail.toPlainText()

    window.allow_close = True
    window.close()


def test_desktop_remembers_project_selection(qt_app, tmp_path: Path) -> None:
    project = tmp_path / "remembered-project"
    project.mkdir()
    service = MemoryService(tmp_path / "home", device_id="settings-test")
    settings_path = tmp_path / "desktop.ini"
    first_settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    first = MainWindow(service=service, settings=first_settings, initial_project=project)
    first.allow_close = True
    first.close()

    second_settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    second = MainWindow(service=service, settings=second_settings)
    assert second.project_path == project.resolve()
    second.allow_close = True
    second.close()


def test_workbench_without_project_has_unambiguous_empty_state(qt_app, tmp_path: Path) -> None:
    service = MemoryService(tmp_path / "home", device_id="empty-state-test")
    discovered = tmp_path / "discovered-project"
    discovered.mkdir()
    service.register_project(discovered)
    settings = QSettings(str(tmp_path / "desktop.ini"), QSettings.Format.IniFormat)

    window = MainWindow(service=service, settings=settings)
    window.resize(980, 650)
    qt_app.processEvents()

    assert window.project_path is None
    assert window.project_selector.currentText() == "选择项目…"
    assert "先选择一个项目" in window.detail.toPlainText()
    assert window.memory_actions.isHidden()
    assert window.sync_box.isHidden()
    assert window.detail.horizontalScrollBar().maximum() == 0

    window.allow_close = True
    window.close()


def test_memory_dialog_is_simple_and_confirmed_by_default(qt_app) -> None:
    dialog = MemoryDialog(None)
    assert dialog.windowTitle() == "记住一件事"
    assert dialog.confirmed.isChecked()
    assert not dialog.subject.isVisible()
    dialog.advanced.setChecked(True)
    assert not dialog.subject.isHidden()
    assert dialog.holder.text() == "我"
    assert dialog.subject.text() == "当前项目"
    dialog.close()


def test_setup_promotes_agent_protocol_after_codex_connection(
    qt_app, tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    service = MemoryService(tmp_path / "home", device_id="protocol-test")
    settings = QSettings(str(tmp_path / "desktop.ini"), QSettings.Format.IniFormat)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(
        "qmemory.desktop.diagnose_codex_connection",
        lambda codex, expected: {"state": "connected"},
    )
    monkeypatch.setattr(
        "qmemory.desktop.diagnose_claude_connection",
        lambda claude, expected: {"state": "connected"},
    )
    window = MainWindow(service=service, settings=settings, initial_project=project)
    qt_app.processEvents()

    assert window.setup_action.property("setupAction") == "protocol"
    assert window.setup_action.text() == "启用自动记忆"

    window.allow_close = True
    window.close()


def test_auto_extract_setting_is_restored_before_ui_build(qt_app, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    service = MemoryService(tmp_path / "home", device_id="auto-extract-test")
    settings = QSettings(str(tmp_path / "desktop.ini"), QSettings.Format.IniFormat)
    settings.setValue("auto_extract_enabled", True)
    window = MainWindow(service=service, settings=settings, initial_project=project)
    assert window.auto_extract.isChecked()
    window.allow_close = True
    window.close()


def test_workbench_navigation_and_l0_l3_progressive_disclosure(
    qt_app, tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    service = MemoryService(tmp_path / "home", device_id="workbench-test")
    service.remember(
        project,
        "decision",
        "原始会话必须先于派生结论保留。",
        confirmed=False,
        source_ref="codex://primary#message-1",
    )
    task = {
        "task_id": "task",
        "title": "重新设计 QMemory 记忆界面",
        "updated_at": "2026-08-15T10:00:00Z",
        "project_root": str(project),
        "sources": ["codex"],
        "source_copy_count": 1,
        "subagent_count": 0,
        "readable_message_count": 2,
        "unique_readable_message_count": 2,
        "raw_message_count": 2,
        "merge_reason": "原始会话完整保留",
        "primary_conversation_id": "primary",
        "primary_coverage": 1.0,
        "has_content_gap": False,
    }
    detail = {
        **task,
        "primary_conversation": {
            "messages": [
                {"role": "user", "sequence": 1, "text": "先保留原始对话"},
                {"role": "assistant", "sequence": 2, "text": "再形成记忆"},
            ]
        },
        "source_copies": [
            {"conversation_id": "primary", "source_kind": "codex", "readable_message_count": 2}
        ],
        "subagents": [],
    }
    monkeypatch.setattr(service, "tasks", lambda *args, **kwargs: [task])
    monkeypatch.setattr(service, "task", lambda *args, **kwargs: detail)
    settings = QSettings(str(tmp_path / "desktop.ini"), QSettings.Format.IniFormat)
    window = MainWindow(service=service, settings=settings, initial_project=project)
    window.show()
    qt_app.processEvents()

    assert window.workspace_title.text() == "工作台"
    assert window.nav_buttons[0].property("selected") is True
    assert window.conversation_mode_stack.currentIndex() == 0
    assert window.task_view_title.text() == "记忆图谱"
    assert "project" in window.memory_graph.node_buttons
    assert "task:task" in window.memory_graph.node_buttons
    assert "source:codex" in window.memory_graph.node_buttons
    assert all("layer:%d" % index in window.memory_graph.node_buttons for index in range(4))
    assert [button.text() for button in window.layer_buttons] == [
        "L0 原始会话",
        "L1 原子记忆",
        "L2 项目场景",
        "L3 稳定内核",
    ]
    assert "先保留原始对话" in window.detail.toPlainText()

    window._show_task_layer(1)
    assert "原始会话必须先于派生结论保留" in window.detail.toPlainText()
    assert "codex://primary#message-1" in window.detail.toPlainText()
    window._show_task_layer(2)
    assert "尚未启用 MemoryCore" in window.detail.toPlainText()
    window._show_task_layer(3)
    assert "尚未启用 MemoryCore" in window.detail.toPlainText()
    assert window.memory_graph.node_buttons["layer:3"].property("selected") is True

    window._set_conversation_view(1)
    assert window.conversation_mode_stack.currentIndex() == 1
    assert window.task_view_title.text() == "任务档案"
    window._set_conversation_view(0)
    assert window.conversation_mode_stack.currentIndex() == 0

    window._switch_workspace(1)
    assert window.workspace_title.text() == "候选记忆"
    assert window.nav_buttons[1].property("selected") is True
    assert window.layer_switch.isHidden()
    window._switch_workspace(2)
    assert window.workspace_title.text() == "正式记忆"

    window.resize(980, 650)
    qt_app.processEvents()
    assert window.conversation_mode_stack.currentIndex() == 1
    assert not window.graph_view_button.isEnabled()
    assert window.detail.horizontalScrollBar().maximum() == 0
    window.resize(1440, 900)
    qt_app.processEvents()
    assert window.conversation_mode_stack.currentIndex() == 0
    assert window.graph_view_button.isEnabled()

    window.allow_close = True
    window.close()
