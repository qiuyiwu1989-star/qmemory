from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QRect, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QSystemTrayIcon,
    QSizePolicy,
    QTextBrowser,
    QTextEdit,
    QToolBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .service import MEMORY_TYPES, MemoryService
from .agent_protocol import (
    global_agents_path,
    global_claude_path,
    inspect_protocol,
    install_protocol,
)
from .desktop_state import (
    bundle_executable,
    diagnose_claude_connection,
    diagnose_codex_connection,
    find_app_bundle,
)
from .insights import CodexInsightExtractor
from .tencent_memorycore import (
    MemoryCoreBridge,
    MemoryCoreClient,
    MemoryCoreConfig,
    MemoryCoreIdentity,
    store_memorycore_api_key,
)
from .textio import force_utf8_streams, run_text


APP_NAME = "QMemory"
ORG_NAME = "Qiuyiwu"

STATUS_LABELS = {
    "active": "有效",
    "proposed": "待确认",
    "superseded": "已替代",
    "archived": "已归档",
}
TYPE_LABELS = {
    "decision": "决定",
    "constraint": "约束",
    "environment": "环境",
    "incident": "事故",
    "status": "状态",
    "handoff": "交接",
    "fact": "事实",
}
TYPE_HELP = {
    "decision": "已经拍板、以后应继续遵守的选择",
    "constraint": "不能违反的边界、规则或前提",
    "environment": "运行环境、依赖和部署条件",
    "incident": "故障现象、根因、修复与防复发",
    "status": "项目目前进行到哪里",
    "handoff": "下一个智能体应从哪里继续",
    "fact": "需要长期保留的项目事实",
}


def app_icon(size: int = 128) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#161D2D"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, size - 4, size - 4, size * 0.24, size * 0.24)
    painter.setBrush(QColor("#6EE7B7"))
    painter.drawEllipse(size * 0.18, size * 0.20, size * 0.28, size * 0.28)
    painter.setBrush(QColor("#60A5FA"))
    painter.drawEllipse(size * 0.51, size * 0.20, size * 0.28, size * 0.28)
    painter.setBrush(QColor("#C4B5FD"))
    painter.drawEllipse(size * 0.345, size * 0.52, size * 0.31, size * 0.31)
    painter.end()
    return QIcon(pixmap)


class MemoryGraphCanvas(QWidget):
    """Accessible, clickable project/task/source/memory graph."""

    task_selected = Signal(str)
    layer_selected = Signal(int)
    source_selected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("memoryGraphCanvas")
        self.setMinimumHeight(430)
        self.setAccessibleName("项目记忆图谱")
        self.project_name = ""
        self.tasks: List[Dict[str, Any]] = []
        self.selected_task_id = ""
        self.task_detail: Dict[str, Any] = {}
        self.selected_layer = 0
        self.l1_count = 0
        self.node_buttons: Dict[str, QPushButton] = {}
        self.node_rects: Dict[str, QRect] = {}
        self.edges: List[tuple[str, str, bool]] = []

    def set_graph(
        self,
        project_name: str,
        tasks: List[Dict[str, Any]],
        selected_task_id: str = "",
        task_detail: Optional[Dict[str, Any]] = None,
        selected_layer: int = 0,
        l1_count: int = 0,
    ) -> None:
        self.project_name = project_name
        self.tasks = list(tasks)
        self.selected_task_id = selected_task_id
        self.task_detail = dict(task_detail or {})
        self.selected_layer = selected_layer
        self.l1_count = l1_count
        self._rebuild()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._rebuild()

    def _clear_nodes(self) -> None:
        for button in self.node_buttons.values():
            button.hide()
            button.deleteLater()
        self.node_buttons.clear()
        self.node_rects.clear()
        self.edges.clear()

    def _add_node(
        self,
        key: str,
        text: str,
        kind: str,
        rect: QRect,
        *,
        selected: bool = False,
        callback: Optional[Any] = None,
    ) -> None:
        button = QPushButton(text, self)
        button.setObjectName("graphNode")
        button.setProperty("nodeKind", kind)
        button.setProperty("selected", selected)
        button.setGeometry(rect)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAccessibleName(text.replace("\n", "，"))
        if callback is not None:
            button.clicked.connect(callback)
        button.show()
        self.node_buttons[key] = button
        self.node_rects[key] = rect

    def _edge(self, parent: str, child: str, active: bool = False) -> None:
        if parent in self.node_rects and child in self.node_rects:
            self.edges.append((parent, child, active))

    def _rebuild(self) -> None:
        self._clear_nodes()
        width = max(640, self.width())
        center = width // 2
        project_rect = QRect(center - 125, 22, 250, 52)
        project_label = self.project_name or "尚未选择项目"
        self._add_node(
            "project",
            "%s\n%d 项任务" % (_compact(project_label, 26), len(self.tasks)),
            "project",
            project_rect,
            selected=True,
        )
        if not self.tasks:
            self.update()
            return

        visible_tasks = list(self.tasks[:4])
        if (
            self.selected_task_id
            and all(str(task.get("task_id")) != self.selected_task_id for task in visible_tasks)
        ):
            selected = next(
                (task for task in self.tasks if str(task.get("task_id")) == self.selected_task_id),
                None,
            )
            if selected is not None:
                visible_tasks[-1:] = [selected]
        count = len(visible_tasks)
        gap = 12
        task_width = min(190, max(125, (width - 90 - gap * (count - 1)) // count))
        total_width = task_width * count + gap * (count - 1)
        task_left = max(45, center - total_width // 2)
        for index, task in enumerate(visible_tasks):
            task_id = str(task.get("task_id") or "")
            selected = task_id == self.selected_task_id
            sources = task.get("sources", [])
            source_label = " + ".join(
                "Claude" if source == "claude-code" else "Codex" for source in sources
            ) or "Agent"
            label = "%s\n%s · %d 轮" % (
                _compact(task.get("title", "未命名任务"), 14),
                source_label,
                int(task.get("unique_readable_message_count", task.get("readable_message_count", 0))),
            )
            key = "task:%s" % task_id
            self._add_node(
                key,
                label,
                "task",
                QRect(task_left + index * (task_width + gap), 102, task_width, 62),
                selected=selected,
                callback=lambda _checked=False, value=task_id: self.task_selected.emit(value),
            )
            self._edge("project", key, selected)

        selected_task_key = "task:%s" % self.selected_task_id
        detail = self.task_detail
        if not detail or selected_task_key not in self.node_rects:
            self.update()
            return

        source_copies = list(detail.get("source_copies", []))
        sources: Dict[str, List[Dict[str, Any]]] = {}
        for source in source_copies:
            sources.setdefault(str(source.get("source_kind") or "agent"), []).append(source)
        source_items = list(sources.items())[:3]
        source_width = 180
        source_gap = 28
        source_total = len(source_items) * source_width + max(0, len(source_items) - 1) * source_gap
        source_left = center - source_total // 2
        source_keys: Dict[str, str] = {}
        for index, (source_kind, members) in enumerate(source_items):
            name = "Claude Code" if source_kind == "claude-code" else "Codex"
            source_key = "source:%s" % source_kind
            source_keys[source_kind] = source_key
            primary_id = str(detail.get("primary_conversation_id") or "")
            target = next(
                (str(item.get("conversation_id")) for item in members if str(item.get("conversation_id")) == primary_id),
                str(members[0].get("conversation_id") or ""),
            )
            self._add_node(
                source_key,
                "%s\n%d 份来源" % (name, len(members)),
                "claude" if source_kind == "claude-code" else "codex",
                QRect(source_left + index * (source_width + source_gap), 202, source_width, 52),
                callback=lambda _checked=False, value=target: self.source_selected.emit(value),
            )
            self._edge(selected_task_key, source_key, True)

        subagents = list(detail.get("subagents", []))[:4]
        branch_keys: List[str] = []
        if subagents:
            branch_width = 145
            branch_gap = 12
            branch_total = len(subagents) * branch_width + (len(subagents) - 1) * branch_gap
            branch_left = center - branch_total // 2
            for index, member in enumerate(subagents):
                conversation_id = str(member.get("conversation_id") or "")
                nickname = str(member.get("agent_nickname") or "").strip() or "执行分支 %d" % (index + 1)
                key = "branch:%s" % conversation_id
                branch_keys.append(key)
                self._add_node(
                    key,
                    "%s\n%d 轮" % (
                        _compact(nickname, 14), int(member.get("readable_message_count") or 0)
                    ),
                    "branch",
                    QRect(branch_left + index * (branch_width + branch_gap), 284, branch_width, 46),
                    callback=lambda _checked=False, value=conversation_id: self.source_selected.emit(value),
                )
                parent = source_keys.get(str(member.get("source_kind") or ""), selected_task_key)
                self._edge(parent, key, False)

        layer_parent_keys = branch_keys or list(source_keys.values()) or [selected_task_key]
        layer_labels = (
            "L0 原始会话\n%d 轮唯一对话" % int(
                detail.get("unique_readable_message_count", detail.get("readable_message_count", 0))
            ),
            "L1 原子记忆\n%d 条可追溯判断" % self.l1_count,
            "L2 项目场景\n等待隔离加工",
            "L3 稳定内核\n尚未形成",
        )
        layer_gap = 10
        layer_width = min(175, max(122, (width - 100 - layer_gap * 3) // 4))
        layer_total = layer_width * 4 + layer_gap * 3
        layer_left = center - layer_total // 2
        previous = ""
        for index, label in enumerate(layer_labels):
            key = "layer:%d" % index
            self._add_node(
                key,
                label,
                "layer",
                QRect(layer_left + index * (layer_width + layer_gap), 374, layer_width, 48),
                selected=index == self.selected_layer,
                callback=lambda _checked=False, value=index: self.layer_selected.emit(value),
            )
            if index == 0:
                for parent in layer_parent_keys:
                    self._edge(parent, key, index == self.selected_layer)
            else:
                self._edge(previous, key, index == self.selected_layer)
            previous = key
        self.update()

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#FBFBFD"))
        painter.setPen(QPen(QColor("#E8EAF0"), 1, Qt.PenStyle.DotLine))
        for y in (88, 182, 270, 350):
            painter.drawLine(18, y, max(18, self.width() - 18), y)
        painter.setPen(QColor("#8A8E9C"))
        for text, y in (("项目", 45), ("任务", 121), ("Agent 来源", 221), ("执行分支", 302), ("记忆层", 394)):
            painter.drawText(22, y, text)
        for parent, child, active in self.edges:
            parent_rect = self.node_rects[parent]
            child_rect = self.node_rects[child]
            color = QColor("#7168EA" if active else "#BFC3CF")
            painter.setPen(QPen(color, 2 if active else 1.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            path = QPainterPath()
            horizontal = abs(parent_rect.center().y() - child_rect.center().y()) < 20
            if horizontal:
                start_x = parent_rect.right()
                start_y = parent_rect.center().y()
                end_x = child_rect.left()
                end_y = child_rect.center().y()
                path.moveTo(start_x, start_y)
                mid_x = start_x + (end_x - start_x) * 0.5
                path.cubicTo(mid_x, start_y, mid_x, end_y, end_x, end_y)
            else:
                start_x = parent_rect.center().x()
                start_y = parent_rect.bottom()
                end_x = child_rect.center().x()
                end_y = child_rect.top()
                path.moveTo(start_x, start_y)
                mid_y = start_y + (end_y - start_y) * 0.52
                path.cubicTo(start_x, mid_y, end_x, mid_y, end_x, end_y)
            painter.drawPath(path)
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(start_x - 3, start_y - 3, 6, 6)
            painter.drawEllipse(end_x - 3, end_y - 3, 6, 6)
        painter.end()


class MemoryCard(QWidget):
    def __init__(self, memory: Dict[str, Any]) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(5)
        meta = QHBoxLayout()
        kind = QLabel(TYPE_LABELS.get(memory["memory_type"], memory["memory_type"]))
        kind.setObjectName("typeChip")
        status = QLabel(STATUS_LABELS.get(memory["status"], memory["status"]))
        status.setObjectName("statusChip")
        status.setProperty("memoryStatus", memory["status"])
        timestamp = QLabel(_friendly_time(memory.get("as_of", "")))
        timestamp.setObjectName("muted")
        meta.addWidget(kind)
        meta.addWidget(status)
        meta.addStretch()
        meta.addWidget(timestamp)
        statement = QLabel(_compact(memory["statement"], 180))
        statement.setObjectName("cardStatement")
        statement.setWordWrap(True)
        context = QLabel("%s · %s" % (memory["holder"], memory["subject"]))
        context.setObjectName("muted")
        layout.addLayout(meta)
        layout.addWidget(statement)
        layout.addWidget(context)


class TaskCard(QWidget):
    """One visible row per user task, regardless of Agent copies or subagents."""

    def __init__(self, task: Dict[str, Any]) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(7)
        meta = QHBoxLayout()
        source_names = {
            "codex": "CODEX",
            "claude-code": "CLAUDE CODE",
        }
        sources = task.get("sources") or [task.get("source_kind")]
        source = QLabel(" + ".join(source_names.get(value, str(value).upper()) for value in sources))
        source.setObjectName("sourceChip")
        count = QLabel("%d 轮对话" % int(task.get("readable_message_count", 0)))
        count.setObjectName("muted")
        time = QLabel(_friendly_time(task.get("updated_at", "")))
        time.setObjectName("muted")
        meta.addWidget(source)
        meta.addWidget(count)
        meta.addStretch()
        meta.addWidget(time)
        title = QLabel(_compact(task["title"], 160))
        title.setObjectName("cardStatement")
        title.setWordWrap(True)
        layout.addLayout(meta)
        layout.addWidget(title)
        facts = []
        copies = int(task.get("source_copy_count", 1))
        subagents = int(task.get("subagent_count", 0))
        if copies > 1:
            facts.append("%d 个 Agent 来源已合并" % copies)
        if subagents:
            facts.append("%d 个子 Agent 分支已折叠" % subagents)
        hint = QLabel(" · ".join(facts) if facts else "1 份完整原始会话")
        hint.setObjectName("mergedHint" if facts else "muted")
        hint.setToolTip(str(task.get("merge_reason") or ""))
        layout.addWidget(hint)


# Kept as a compatibility name for extensions importing the old widget.
ConversationCard = TaskCard


class ProjectCard(QWidget):
    def __init__(self, project: Dict[str, Any]) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(5)
        heading = QHBoxLayout()
        name = QLabel(project["display_name"])
        name.setObjectName("cardStatement")
        state = QLabel(
            "已忽略"
            if project["ignored"]
            else ("自动整理" if project["sync_enabled"] else "已暂停")
        )
        state.setObjectName("projectState")
        state.setProperty(
            "projectState",
            "ignored" if project["ignored"] else ("on" if project["sync_enabled"] else "paused"),
        )
        heading.addWidget(name)
        heading.addStretch()
        heading.addWidget(state)
        source = "Git · %s" % project["remote"] if project.get("remote") else "本地文件夹"
        meta = QLabel(
            "%s  ·  %d 项任务  ·  %d 份原始会话"
            % (
                source,
                int(project.get("task_count", project["conversation_count"])),
                project["conversation_count"],
            )
        )
        meta.setObjectName("muted")
        roots = QLabel(
            "%d 个相关目录" % project["root_count"]
            if project["root_count"] > 1
            else project["canonical_root"]
        )
        roots.setObjectName("muted")
        layout.addLayout(heading)
        layout.addWidget(meta)
        layout.addWidget(roots)


class MetricCard(QFrame):
    def __init__(self, label: str) -> None:
        super().__init__()
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 11, 15, 11)
        layout.setSpacing(2)
        self.value = QLabel("—")
        self.value.setObjectName("metricValue")
        caption = QLabel(label)
        caption.setObjectName("metricLabel")
        layout.addWidget(self.value)
        layout.addWidget(caption)


class ProjectLibraryDialog(QDialog):
    def __init__(
        self,
        service: MemoryService,
        parent: QWidget,
        current_project_id: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.current_project_id = current_project_id
        self.projects: List[Dict[str, Any]] = []
        self.selected_path: Optional[Path] = None
        self.setWindowTitle("项目库")
        self.resize(900, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)

        title = QLabel("项目库")
        title.setObjectName("dialogTitle")
        intro = QLabel("Codex 与 Claude Code 的会话会自动归入对应项目；同一 Git remote 的目录只显示一次。")
        intro.setObjectName("dialogIntro")
        intro.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(intro)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索项目、remote 或目录")
        self.search.setClearButtonEnabled(True)
        self.list = QListWidget()
        self.list.setObjectName("projectList")
        self.list.setSpacing(0)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.list, 1)

        right = QFrame()
        right.setObjectName("projectDetail")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(18, 16, 18, 16)
        right_layout.setSpacing(12)
        self.detail_title = QLabel("选择一个项目")
        self.detail_title.setObjectName("sectionTitle")
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        right_layout.addWidget(self.detail_title)
        right_layout.addWidget(self.detail, 1)
        self.rename_button = QPushButton("重命名")
        self.toggle_button = QPushButton("暂停自动整理")
        self.root_button = QPushButton("设置主目录")
        self.ignore_button = QPushButton("忽略")
        action_row = QGridLayout()
        action_row.addWidget(self.rename_button, 0, 0)
        action_row.addWidget(self.toggle_button, 0, 1)
        action_row.addWidget(self.root_button, 1, 0)
        action_row.addWidget(self.ignore_button, 1, 1)
        right_layout.addLayout(action_row)
        body.addWidget(left)
        body.addWidget(right)
        body.setSizes([510, 340])
        layout.addWidget(body, 1)

        footer = QDialogButtonBox()
        add_button = footer.addButton("添加文件夹…", QDialogButtonBox.ButtonRole.ActionRole)
        self.open_button = footer.addButton("打开项目", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = footer.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        footer.rejected.connect(self.reject)
        cancel_button.clicked.connect(self.reject)
        add_button.clicked.connect(self._add_folder)
        self.open_button.clicked.connect(self._open)
        layout.addWidget(footer)

        self.search.textChanged.connect(self.refresh)
        self.list.currentRowChanged.connect(self._show_selected)
        self.list.itemDoubleClicked.connect(lambda _: self._open())
        self.rename_button.clicked.connect(self._rename)
        self.toggle_button.clicked.connect(self._toggle)
        self.root_button.clicked.connect(self._choose_root)
        self.ignore_button.clicked.connect(self._ignore)
        self.refresh()

    def _selected(self) -> Optional[Dict[str, Any]]:
        item = self.list.currentItem()
        if item is None:
            return None
        project_id = str(item.data(Qt.ItemDataRole.UserRole))
        return next((project for project in self.projects if project["project_id"] == project_id), None)

    def refresh(self) -> None:
        selected_id = self._selected()["project_id"] if self._selected() else self.current_project_id
        query = self.search.text().strip().casefold()
        self.projects = self.service.projects(include_ignored=True)
        visible = []
        for project in self.projects:
            haystack = " ".join(
                [project["display_name"], str(project.get("remote") or ""), *project["roots"]]
            ).casefold()
            if not query or query in haystack:
                visible.append(project)
        self.list.clear()
        restore = -1
        for index, project in enumerate(visible):
            try:
                project["task_count"] = len(
                    self.service.tasks(Path(project["canonical_root"]), limit=500)
                )
            except Exception:
                project["task_count"] = int(project.get("conversation_count", 0))
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, project["project_id"])
            item.setToolTip(project["canonical_root"])
            card = ProjectCard(project)
            card.adjustSize()
            item.setSizeHint(QSize(0, max(82, card.sizeHint().height())))
            self.list.addItem(item)
            self.list.setItemWidget(item, card)
            if project["project_id"] == selected_id:
                restore = index
        if self.list.count():
            self.list.setCurrentRow(restore if restore >= 0 else 0)
        else:
            self.detail_title.setText("没有匹配的项目")
            self.detail.setHtml("<p>同步 Agent 会话或添加一个项目文件夹后，它会出现在这里。</p>")
            self._set_actions(False)

    def _set_actions(self, enabled: bool) -> None:
        for button in (
            self.open_button, self.rename_button, self.toggle_button,
            self.root_button, self.ignore_button,
        ):
            button.setEnabled(enabled)

    def _show_selected(self) -> None:
        project = self._selected()
        self._set_actions(project is not None)
        if project is None:
            return
        self.detail_title.setText(project["display_name"])
        source = (
            "Git remote<br><b>%s</b>" % _html(project["remote"])
            if project.get("remote")
            else "普通文件夹项目"
        )
        roots = "".join(
            "<li>%s%s</li>" % (
                _html(root),
                " <b>· 主目录</b>" if root == project["canonical_root"] else "",
            )
            for root in project["roots"]
        )
        self.detail.setHtml(
            "<p>%s</p><hr><p><b>%d</b> 项任务 · <b>%d</b> 份原始会话 · <b>%d</b> 条消息<br>"
            "Codex %d · Claude Code %d<br>还有 %d 段会话待提炼</p>"
            "<hr><p><b>相关目录</b></p><ul>%s</ul>"
            % (
                source, project.get("task_count", project["conversation_count"]),
                project["conversation_count"], project["message_count"],
                project["codex_count"], project["claude_count"], project["pending_count"], roots,
            )
        )
        self.toggle_button.setText("暂停自动整理" if project["sync_enabled"] else "恢复自动整理")
        self.ignore_button.setText("恢复显示" if project["ignored"] else "忽略项目")
        self.root_button.setEnabled(len(project["roots"]) > 1)

    def _open(self) -> None:
        project = self._selected()
        if project is None or not project["canonical_root"]:
            return
        self.selected_path = Path(project["canonical_root"])
        self.accept()

    def _add_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "添加项目文件夹", str(Path.home()))
        if not chosen:
            return
        project = self.service.register_project(Path(chosen))
        self.current_project_id = project["project_id"]
        self.refresh()

    def _rename(self) -> None:
        project = self._selected()
        if project is None:
            return
        value, ok = QInputDialog.getText(self, "重命名项目", "项目名称", text=project["display_name"])
        if ok and value.strip():
            self.service.update_project(project["project_id"], display_name=value)
            self.current_project_id = project["project_id"]
            self.refresh()

    def _toggle(self) -> None:
        project = self._selected()
        if project is None:
            return
        self.service.update_project(project["project_id"], sync_enabled=not project["sync_enabled"])
        self.current_project_id = project["project_id"]
        self.refresh()

    def _choose_root(self) -> None:
        project = self._selected()
        if project is None or len(project["roots"]) < 2:
            return
        value, ok = QInputDialog.getItem(
            self, "设置主目录", "打开项目时默认使用", project["roots"],
            project["roots"].index(project["canonical_root"]), False,
        )
        if ok:
            self.service.update_project(project["project_id"], canonical_root=value)
            self.current_project_id = project["project_id"]
            self.refresh()

    def _ignore(self) -> None:
        project = self._selected()
        if project is None:
            return
        self.service.update_project(project["project_id"], ignored=not project["ignored"])
        self.current_project_id = project["project_id"]
        self.refresh()


class MemoryDialog(QDialog):
    def __init__(self, parent: QWidget, replacement: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(parent)
        self.replacement = replacement
        self.setWindowTitle("更新一条记忆" if replacement else "记住一件事")
        self.setMinimumWidth(580)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "写下发生变化后的新判断。旧版本会保留。"
            if replacement
            else "写下那些下一个智能体如果不知道，就容易做错的事。"
        )
        intro.setObjectName("dialogIntro")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        statement_label = QLabel("希望智能体记住什么？")
        statement_label.setObjectName("fieldTitle")
        layout.addWidget(statement_label)
        self.statement = QTextEdit()
        self.statement.setPlaceholderText(
            "例如：数据库迁移前必须先完成可恢复备份，并验证回滚流程。"
        )
        self.statement.setMinimumHeight(150)
        layout.addWidget(self.statement)

        type_row = QHBoxLayout()
        type_label = QLabel("这是一条")
        self.memory_type = QComboBox()
        preferred_order = [
            "decision", "constraint", "status", "handoff", "incident", "environment", "fact"
        ]
        for value in preferred_order:
            self.memory_type.addItem(TYPE_LABELS[value], value)
        self.type_help = QLabel()
        self.type_help.setObjectName("muted")
        self.type_help.setWordWrap(True)
        type_row.addWidget(type_label)
        type_row.addWidget(self.memory_type)
        type_row.addWidget(self.type_help, 1)
        layout.addLayout(type_row)

        self.confirmed = QCheckBox("立即提供给智能体（推荐）")
        self.confirmed.setChecked(True)
        layout.addWidget(self.confirmed)

        self.advanced = QGroupBox("补充依据与归属（可选）")
        self.advanced.setCheckable(True)
        self.advanced.setChecked(False)
        form = QFormLayout(self.advanced)
        self.subject = QLineEdit("当前项目")
        self.holder = QLineEdit("我")
        self.source_ref = QLineEdit()
        self.source_ref.setPlaceholderText("文件、Issue 或文档链接")
        form.addRow("关于谁或什么", self.subject)
        form.addRow("谁认为", self.holder)
        form.addRow("依据在哪里", self.source_ref)
        self.advanced.toggled.connect(self._set_advanced_visible)
        layout.addWidget(self.advanced)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.save_button = buttons.addButton(
            "保存新版本" if replacement else "保存记忆",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if replacement:
            for combo in (self.memory_type,):
                index = combo.findData(replacement["memory_type"])
                combo.setCurrentIndex(index)
                combo.setEnabled(False)
            self.subject.setText(replacement["subject"])
            self.subject.setEnabled(False)
            self.holder.setText(replacement["holder"])
            self.holder.setEnabled(False)
            self.confirmed.setChecked(True)
            self.confirmed.setText("新版本立即生效")
        self.memory_type.currentIndexChanged.connect(self._update_type_help)
        self._update_type_help()
        self._set_advanced_visible(False)
        self.statement.setFocus()

    def _update_type_help(self) -> None:
        self.type_help.setText(TYPE_HELP.get(str(self.memory_type.currentData()), ""))

    def _set_advanced_visible(self, visible: bool) -> None:
        form = self.advanced.layout()
        for field in (self.subject, self.holder, self.source_ref):
            field.setVisible(visible)
            label = form.labelForField(field) if isinstance(form, QFormLayout) else None
            if label:
                label.setVisible(visible)

    def _accept_if_valid(self) -> None:
        if not self.statement.toPlainText().strip():
            QMessageBox.warning(self, APP_NAME, "请填写记忆陈述。")
            return
        self.accept()

    def values(self) -> Dict[str, Any]:
        return {
            "memory_type": self.memory_type.currentData(),
            "subject": self.subject.text().strip(),
            "holder": self.holder.text().strip(),
            "statement": self.statement.toPlainText().strip(),
            "source_ref": self.source_ref.text().strip() or None,
            "confirmed": self.confirmed.isChecked(),
        }


class ConnectionDialog(QDialog):
    """One place for Agent archive connections and the optional processing kernel."""

    def __init__(self, config_path: Path, parent: QWidget) -> None:
        super().__init__(parent)
        self.config_path = config_path
        self.config = MemoryCoreConfig.load(config_path)
        self.open_agent_setup = False
        self.setWindowTitle("设置与连接")
        self.resize(620, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)

        title = QLabel("连接与记忆加工")
        title.setObjectName("dialogTitle")
        intro = QLabel("原始会话始终保存在本机；MemoryCore 只接收脱敏副本并生成 L1 / L2 / L3。")
        intro.setObjectName("dialogIntro")
        intro.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(intro)

        agent_box = QGroupBox("会话来源")
        agent_layout = QHBoxLayout(agent_box)
        agent_layout.addWidget(QLabel("Codex 与 Claude Code 自动归档到 QMemory"))
        agent_layout.addStretch()
        agent_button = QPushButton("管理 Agent 连接")
        agent_button.clicked.connect(self._choose_agent_setup)
        agent_layout.addWidget(agent_button)
        layout.addWidget(agent_box)

        memory_box = QGroupBox("TencentDB MemoryCore")
        form = QFormLayout(memory_box)
        self.enabled = QCheckBox("启用 L1 / L2 / L3 加工")
        self.enabled.setChecked(self.config.enabled)
        self.base_url = QLineEdit(self.config.base_url)
        self.base_url.setPlaceholderText("https://memory.example.com")
        self.service_id = QLineEdit(self.config.service_id)
        self.team_id = QLineEdit(self.config.team_id)
        self.user_id = QLineEdit(self.config.user_id)
        self.api_key_env = QLineEdit(self.config.api_key_env)
        self.api_key_env.setToolTip("这里只填写环境变量名称，不填写密钥本身")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("留空则沿用 macOS 钥匙串；本地无鉴权也可留空")
        form.addRow(self.enabled)
        form.addRow("服务地址", self.base_url)
        form.addRow("Service ID", self.service_id)
        form.addRow("Team ID", self.team_id)
        form.addRow("User ID", self.user_id)
        form.addRow("密钥环境变量", self.api_key_env)
        form.addRow("网关密钥", self.api_key)
        layout.addWidget(memory_box)

        security = QLabel("安全规则：远程地址必须使用 HTTPS；密钥由环境变量提供，不会写入 QMemory。")
        security.setObjectName("muted")
        security.setWordWrap(True)
        layout.addWidget(security)
        self.test_state = QLabel("尚未测试连接")
        self.test_state.setObjectName("muted")
        layout.addWidget(self.test_state)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        test_button = buttons.addButton("测试连接", QDialogButtonBox.ButtonRole.ActionRole)
        save_button = buttons.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
        test_button.clicked.connect(self._test_connection)
        save_button.clicked.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _values(self) -> MemoryCoreConfig:
        return MemoryCoreConfig(
            enabled=self.enabled.isChecked(),
            base_url=self.base_url.text().strip(),
            service_id=self.service_id.text().strip(),
            team_id=self.team_id.text().strip(),
            user_id=self.user_id.text().strip(),
            api_key_env=self.api_key_env.text().strip(),
        )

    def _test_connection(self) -> None:
        try:
            config = self._values()
            identity = MemoryCoreIdentity(
                config.team_id, config.user_id, "qmemory-connection-test", config.service_id
            )
            result = MemoryCoreClient(config, identity, timeout=4.0).health()
            self.test_state.setText("连接正常 · %s" % _compact(json.dumps(result, ensure_ascii=False), 100))
        except Exception as exc:
            self.test_state.setText("连接失败 · %s" % exc)

    def _save(self) -> None:
        try:
            self.config = self._values()
            self.config.save(self.config_path)
            if self.api_key.text():
                store_memorycore_api_key(self.config, self.api_key.text())
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "无法保存", str(exc))

    def _choose_agent_setup(self) -> None:
        self.open_agent_setup = True
        self.accept()


class MainWindow(QMainWindow):
    memory_changed = Signal()
    source_operation_finished = Signal(str, object)
    source_operation_failed = Signal(str, str)
    device_sync_finished = Signal(str, object)
    device_sync_failed = Signal(str, str)

    def __init__(
        self,
        service: Optional[MemoryService] = None,
        settings: Optional[QSettings] = None,
        initial_project: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self.service = service or MemoryService()
        self.app_settings = settings or QSettings(ORG_NAME, APP_NAME)
        self.project_path: Optional[Path] = None
        self.current_memories: List[Dict[str, Any]] = []
        self.active_memories: List[Dict[str, Any]] = []
        self.current_tasks: List[Dict[str, Any]] = []
        self.current_conversations: List[Dict[str, Any]] = []
        self.current_task_detail: Optional[Dict[str, Any]] = None
        self.current_project_id: Optional[str] = None
        self.sync_busy = False
        self.source_busy = False
        self.allow_close = False
        self.tray: Optional[QSystemTrayIcon] = None
        self.codex_state: Dict[str, Any] = {}
        self.claude_state: Dict[str, Any] = {}
        self._initializing = True
        self._responsive_forced_list = False
        self._switching_project = False
        self.nav_buttons: Dict[int, QPushButton] = {}
        self.layer_buttons: List[QPushButton] = []
        self.current_task_layer = 0
        self.memorycore_config_path = self.service.settings.config_dir / "memorycore.json"
        self.memorycore_config = MemoryCoreConfig.load(self.memorycore_config_path)
        self.memorycore_bridge = MemoryCoreBridge(self.service, self.memorycore_config)
        self.memorycore_layers: Dict[str, Dict[str, Any]] = {}

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1440, 900)
        self.setMinimumSize(980, 650)
        self._build_ui()
        self._build_tray()
        self._connect_signals()
        self._install_shortcuts()
        self._restore_state(initial_project)
        saved_view = str(self.app_settings.value("conversation_view", "graph"))
        self._set_conversation_view(1 if saved_view == "list" else 0)
        self._initializing = False
        self.refresh_all()

        self.sync_timer = QTimer(self)
        self.sync_timer.setInterval(5 * 60 * 1000)
        self.sync_timer.timeout.connect(self.auto_sync)
        self.sync_timer.start()
        QTimer.singleShot(900, self.auto_sync)

        self.archive_timer = QTimer(self)
        self.archive_timer.setInterval(5 * 60 * 1000)
        self.archive_timer.timeout.connect(self.auto_archive_codex)
        self.archive_timer.start()
        QTimer.singleShot(1400, self.auto_archive_codex)

    def _build_ui(self) -> None:
        self.add_action = QAction("补充记忆", self)
        self.add_action.triggered.connect(self.add_memory)
        self.import_action = QAction("同步对话", self)
        self.import_action.triggered.connect(self.import_codex_conversations)
        self.codex_action = QAction("连接 Agent", self)
        self.codex_action.triggered.connect(self.configure_codex)

        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_sidebar())

        content = QWidget()
        content.setObjectName("contentCanvas")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 18, 24, 16)
        content_layout.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        self.workspace_title = QLabel("工作台")
        self.workspace_title.setObjectName("pageTitle")
        self.workspace_subtitle = QLabel("证据先于结论 · 自动归档不同 Agent 里完成的每一项工作")
        self.workspace_subtitle.setObjectName("subtitle")
        title_box.addWidget(self.workspace_title)
        title_box.addWidget(self.workspace_subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.archive_status_badge = QLabel("自动归档 · 检查中")
        self.archive_status_badge.setObjectName("archiveStatusBadge")
        self.archive_status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.archive_status_badge.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        header.addWidget(self.archive_status_badge)
        self.memorycore_badge = QLabel("MemoryCore · %s" % ("已启用" if self.memorycore_config.enabled else "未启用"))
        self.memorycore_badge.setObjectName("memorycoreBadge")
        self.memorycore_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.memorycore_badge.setToolTip("点击打开连接设置")
        self.memorycore_badge.mousePressEvent = lambda _event: self.configure_connections()
        header.addWidget(self.memorycore_badge)
        self.health_badge = QLabel("检查中")
        self.health_badge.setObjectName("healthBadge")
        self.health_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.health_badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        header.addWidget(self.health_badge)
        content_layout.addLayout(header)

        self.total_memory_badge = QLabel("统计中")
        self.total_memory_badge.hide()
        content_layout.addWidget(self._build_metrics())

        self.setup_panel = self._build_setup_panel()
        content_layout.addWidget(self.setup_panel)

        project_bar = QFrame()
        project_bar.setObjectName("projectBar")
        project_layout = QGridLayout(project_bar)
        project_layout.setContentsMargins(14, 10, 14, 10)
        project_layout.setHorizontalSpacing(14)
        self.project_selector = QComboBox()
        self.project_selector.setObjectName("projectSelector")
        self.project_selector.setMinimumWidth(230)
        self.project_selector.setToolTip("自动同步发现的项目")
        project_layout.addWidget(self.project_selector, 0, 0, 2, 1)
        self.project_label = QLabel("尚未选择项目")
        self.project_label.setObjectName("projectLabel")
        self.project_id_label = QLabel("选择一个 Git 项目或普通文件夹")
        self.project_id_label.setObjectName("muted")
        project_layout.addWidget(self.project_label, 0, 1)
        project_layout.addWidget(self.project_id_label, 1, 1)
        self.project_auto_state = QLabel("自动发现相关项目")
        self.project_auto_state.setObjectName("projectAutoState")
        project_layout.addWidget(self.project_auto_state, 0, 2, 2, 1)
        choose_button = QPushButton("打开项目库")
        choose_button.clicked.connect(self.open_project_library)
        project_layout.addWidget(choose_button, 0, 3, 2, 1)
        content_layout.addWidget(project_bar)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setObjectName("mainSplitter")
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.addWidget(self._build_workspace_panel())
        self.main_splitter.addWidget(self._build_detail_panel())
        self.main_splitter.setSizes([820, 450])
        content_layout.addWidget(self.main_splitter, 1)
        root_layout.addWidget(content, 1)

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("就绪")
        self.setStyleSheet(STYLE_SHEET)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(172)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setSpacing(7)

        brand = QLabel("QMemory")
        brand.setObjectName("sidebarBrand")
        promise = QLabel("本地 Agent 记忆")
        promise.setObjectName("sidebarPromise")
        layout.addWidget(brand)
        layout.addWidget(promise)
        layout.addSpacing(20)

        for index, text in ((0, "工作台"), (1, "候选记忆"), (2, "正式记忆")):
            button = QPushButton(text)
            button.setObjectName("navButton")
            button.setProperty("selected", index == 0)
            button.clicked.connect(lambda _checked=False, value=index: self._switch_workspace(value))
            layout.addWidget(button)
            self.nav_buttons[index] = button

        projects = QPushButton("项目库")
        projects.setObjectName("navButton")
        projects.clicked.connect(self.open_project_library)
        layout.addWidget(projects)

        settings = QPushButton("设置与连接")
        settings.setObjectName("navButton")
        settings.clicked.connect(self.configure_connections)
        layout.addWidget(settings)
        layout.addStretch()

        self.sidebar_archive = QLabel("正在检查会话档案")
        self.sidebar_archive.setObjectName("sidebarMeta")
        self.sidebar_archive.setWordWrap(True)
        self.sidebar_health = QLabel("设备状态检查中")
        self.sidebar_health.setObjectName("sidebarMeta")
        self.sidebar_health.setWordWrap(True)
        layout.addWidget(self.sidebar_archive)
        layout.addWidget(self.sidebar_health)

        add_button = QPushButton("＋ 补充一条记忆")
        add_button.setObjectName("sidebarAction")
        add_button.clicked.connect(self.add_memory)
        layout.addWidget(add_button)
        return sidebar

    def _build_metrics(self) -> QWidget:
        strip = QWidget()
        strip.setObjectName("metricStrip")
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.projects_metric = MetricCard("项目")
        self.tasks_metric = MetricCard("任务")
        self.sources_metric = MetricCard("原始会话")
        self.memories_metric = MetricCard("正式记忆")
        for card in (
            self.projects_metric,
            self.tasks_metric,
            self.sources_metric,
            self.memories_metric,
        ):
            layout.addWidget(card, 1)
        return strip

    def _switch_workspace(self, index: int) -> None:
        if not hasattr(self, "workspace_tabs"):
            return
        self.workspace_tabs.setCurrentIndex(index)
        titles = {
            0: ("工作台", "证据先于结论 · 任务按项目自动归档，多 Agent 来源合并展示"),
            1: ("候选记忆", "只审阅值得长期保留的判断；原始会话始终完整保留"),
            2: ("正式记忆", "当前会提供给 Agent 的项目判断与历史版本"),
        }
        title, subtitle = titles.get(index, titles[0])
        self.workspace_title.setText(title)
        self.workspace_subtitle.setText(subtitle)
        for value, button in self.nav_buttons.items():
            button.setProperty("selected", value == index)
            button.style().unpolish(button)
            button.style().polish(button)

    def _set_conversation_view(self, index: int, *, persist: bool = True) -> None:
        requested_index = index
        if index == 0 and self.width() < 1180:
            index = 1
            self._responsive_forced_list = True
        elif persist:
            self._responsive_forced_list = False
        self.conversation_mode_stack.setCurrentIndex(index)
        graph_selected = index == 0
        self.task_view_title.setText("记忆图谱" if graph_selected else "任务档案")
        for button, selected in (
            (self.graph_view_button, graph_selected),
            (self.list_view_button, not graph_selected),
        ):
            button.setProperty("selected", selected)
            button.style().unpolish(button)
            button.style().polish(button)
        if hasattr(self, "main_splitter"):
            self.main_splitter.setSizes([820, 450] if graph_selected else [570, 700])
        if persist:
            self.app_settings.setValue(
                "conversation_view", "graph" if requested_index == 0 else "list"
            )
        if graph_selected:
            self._refresh_memory_graph()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if not hasattr(self, "conversation_mode_stack"):
            return
        compact = self.width() < 1180
        self.graph_view_button.setEnabled(not compact)
        self.graph_view_button.setToolTip(
            "将窗口加宽后查看完整图谱" if compact else "查看项目、任务、来源与记忆层关系"
        )
        if compact and self.conversation_mode_stack.currentIndex() == 0:
            self._set_conversation_view(1, persist=False)
            self._responsive_forced_list = True
        elif not compact and self._responsive_forced_list:
            saved = str(self.app_settings.value("conversation_view", "graph"))
            self._responsive_forced_list = False
            self._set_conversation_view(0 if saved == "graph" else 1, persist=False)

    def _select_task_from_graph(self, task_id: str) -> None:
        for index in range(self.conversation_list.count()):
            item = self.conversation_list.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole)) == task_id:
                if self.conversation_list.currentRow() == index:
                    self.show_conversation()
                else:
                    self.conversation_list.setCurrentRow(index)
                return

    def _select_layer_from_graph(self, index: int) -> None:
        self._show_task_layer(index)
        self._refresh_memory_graph()

    def _select_source_from_graph(self, conversation_id: str) -> None:
        self._show_task_layer(0)
        source_index = self.task_source_selector.findData(conversation_id)
        if source_index >= 0:
            self.task_source_selector.setCurrentIndex(source_index)
        self._refresh_memory_graph()

    def _related_memories_for_current_task(self) -> List[Dict[str, Any]]:
        if not self.project_path or not self.current_task_detail:
            return []
        conversation_ids = self._task_conversation_ids()
        needles = set(conversation_ids)
        needles.update(value.split(":", 1)[-1] for value in conversation_ids)
        memories = self.service.browse(self.project_path, status="proposed", limit=200)
        memories += self.service.browse(self.project_path, status="active", limit=200)
        return [
            memory
            for memory in memories
            if str(memory.get("source_ref") or "")
            and any(needle and needle in str(memory.get("source_ref") or "") for needle in needles)
        ]

    def _refresh_memory_graph(self) -> None:
        if not hasattr(self, "memory_graph"):
            return
        selected_id = self.selected_conversation_id() or ""
        detail = (
            self.current_task_detail
            if self.current_task_detail
            and str(self.current_task_detail.get("task_id") or "") == selected_id
            else None
        )
        l1_count = len(self._related_memories_for_current_task()) if detail else 0
        project_name = self.project_label.text() if self.project_path else "尚未选择项目"
        self.memory_graph.set_graph(
            project_name,
            self.current_tasks,
            selected_id,
            detail,
            self.current_task_layer,
            l1_count,
        )
        if not self.project_path:
            status = "选择一个项目，查看任务、Agent 来源与记忆层"
        elif not self.current_tasks:
            status = "这个项目还没有 Agent 会话；归档后会自动形成图谱"
        elif detail:
            status = "%d 项任务 · 当前任务 %d 份来源 · %d 个执行分支 · L1 %d 条" % (
                len(self.current_tasks),
                int(detail.get("source_copy_count", 0)),
                int(detail.get("subagent_count", 0)),
                l1_count,
            )
        else:
            status = "%d 项任务 · 选择节点查看完整来源链" % len(self.current_tasks)
        self.graph_status.setText(status)

    def _build_setup_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("setupPanel")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(18, 15, 18, 15)
        text_box = QVBoxLayout()
        self.setup_title = QLabel("让第一个智能体真正记住你的项目")
        self.setup_title.setObjectName("setupTitle")
        self.setup_detail = QLabel("只需完成三步。QMemory 会告诉你当前应该做什么。")
        self.setup_detail.setObjectName("setupDetail")
        self.setup_detail.setWordWrap(True)
        steps = QHBoxLayout()
        self.setup_steps: List[QLabel] = []
        for text in ("安装应用", "选择项目", "连接 Codex", "自动记忆"):
            label = QLabel(text)
            label.setObjectName("setupStep")
            steps.addWidget(label)
            self.setup_steps.append(label)
        steps.addStretch()
        text_box.addWidget(self.setup_title)
        text_box.addWidget(self.setup_detail)
        text_box.addLayout(steps)
        layout.addLayout(text_box, 1)
        self.setup_action = QPushButton("开始设置")
        self.setup_action.setObjectName("setupAction")
        layout.addWidget(self.setup_action)
        return panel

    def _build_workspace_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.setObjectName("workspaceTabs")
        self.workspace_tabs.addTab(self._build_conversation_panel(), "任务档案")
        self.workspace_tabs.addTab(self._build_memory_panel(), "候选记忆")
        self.workspace_tabs.addTab(self._build_memory_panel(current_only=True), "项目记忆")
        self.workspace_tabs.tabBar().hide()
        layout.addWidget(self.workspace_tabs)
        return panel

    def _build_conversation_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 12)
        title_row = QHBoxLayout()
        self.task_view_title = QLabel("记忆图谱")
        self.task_view_title.setObjectName("sectionTitle")
        title_row.addWidget(self.task_view_title)
        title_row.addStretch()
        self.graph_view_button = QPushButton("图谱")
        self.graph_view_button.setObjectName("viewModeButton")
        self.graph_view_button.setProperty("selected", True)
        self.graph_view_button.clicked.connect(lambda: self._set_conversation_view(0))
        self.list_view_button = QPushButton("列表")
        self.list_view_button.setObjectName("viewModeButton")
        self.list_view_button.setProperty("selected", False)
        self.list_view_button.clicked.connect(lambda: self._set_conversation_view(1))
        title_row.addWidget(self.graph_view_button)
        title_row.addWidget(self.list_view_button)
        layout.addLayout(title_row)

        self.conversation_mode_stack = QStackedWidget()
        graph_page = QWidget()
        graph_layout = QVBoxLayout(graph_page)
        graph_layout.setContentsMargins(0, 0, 0, 0)
        graph_layout.setSpacing(8)
        self.graph_status = QLabel("选择一个项目，查看任务如何形成记忆")
        self.graph_status.setObjectName("pipelineStatus")
        graph_layout.addWidget(self.graph_status)
        self.memory_graph = MemoryGraphCanvas()
        self.memory_graph.task_selected.connect(self._select_task_from_graph)
        self.memory_graph.layer_selected.connect(self._select_layer_from_graph)
        self.memory_graph.source_selected.connect(self._select_source_from_graph)
        graph_layout.addWidget(self.memory_graph, 1)
        legend = QLabel("● 项目    ● 任务    ● Agent 来源    ● 执行分支    ● L0–L3 记忆层")
        legend.setObjectName("graphLegend")
        graph_layout.addWidget(legend)
        self.conversation_mode_stack.addWidget(graph_page)

        list_page = QWidget()
        list_layout = QVBoxLayout(list_page)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(8)
        self.pipeline_status = QLabel("完整档案 0 项任务 · 0 轮对话")
        self.pipeline_status.setObjectName("pipelineStatus")
        list_layout.addWidget(self.pipeline_status)
        header = QHBoxLayout()
        self.conversation_search = QLineEdit()
        self.conversation_search.setPlaceholderText("搜索本项目的工作和对话")
        self.codex_import_button = QPushButton("立即同步")
        self.codex_import_button.clicked.connect(self.import_codex_conversations)
        header.addWidget(self.conversation_search, 1)
        header.addWidget(self.codex_import_button)
        list_layout.addLayout(header)
        self.conversation_list = QListWidget()
        self.conversation_list.setSpacing(0)
        self.conversation_list.setWordWrap(True)
        list_layout.addWidget(self.conversation_list, 1)
        self.conversation_summary = QLabel("尚未同步 Agent 工作记录")
        self.conversation_summary.setObjectName("muted")
        list_layout.addWidget(self.conversation_summary)
        self.conversation_mode_stack.addWidget(list_page)
        layout.addWidget(self.conversation_mode_stack, 1)
        return panel

    def _build_memory_panel(self, current_only: bool = False) -> QWidget:
        panel = QWidget()
        panel.setProperty("memoryView", "current" if current_only else "proposed")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 12)
        heading = QLabel("正式记忆" if current_only else "候选记忆")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        controls = QHBoxLayout()
        search_input = QLineEdit()
        search_input.setPlaceholderText("搜索判断、主题或类型")
        search_input.setClearButtonEnabled(True)
        status_filter = QComboBox()
        if current_only:
            status_filter.addItem("有效记忆", "active")
            status_filter.addItem("历史版本", "history")
        else:
            status_filter.addItem("等待审阅", "proposed")
        controls.addWidget(search_input, 1)
        controls.addWidget(status_filter)
        layout.addLayout(controls)

        if not current_only:
            analysis = QFrame()
            analysis.setObjectName("analysisBox")
            analysis_layout = QHBoxLayout(analysis)
            analysis_layout.setContentsMargins(12, 10, 12, 10)
            analysis_copy = QVBoxLayout()
            analysis_title = QLabel("从原始会话生成候选记忆")
            analysis_title.setObjectName("fieldTitle")
            analysis_hint = QLabel("这是后期加工；不影响原始会话的自动归档。")
            analysis_hint.setObjectName("muted")
            analysis_copy.addWidget(analysis_title)
            analysis_copy.addWidget(analysis_hint)
            self.auto_extract = QCheckBox("自动提炼")
            self.auto_extract.setToolTip("轮询已发现项目，生成的洞察仍需要你确认。")
            self.auto_extract.setChecked(
                self.app_settings.value("auto_extract_enabled", False, type=bool)
            )
            self.extract_button = QPushButton("提炼新增会话")
            self.extract_button.clicked.connect(self.extract_codex_insights)
            analysis_layout.addLayout(analysis_copy, 1)
            analysis_layout.addWidget(self.auto_extract)
            analysis_layout.addWidget(self.extract_button)
            layout.addWidget(analysis)

        memory_list = QListWidget()
        memory_list.setSpacing(0)
        memory_list.setWordWrap(True)
        layout.addWidget(memory_list, 1)
        summary = QLabel("0 条记忆")
        summary.setObjectName("muted")
        layout.addWidget(summary)
        if current_only:
            self.current_search_input = search_input
            self.current_status_filter = status_filter
            self.current_memory_list = memory_list
            self.current_list_summary = summary
        else:
            self.search_input = search_input
            self.status_filter = status_filter
            self.memory_list = memory_list
            self.list_summary = summary
        return panel

    def _build_detail_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        self.detail_heading = QLabel("会话详情")
        self.detail_heading.setObjectName("sectionTitle")
        layout.addWidget(self.detail_heading)
        self.layer_switch = QFrame()
        self.layer_switch.setObjectName("layerSwitch")
        layer_layout = QHBoxLayout(self.layer_switch)
        layer_layout.setContentsMargins(4, 4, 4, 4)
        layer_layout.setSpacing(4)
        for index, text in enumerate(
            ("L0 原始会话", "L1 原子记忆", "L2 项目场景", "L3 稳定内核")
        ):
            button = QPushButton(text)
            button.setObjectName("layerButton")
            button.setProperty("selected", index == 0)
            button.clicked.connect(
                lambda _checked=False, value=index: self._show_task_layer(value)
            )
            layer_layout.addWidget(button, 1)
            self.layer_buttons.append(button)
        layout.addWidget(self.layer_switch)
        self.task_source_bar = QWidget()
        source_bar_layout = QHBoxLayout(self.task_source_bar)
        source_bar_layout.setContentsMargins(0, 0, 0, 0)
        source_bar_layout.setSpacing(8)
        source_label = QLabel("查看来源")
        source_label.setObjectName("muted")
        self.task_source_selector = QComboBox()
        self.task_source_selector.setObjectName("taskSourceSelector")
        self.task_source_selector.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        source_bar_layout.addWidget(source_label)
        source_bar_layout.addWidget(self.task_source_selector, 1)
        layout.addWidget(self.task_source_bar)
        self.task_source_bar.hide()
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        self.detail.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.detail.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.detail.setPlaceholderText("选择一项任务，查看完整 Agent 对话。")
        layout.addWidget(self.detail, 1)
        self.memory_actions = QWidget()
        actions = QHBoxLayout(self.memory_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        self.confirm_button = QPushButton("确认")
        self.confirm_button.setObjectName("primaryButton")
        self.confirm_button.clicked.connect(self.confirm_selected)
        self.supersede_button = QPushButton("替代")
        self.supersede_button.clicked.connect(self.supersede_selected)
        self.helpful_button = QPushButton("有帮助")
        self.helpful_button.clicked.connect(lambda: self.feedback_selected("helpful"))
        actions.addWidget(self.confirm_button)
        actions.addWidget(self.supersede_button)
        actions.addStretch()
        actions.addWidget(self.helpful_button)
        layout.addWidget(self.memory_actions)
        self.memory_actions.hide()

        sync = QFrame()
        sync.setObjectName("syncBox")
        sync_layout = QVBoxLayout(sync)
        sync_title = QLabel("多设备记忆同步")
        sync_title.setObjectName("sectionTitle")
        self.sync_path_label = QLabel("尚未选择同步目录")
        self.sync_path_label.setObjectName("muted")
        self.sync_path_label.setWordWrap(True)
        sync_buttons = QHBoxLayout()
        choose_sync = QPushButton("选择目录")
        choose_sync.clicked.connect(self.choose_sync_directory)
        self.sync_now_button = QPushButton("立即同步")
        self.sync_now_button.clicked.connect(self.auto_sync)
        sync_buttons.addWidget(choose_sync)
        sync_buttons.addWidget(self.sync_now_button)
        sync_layout.addWidget(sync_title)
        sync_layout.addWidget(self.sync_path_label)
        sync_layout.addLayout(sync_buttons)
        layout.addWidget(sync)
        self.sync_box = sync
        self.sync_box.hide()
        return panel

    def _connect_signals(self) -> None:
        self.search_input.textChanged.connect(self.refresh_memories)
        self.status_filter.currentIndexChanged.connect(self.refresh_memories)
        self.memory_list.currentRowChanged.connect(self.show_selected)
        self.current_search_input.textChanged.connect(self.refresh_memories)
        self.current_status_filter.currentIndexChanged.connect(self.refresh_memories)
        self.current_memory_list.currentRowChanged.connect(self.show_selected)
        self.conversation_search.textChanged.connect(self.refresh_conversations)
        self.conversation_list.currentRowChanged.connect(self.show_conversation)
        self.task_source_selector.currentIndexChanged.connect(self._show_task_source)
        self.workspace_tabs.currentChanged.connect(self._workspace_changed)
        self.memory_changed.connect(self.refresh_all)
        self.source_operation_finished.connect(self._source_operation_done)
        self.source_operation_failed.connect(self._source_operation_error)
        self.device_sync_finished.connect(self._device_sync_done)
        self.device_sync_failed.connect(self._device_sync_error)
        self.auto_extract.toggled.connect(self._auto_extract_changed)
        self.project_selector.currentIndexChanged.connect(self._project_selector_changed)
        self.setup_action.clicked.connect(self._run_setup_action)

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Meta+N"), self, activated=self.add_memory)
        QShortcut(QKeySequence("Meta+O"), self, activated=self.open_project_library)
        QShortcut(QKeySequence("Meta+F"), self, activated=self.search_input.setFocus)
        QShortcut(QKeySequence("Meta+R"), self, activated=self.refresh_all)

    def _restore_state(self, initial_project: Optional[Path]) -> None:
        stored_project = self.app_settings.value("project_path", "")
        candidate = initial_project or (Path(str(stored_project)) if stored_project else None)
        if candidate and candidate.exists():
            self.set_project(candidate)
        sync_path = str(self.app_settings.value("sync_path", ""))
        if sync_path:
            self.sync_path_label.setText(sync_path)
        geometry = self.app_settings.value("window_geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def set_project(self, path: Path) -> None:
        project = self.service.register_project(path)
        identity = self.service.project(path)
        canonical = str(project.get("canonical_root") or identity.root)
        self.project_path = Path(canonical)
        self.current_project_id = identity.project_id
        self.project_label.setText(project["display_name"])
        remote_hint = identity.remote or "本地文件夹"
        related = " · %d 个相关目录" % project["root_count"] if project["root_count"] > 1 else ""
        self.project_id_label.setText("%s%s  ·  %s" % (remote_hint, related, canonical))
        self.project_auto_state.setText("自动整理" if project["sync_enabled"] else "已暂停自动整理")
        self.project_auto_state.setProperty("state", "on" if project["sync_enabled"] else "paused")
        self.project_auto_state.style().unpolish(self.project_auto_state)
        self.project_auto_state.style().polish(self.project_auto_state)
        self.project_label.setToolTip(canonical)
        self.app_settings.setValue("project_path", canonical)
        self.add_action.setEnabled(True)
        if not self._initializing:
            self._refresh_project_selector()
            self.refresh_conversations()
            self.refresh_memories()

    def _refresh_project_selector(self) -> None:
        selected = self.current_project_id
        self.project_selector.blockSignals(True)
        self.project_selector.clear()
        self.project_selector.addItem("选择项目…", "")
        for project in self.service.projects():
            task_count = int(project.get("conversation_count", 0))
            label = "%s  ·  %d 项任务" % (project["display_name"], task_count)
            self.project_selector.addItem(label, project["canonical_root"])
            self.project_selector.setItemData(
                self.project_selector.count() - 1,
                project["project_id"],
                Qt.ItemDataRole.UserRole + 1,
            )
            if project["project_id"] == selected:
                self.project_selector.setCurrentIndex(self.project_selector.count() - 1)
        self.project_selector.blockSignals(False)

    def _project_selector_changed(self) -> None:
        value = str(self.project_selector.currentData() or "")
        if value and Path(value).exists():
            self.set_project(Path(value))

    def choose_project(self) -> None:
        start = str(self.project_path or Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选择项目目录", start)
        if chosen:
            self.set_project(Path(chosen))

    def open_project_library(self) -> None:
        dialog = ProjectLibraryDialog(self.service, self, self.current_project_id)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_path:
            self.set_project(dialog.selected_path)

    def refresh_all(self) -> None:
        try:
            status = self.service.status()
            issues = int(status.get("projection_issues", 0))
            conflicts = len(list(self.service.settings.conflicts_dir.iterdir()))
            if issues or conflicts:
                self.health_badge.setText("需处理 · %d" % (issues + conflicts))
                self.health_badge.setProperty("state", "warning")
                self.sidebar_health.setText("设备需要处理 · %d 项" % (issues + conflicts))
            else:
                self.health_badge.setText("设备健康")
                self.health_badge.setProperty("state", "ok")
                self.sidebar_health.setText("设备健康\n%s" % status["device_id"])
            self.health_badge.style().unpolish(self.health_badge)
            self.health_badge.style().polish(self.health_badge)
            self._refresh_codex_state()
            totals = self.service.totals()
            memories = totals["memories"]
            self.total_memory_badge.setText(
                "%d 个项目 · %d 项任务 · %d 份会话"
                % (
                    int(totals["projects"]),
                    int(totals["tasks"]),
                    int(totals["conversation_files"]),
                )
            )
            self.projects_metric.value.setText(str(int(totals["projects"])))
            self.tasks_metric.value.setText(str(int(totals["tasks"])))
            self.sources_metric.value.setText(str(int(totals["conversation_files"])))
            self.memories_metric.value.setText(str(int(memories.get("active", 0))))
            self.sidebar_archive.setText(
                "%d 份原始会话\nCodex 与 Claude Code" % int(totals["conversation_files"])
            )
            self.total_memory_badge.setToolTip(
                "正式记忆 %d 条 · 候选 %d 条 · 已合并 %d 个来源副本"
                % (
                    int(memories.get("active", 0)), int(memories.get("proposed", 0)),
                    int(totals["duplicate_files"]),
                )
            )
            self.archive_status_badge.setText("自动归档 · 已就绪")
            self.archive_status_badge.setToolTip(
                "Codex 与 Claude Code 每 5 分钟增量扫描；上次检查 %s"
                % datetime.now().astimezone().strftime("%H:%M")
            )
            if self.project_path:
                project = self.service.register_project(self.project_path)
                self.project_label.setText(project["display_name"])
                self.project_auto_state.setText(
                    "自动整理" if project["sync_enabled"] else "已暂停自动整理"
                )
            self._refresh_project_selector()
            self.refresh_conversations()
            self.refresh_memories()
            self._refresh_setup_state()
        except Exception as exc:
            self._show_error("刷新失败", exc)

    def refresh_memories(self) -> None:
        selected_id = self.selected_memory_id()
        self.memory_list.clear()
        self.current_memory_list.clear()
        self.current_memories = []
        self.active_memories = []
        if not self.project_path:
            self.list_summary.setText("请选择项目")
            self.current_list_summary.setText("请选择项目")
            if self.workspace_tabs.currentIndex() != 0:
                self.detail.clear()
            self._update_action_state(None)
            return
        try:
            self.current_memories = self.service.browse(
                self.project_path,
                query=self.search_input.text(),
                status=str(self.status_filter.currentData()),
                limit=100,
            )
            self.active_memories = self.service.browse(
                self.project_path,
                query=self.current_search_input.text(),
                status=str(self.current_status_filter.currentData()),
                limit=100,
            )
            proposed_row = self._fill_memory_list(
                self.memory_list, self.current_memories, selected_id
            )
            active_row = self._fill_memory_list(
                self.current_memory_list, self.active_memories, selected_id
            )
            self.list_summary.setText("%d 条记忆" % len(self.current_memories))
            self.current_list_summary.setText("%d 条记忆" % len(self.active_memories))
            active_list = (
                self.current_memory_list
                if self.workspace_tabs.currentIndex() == 2
                else self.memory_list
            )
            if (
                self.workspace_tabs.currentIndex() == 1
                and not self.current_memories
                and selected_id
                and any(memory["memory_id"] == selected_id for memory in self.active_memories)
            ):
                self.workspace_tabs.setCurrentIndex(2)
                active_list = self.current_memory_list
                row = active_row
            row = active_row if active_list is self.current_memory_list else proposed_row
            if active_list.count():
                active_list.setCurrentRow(row if row >= 0 else 0)
            elif self.workspace_tabs.currentIndex() in (1, 2):
                self.detail.clear()
                self._update_action_state(None)
        except Exception as exc:
            self._show_error("读取记忆失败", exc)

    @staticmethod
    def _fill_memory_list(
        widget: QListWidget, memories: List[Dict[str, Any]], selected_id: Optional[str]
    ) -> int:
        restore_row = -1
        for index, memory in enumerate(memories):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, memory["memory_id"])
            item.setToolTip(memory["statement"])
            card = MemoryCard(memory)
            card.adjustSize()
            item.setSizeHint(QSize(0, max(84, card.sizeHint().height())))
            widget.addItem(item)
            widget.setItemWidget(item, card)
            if memory["memory_id"] == selected_id:
                restore_row = index
        return restore_row

    def refresh_conversations(self) -> None:
        selected = self.selected_conversation_id()
        self.conversation_list.clear()
        self.current_tasks = []
        self.current_conversations = []
        if not self.project_path:
            self.conversation_summary.setText("选择项目后查看对话档案")
            self.current_task_detail = None
            self.detail_heading.setText("任务详情")
            self.layer_switch.show()
            self.task_source_bar.hide()
            self.memory_actions.hide()
            self.sync_box.hide()
            self.detail.setHtml(
                '<div style="color:#626574;padding:24px 8px;">'
                '<h3 style="color:#16181F;">先选择一个项目</h3>'
                '<p>QMemory 会把 Codex 与 Claude Code 的会话按项目自动归档，'
                '并在这里显示完整任务证据。</p>'
                '</div>'
            )
            self._refresh_memory_graph()
            return
        try:
            self.current_tasks = self.service.tasks(
                self.project_path, query=self.conversation_search.text(), limit=200
            )
            # Compatibility for integrations that still inspect this public field.
            self.current_conversations = self.current_tasks
            restore_row = -1
            total_messages = 0
            for index, task in enumerate(self.current_tasks):
                total_messages += int(task.get("readable_message_count", 0))
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, task["task_id"])
                item.setToolTip(task["title"])
                card = TaskCard(task)
                card.adjustSize()
                item.setSizeHint(QSize(0, max(92, card.sizeHint().height())))
                self.conversation_list.addItem(item)
                self.conversation_list.setItemWidget(item, card)
                if task["task_id"] == selected:
                    restore_row = index
            self.conversation_summary.setText(
                "%d 项任务 · %d 轮对话 · 多 Agent 副本已合并，原始档案完整保留"
                % (len(self.current_tasks), total_messages)
            )
            stats = self.service.insight_stats(self.project_path)
            proposed = len(self.service.browse(self.project_path, status="proposed"))
            active = len(self.service.browse(self.project_path, status="active"))
            self.pipeline_status.setText(
                "完整档案 %d 项任务 · %d 轮对话   |   后期加工：%d 待提炼 · %d 候选 · %d 正式"
                % (len(self.current_tasks), total_messages, stats["pending"], proposed, active)
            )
            self.extract_button.setText(
                "重试失败任务" if stats["failed"] else "提炼新增会话 · %d" % stats["pending"]
            )
            self._refresh_memory_graph()
            if self.current_tasks and self.workspace_tabs.currentIndex() == 0:
                self.conversation_list.setCurrentRow(restore_row if restore_row >= 0 else 0)
            elif not self.current_tasks and self.workspace_tabs.currentIndex() == 0:
                self.current_task_detail = None
                self.detail_heading.setText("任务详情")
                self.detail.setHtml(
                    '<div style="color:#77736b;padding:24px 8px;">'
                    '<h3 style="color:#161616;">这个项目还没有 Agent 会话</h3>'
                    '<p>继续使用 Codex 或 Claude Code，QMemory 会在下次扫描时自动归档。</p>'
                    '</div>'
                )
                self._refresh_memory_graph()
        except Exception as exc:
            self._show_error("读取会话失败", exc)

    def _refresh_codex_state(self) -> None:
        expected = self._packaged_executable()
        self.codex_state = diagnose_codex_connection(self._find_codex(), expected)
        self.claude_state = diagnose_claude_connection(self._find_claude(), expected)
        connected = sum(
            state.get("state") == "connected"
            for state in (self.codex_state, self.claude_state)
        )
        self.codex_action.setText(
            "2 个 Agent 已连接" if connected == 2 else "连接 Agent · %d/2" % connected
        )

    def _refresh_setup_state(self) -> None:
        bundle = self._app_bundle()
        install_ready = bundle is None or bundle.parent == Path("/Applications")
        project_ready = bool(self.project_path and self.project_path.exists())
        codex_ready = (
            self.codex_state.get("state") == "connected"
            and self.claude_state.get("state") == "connected"
        )
        protocol_ready = all(
            inspect_protocol(path)["state"] == "current"
            for path in (global_agents_path(), global_claude_path())
        )
        states = (install_ready, project_ready, codex_ready, protocol_ready)
        self.setup_panel.setVisible(not all(states))
        for label, done in zip(self.setup_steps, states):
            label.setText(("✓ " if done else "○ ") + label.text().lstrip("✓○ "))
            label.setProperty("done", done)
            label.style().unpolish(label)
            label.style().polish(label)
        if not install_ready:
            self.setup_title.setText("先把 QMemory 安装到应用程序")
            self.setup_detail.setText("固定安装位置后，Codex 连接不会因为移动文件而失效。")
            self.setup_action.setText("安装到应用程序")
            self.setup_action.setProperty("setupAction", "install")
        elif not project_ready:
            self.setup_title.setText("第一步：打开你的项目库")
            self.setup_detail.setText("QMemory 会从 Agent 会话自动发现项目；也可以手动添加一个文件夹。")
            self.setup_action.setText("打开项目库")
            self.setup_action.setProperty("setupAction", "project")
        elif not codex_ready:
            self.setup_title.setText("第二步：让 Agent 使用 QMemory")
            self.setup_detail.setText(
                "%s；%s。"
                % (
                    self.codex_state.get("title", "尚未连接 Codex"),
                    self.claude_state.get("title", "尚未连接 Claude Code"),
                )
            )
            self.setup_action.setText("修复连接" if self.codex_state.get("state") in ("broken", "outdated", "disabled") else "连接 Codex")
            self.setup_action.setProperty("setupAction", "codex")
        elif not protocol_ready:
            self.setup_title.setText("第三步：让 Agent 自动维护记忆")
            self.setup_detail.setText("安装全局协作协议后，Agent 会在任务开始读取、过程中沉淀、结束时交接。")
            self.setup_action.setText("启用自动记忆")
            self.setup_action.setProperty("setupAction", "protocol")
        else:
            pending = 0
            if project_ready:
                pending = len(self.service.browse(self.project_path, status="proposed"))
            self.setup_title.setText("Agent 自动记忆已开启")
            self.setup_detail.setText(
                ("有 %d 条 Agent 提议等待你审阅。" % pending)
                if pending
                else "开始正常使用 Codex；QMemory 会自动读取、沉淀与交接。"
            )
            self.setup_action.setText("查看待确认" if pending else "手动补充")
            self.setup_action.setProperty("setupAction", "review" if pending else "complete")
        self.setup_action.style().unpolish(self.setup_action)
        self.setup_action.style().polish(self.setup_action)

    def _run_setup_action(self) -> None:
        action = self.setup_action.property("setupAction")
        if action == "install":
            self.install_application()
        elif action == "project":
            self.open_project_library()
        elif action == "codex":
            self.configure_codex()
        elif action == "protocol":
            self.enable_agent_protocol()
        elif action == "review":
            self.workspace_tabs.setCurrentIndex(1)
            self.status_filter.setCurrentIndex(self.status_filter.findData("proposed"))
        else:
            self.add_memory()

    def enable_agent_protocol(self) -> None:
        paths = (global_agents_path(), global_claude_path())
        if all(inspect_protocol(path)["state"] == "current" for path in paths):
            self.statusBar().showMessage("Agent 自动记忆已启用", 3000)
            self._refresh_setup_state()
            return
        answer = QMessageBox.question(
            self,
            "启用 Agent 自动记忆",
            "把 QMemory 协作协议写入 Codex 与 Claude Code 全局指令？\n\n"
            "之后两个 Agent 都会自动归档对话、读取项目记忆、"
            "提出稳定判断并在任务结束写交接。"
            "Agent 推断默认待确认，不会把沉默当作批准。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            for path in paths:
                install_protocol(path)
            self._refresh_setup_state()
            QMessageBox.information(
                self,
                "自动记忆已启用",
                "请重启 Codex 与 Claude Code。此后无需先提醒 Agent 读取 QMemory。",
            )
        except Exception as exc:
            self._show_error("启用自动记忆失败", exc)

    def install_application(self) -> None:
        source = self._app_bundle()
        if source is None:
            QMessageBox.information(self, "安装 QMemory", "源码运行模式无需安装。")
            return
        destination = Path("/Applications/QMemory.app")
        if source == destination:
            self.statusBar().showMessage("QMemory 已安装", 3000)
            self.refresh_all()
            return
        answer = QMessageBox.question(
            self,
            "安装 QMemory",
            "把 QMemory 复制到“应用程序”文件夹？\n\n"
            "安装后会从固定位置重新打开，当前记忆数据不会移动。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = run_text(
                ["/usr/bin/ditto", str(source), str(destination)],
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stdout.strip() or "无法复制到应用程序目录")
            subprocess.Popen(["/usr/bin/open", str(destination)])
            self.allow_close = True
            QTimer.singleShot(400, self.quit_application)
        except Exception as exc:
            self._show_error("安装失败", exc)

    def _active_memory_list(self) -> QListWidget:
        return self.current_memory_list if self.workspace_tabs.currentIndex() == 2 else self.memory_list

    def selected_memory_id(self) -> Optional[str]:
        item = self._active_memory_list().currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def selected_memory(self) -> Optional[Dict[str, Any]]:
        memory_id = self.selected_memory_id()
        if not memory_id:
            return None
        memories = self.active_memories if self.workspace_tabs.currentIndex() == 2 else self.current_memories
        return next(
            (memory for memory in memories if memory["memory_id"] == memory_id),
            None,
        )

    def show_selected(self) -> None:
        if self.workspace_tabs.currentIndex() == 0:
            return
        memory = self.selected_memory()
        self.detail_heading.setText("记忆详情")
        self.layer_switch.hide()
        self.task_source_bar.hide()
        self.sync_box.show()
        self.memory_actions.show()
        self._update_action_state(memory)
        if not memory:
            self.detail.clear()
            return
        source = memory.get("source_ref") or "未提供"
        commit = memory.get("commit_sha") or "未关联"
        html = """
        <p style="color:#87837a;font-size:12px;">{kind} · {status}</p>
        <h2 style="line-height:1.35;color:#161616;">{statement}</h2>
        <hr>
        <p><b>谁的判断</b><br>{holder}</p>
        <p><b>关于什么</b><br>{subject}</p>
        <p><b>从何时成立</b><br>{as_of}</p>
        <p><b>判断依据</b><br>{source}</p>
        <br><p style="color:#87837a;font-size:11px;">代码版本 {commit}<br>记录设备 {device}</p>
        """.format(
            statement=_html(memory["statement"]),
            status=_html(STATUS_LABELS.get(memory["status"], memory["status"])),
            kind=_html(TYPE_LABELS.get(memory["memory_type"], memory["memory_type"])),
            holder=_html(memory["holder"]),
            subject=_html(memory["subject"]),
            as_of=_html(memory["as_of"]),
            source=_html(source),
            commit=_html(commit),
            device=_html(memory["created_by_device"]),
        )
        self.detail.setHtml(html)

    def _update_action_state(self, memory: Optional[Dict[str, Any]]) -> None:
        status = memory["status"] if memory else ""
        self.confirm_button.setEnabled(status == "proposed")
        self.supersede_button.setEnabled(status in ("active", "proposed"))
        self.helpful_button.setEnabled(status == "active")

    def add_memory(self) -> None:
        if not self.project_path:
            self.choose_project()
            if not self.project_path:
                return
        dialog = MemoryDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            result = self.service.remember(
                self.project_path,
                values["memory_type"],
                values["statement"],
                subject=values["subject"],
                holder=values["holder"],
                confirmed=values["confirmed"],
                source_kind="desktop",
                source_ref=values["source_ref"],
            )
            self.memory_changed.emit()
            self._select_memory(result["memory_id"])
            self._notify("记忆已保存", result["statement"])
        except Exception as exc:
            self._show_error("保存失败", exc)

    def confirm_selected(self) -> None:
        memory = self.selected_memory()
        if not memory or not self.project_path:
            return
        answer = QMessageBox.question(
            self,
            "确认记忆",
            "确认后，这条判断会进入智能体的默认召回上下文。\n\n%s" % memory["statement"],
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.confirm(self.project_path, memory["memory_id"])
            self.memory_changed.emit()
            self.workspace_tabs.setCurrentIndex(2)
            for index in range(self.current_memory_list.count()):
                item = self.current_memory_list.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == memory["memory_id"]:
                    self.current_memory_list.setCurrentRow(index)
                    break
        except Exception as exc:
            self._show_error("确认失败", exc)

    def supersede_selected(self) -> None:
        memory = self.selected_memory()
        if not memory or not self.project_path:
            return
        dialog = MemoryDialog(self, replacement=memory)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            result = self.service.supersede(
                self.project_path,
                memory["memory_id"],
                values["statement"],
                confirmed=values["confirmed"],
                source_ref=values["source_ref"],
            )
            self.memory_changed.emit()
            self._select_memory(result["memory_id"])
        except Exception as exc:
            self._show_error("替代失败", exc)

    def feedback_selected(self, signal: str) -> None:
        memory = self.selected_memory()
        if not memory or not self.project_path:
            return
        try:
            self.service.feedback(self.project_path, memory["memory_id"], signal)
            self.statusBar().showMessage("已记录检索反馈", 3000)
        except Exception as exc:
            self._show_error("记录反馈失败", exc)

    def _select_memory(self, memory_id: str) -> None:
        self.workspace_tabs.setCurrentIndex(1)
        for index in range(self.memory_list.count()):
            item = self.memory_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == memory_id:
                self.memory_list.setCurrentRow(index)
                return

    def selected_conversation_id(self) -> Optional[str]:
        item = self.conversation_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def show_conversation(self) -> None:
        if self.workspace_tabs.currentIndex() != 0 or not self.project_path:
            return
        task_id = self.selected_conversation_id()
        self.detail_heading.setText("任务详情")
        self.layer_switch.show()
        self.sync_box.hide()
        self.memory_actions.hide()
        self._update_action_state(None)
        if not task_id:
            self.current_task_detail = None
            self.task_source_bar.hide()
            self.detail.clear()
            self._refresh_memory_graph()
            return
        try:
            task = self.service.task(self.project_path, task_id)
            self.current_task_detail = task
            self._populate_task_sources(task)
            self._show_task_layer(0)
        except Exception as exc:
            self._show_error("读取会话失败", exc)

    def _show_task_layer(self, index: int) -> None:
        if not self.current_task_detail or self.workspace_tabs.currentIndex() != 0:
            return
        self.current_task_layer = index
        for value, button in enumerate(self.layer_buttons):
            button.setProperty("selected", value == index)
            button.style().unpolish(button)
            button.style().polish(button)
        if index == 0:
            self.task_source_bar.setVisible(self.task_source_selector.count() > 0)
            self._show_task_source()
        elif index == 1:
            self.task_source_bar.hide()
            self._render_task_memories()
            self._request_memorycore_layers()
        elif index == 2:
            self.task_source_bar.hide()
            self._render_memorycore_layer(2)
            self._request_memorycore_layers()
        else:
            self.task_source_bar.hide()
            self._render_memorycore_layer(3)
            self._request_memorycore_layers()
        self._refresh_memory_graph()

    def _request_memorycore_layers(self) -> None:
        if not self.memorycore_config.enabled or not self.project_path or not self.current_task_detail:
            return
        task_id = str(self.current_task_detail.get("task_id") or "")
        if not task_id or task_id in self.memorycore_layers or self.source_busy:
            return
        project = self.project_path
        self._run_source_operation(
            "memorycore-layer",
            lambda: self.memorycore_bridge.task_layers(project, task_id),
        )

    @staticmethod
    def _memorycore_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("content", "statement", "summary", "text"):
                if value.get(key):
                    return str(value[key])
        return json.dumps(value, ensure_ascii=False, indent=2)

    def _render_memorycore_layer(self, layer: int) -> None:
        task_id = str((self.current_task_detail or {}).get("task_id") or "")
        data = self.memorycore_layers.get(task_id)
        title = "L2 项目场景" if layer == 2 else "L3 稳定内核"
        purpose = (
            "把相关 L1 原子记忆组织成项目背景、当前状态、重要约束和下一步。"
            if layer == 2 else
            "只保存长期稳定、跨任务仍然成立的项目原则和个人协作偏好。"
        )
        if not self.memorycore_config.enabled:
            self._render_future_layer(title, purpose, "尚未启用 MemoryCore。可在“设置与连接”中配置。")
            return
        if not data:
            self._render_future_layer(title, purpose, "正在读取 MemoryCore 加工结果……")
            return
        value = data.get("l2" if layer == 2 else "l3")
        text = self._memorycore_text(value)
        self.detail.setHtml(
            '<div style="padding:22px 10px;color:#626574;">'
            '<p style="color:#5A4FE6;font-size:11px;letter-spacing:.08em;">MemoryCore · 只读加工结果</p>'
            '<h2 style="color:#16181F;line-height:1.25;">%s</h2><p>%s</p>'
            '<div style="background:#F8F8FA;border:1px solid #DDE0E8;padding:13px;border-radius:8px;line-height:1.6;">%s</div>'
            '<p style="color:#747887;font-size:11px;">该结果不会自动写入正式记忆；L0 原始会话仍由 QMemory 完整保留。</p></div>'
            % (_html(title), _html(purpose), _html(text))
        )

    def _task_conversation_ids(self) -> List[str]:
        task = self.current_task_detail or {}
        values = [str(task.get("primary_conversation_id") or "")]
        values.extend(
            str(member.get("conversation_id") or "")
            for member in list(task.get("source_copies", [])) + list(task.get("subagents", []))
        )
        return [value for value in dict.fromkeys(values) if value]

    def _render_task_memories(self) -> None:
        if not self.project_path:
            return
        related = self._related_memories_for_current_task()
        task = self.current_task_detail or {}
        remote = self.memorycore_layers.get(str(task.get("task_id") or ""), {}).get("l1", [])
        if not related and not remote:
            self.detail.setHtml(
                '<div style="padding:22px 10px;color:#626574;">'
                '<p style="color:#5A4FE6;font-size:11px;letter-spacing:.08em;">L1 · 原子记忆</p>'
                '<h2 style="color:#16181F;line-height:1.25;">这项任务还没有形成候选记忆</h2>'
                '<p>原始会话已经安全归档。只有长期有效的决定、约束、事实、事故和交接才会进入这里。</p>'
                '<p style="background:#F0EEFF;padding:10px 12px;border-radius:8px;">'
                '没有候选记忆也是正常状态，不需要为了填满列表而提炼。</p></div>'
            )
            return
        cards = []
        for memory in related[:12]:
            status = STATUS_LABELS.get(memory["status"], memory["status"])
            color = "#B77A22" if memory["status"] == "proposed" else "#0E9C6B"
            cards.append(
                '<div style="border:1px solid #DDE0E8;border-left:3px solid %s;'
                'padding:11px 13px;margin:0 0 10px 0;border-radius:8px;">'
                '<p style="color:%s;font-size:11px;margin:0 0 5px 0;">%s · %s</p>'
                '<p style="color:#16181F;font-size:14px;margin:0;line-height:1.55;">%s</p>'
                '<p style="color:#747887;font-size:10px;margin:7px 0 0 0;">%s</p></div>'
                % (
                    color,
                    color,
                    _html(TYPE_LABELS.get(memory["memory_type"], memory["memory_type"])),
                    _html(status),
                    _html(memory["statement"]),
                    _html(memory.get("source_ref") or "来源待补充"),
                )
            )
        for memory in remote[:12]:
            text = self._memorycore_text(memory)
            refs = memory.get("qmemory_source_refs", []) if isinstance(memory, dict) else []
            source = (
                "%d 个 QMemory 原始消息引用" % len(refs)
                if refs else "MemoryCore 派生 · 来源映射待补齐"
            )
            cards.append(
                '<div style="border:1px solid #D8D4FF;border-left:3px solid #7168EA;'
                'padding:11px 13px;margin:0 0 10px 0;border-radius:8px;">'
                '<p style="color:#5A4FE6;font-size:11px;margin:0 0 5px 0;">MemoryCore · 只读预览</p>'
                '<p style="color:#16181F;font-size:14px;margin:0;line-height:1.55;">%s</p>'
                '<p style="color:#747887;font-size:10px;margin:7px 0 0 0;">%s</p></div>'
                % (_html(text), _html(source))
            )
        self.detail.setHtml(
            '<p style="color:#5A4FE6;font-size:11px;letter-spacing:.08em;">L1 · 原子记忆</p>'
            '<h2 style="color:#16181F;line-height:1.25;">%s</h2>'
            '<p style="color:#626574;">%d 条 QMemory 判断 · %d 条 MemoryCore 加工结果。</p>%s'
            % (_html(task.get("title", "任务记忆")), len(related), len(remote), "".join(cards))
        )

    def _render_future_layer(self, title: str, purpose: str, state: str) -> None:
        self.detail.setHtml(
            '<div style="padding:22px 10px;color:#626574;">'
            '<p style="color:#5A4FE6;font-size:11px;letter-spacing:.08em;">记忆编译管线</p>'
            '<h2 style="color:#16181F;line-height:1.25;">%s</h2>'
            '<p>%s</p>'
            '<p style="background:#F0EEFF;padding:11px 13px;border-radius:8px;">%s</p>'
            '<p style="color:#747887;font-size:11px;">L0 原始会话始终保留；上层内容不会覆盖证据。</p>'
            '</div>' % (_html(title), _html(purpose), _html(state))
        )

    def _populate_task_sources(self, task: Dict[str, Any]) -> None:
        source_names = {"codex": "Codex", "claude-code": "Claude Code"}
        primary_id = str(task.get("primary_conversation_id") or "")
        seen: set[str] = set()
        self.task_source_selector.blockSignals(True)
        self.task_source_selector.clear()
        members = [
            (summary, False) for summary in task.get("source_copies", [])
        ] + [
            (summary, True) for summary in task.get("subagents", [])
        ]
        for summary, is_subagent in members:
            conversation_id = str(summary.get("conversation_id") or "")
            if not conversation_id or conversation_id in seen:
                continue
            seen.add(conversation_id)
            source = source_names.get(
                str(summary.get("source_kind") or ""),
                str(summary.get("source_kind") or "Agent"),
            )
            if conversation_id == primary_id:
                label = "%s · 主会话" % source
            elif is_subagent:
                nickname = str(summary.get("agent_nickname") or "").strip()
                label = "%s · 执行分支%s" % (
                    source, " · %s" % nickname if nickname else ""
                )
            else:
                label = "%s · 其他来源" % source
            label += " · %d 轮" % int(summary.get("readable_message_count") or 0)
            self.task_source_selector.addItem(label, conversation_id)
        if primary_id and primary_id not in seen:
            self.task_source_selector.insertItem(0, "主会话", primary_id)
        primary_index = self.task_source_selector.findData(primary_id)
        self.task_source_selector.setCurrentIndex(max(0, primary_index))
        self.task_source_selector.blockSignals(False)
        self.task_source_bar.setVisible(self.task_source_selector.count() > 0)

    def _show_task_source(self) -> None:
        if (
            self.workspace_tabs.currentIndex() != 0
            or self.current_task_layer != 0
            or not self.project_path
            or not self.current_task_detail
        ):
            return
        conversation_id = str(self.task_source_selector.currentData() or "")
        if not conversation_id:
            return
        try:
            if conversation_id == str(self.current_task_detail.get("primary_conversation_id")):
                conversation = self.current_task_detail["primary_conversation"]
            else:
                conversation = self.service.conversation(
                    self.project_path, conversation_id, redact=True
                )
            self._render_task_conversation(conversation)
        except Exception as exc:
            self._show_error("读取会话来源失败", exc)

    def _render_task_conversation(self, conversation: Dict[str, Any]) -> None:
        task = self.current_task_detail or {}
        messages = []
        for message in conversation.get("messages", []):
            is_user = message["role"] == "user"
            speaker = "你" if is_user else "Agent"
            background = "#F0EEFF" if is_user else "#F8F8FA"
            border = "#D8D4FF" if is_user else "#DDE0E8"
            messages.append(
                '<table width="100%%" cellspacing="0" cellpadding="0" style="margin:0 0 14px 0;">'
                '<tr><td style="color:#747887;font-size:11px;padding:0 0 5px 2px;">%s · #%d</td></tr>'
                '<tr><td style="background:%s;border:1px solid %s;border-radius:8px;'
                'padding:11px 13px;line-height:1.6;">%s</td></tr></table>'
                % (_html(speaker), message["sequence"], background, border, _html(message["text"]))
            )
        source_names = {"codex": "Codex", "claude-code": "Claude Code"}
        source_line = " + ".join(
            source_names.get(value, str(value)) for value in task.get("sources", [])
        )
        coverage = float(task.get("primary_coverage", 1.0) or 0.0)
        has_gap = bool(task.get("has_content_gap", coverage < 0.95))
        warning = ""
        if has_gap:
            severe = coverage < 0.80
            warning = (
                '<p style="background:%s;color:%s;padding:9px 11px;"><b>%s</b><br>'
                '主会话覆盖约 %.0f%% 的可读内容。%s</p>'
                % (
                    "#FFF6E8", "#8B5D1C", "完整内容分散在多个来源" if severe else "其他来源存在差异",
                    coverage * 100,
                    "请使用上方“查看来源”逐一查看，不要只依赖主会话。"
                    if severe else "可从上方切换查看差异内容。",
                )
            )
        archive_note = _html(task.get("merge_reason") or "原始会话完整保留")
        self.detail.setHtml(
            '<p style="color:#5A4FE6;font-size:11px;letter-spacing:.08em;">L0 · 原始会话</p>'
            '<h2 style="line-height:1.25;color:#16181F;">%s</h2>'
            '<p style="color:#747887;font-size:11px;">%s · %d 轮唯一对话 · '
            '%d 份来源 · %d 个执行分支</p>%s'
            '<p style="background:#EAF7F1;color:#176B4D;padding:8px 10px;">%s</p>'
            '<p style="background:#F0EEFF;color:#4036B3;padding:8px 10px;">'
            '切换到“L1 原子记忆”，查看这项任务形成了哪些可追溯判断。</p>'
            '<hr style="border:none;border-top:1px solid #DDE0E8;">%s'
            % (
                _html(task.get("title", conversation.get("title", "Agent 任务"))),
                _html(source_line), int(task.get("unique_readable_message_count", task.get("readable_message_count", 0))),
                int(task.get("source_copy_count", 1)), int(task.get("subagent_count", 0)),
                warning, archive_note,
                "".join(messages) or "<p>该会话没有可读消息。</p>",
            )
        )

    def _workspace_changed(self, index: int) -> None:
        self._switch_workspace(index)
        if index == 0:
            self.show_conversation()
        else:
            self.show_selected()

    def _set_source_busy(self, busy: bool, message: str = "") -> None:
        self.source_busy = busy
        self.codex_import_button.setEnabled(not busy)
        self.extract_button.setEnabled(not busy)
        self.import_action.setEnabled(not busy)
        if message:
            self.statusBar().showMessage(message)

    def _run_source_operation(self, operation: str, callback: Any) -> None:
        if self.source_busy:
            return
        self._set_source_busy(True, "正在处理 Agent 会话……")

        def worker() -> None:
            try:
                self.source_operation_finished.emit(operation, callback())
            except Exception as exc:
                self.source_operation_failed.emit(operation, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def import_codex_conversations(self) -> None:
        try:
            preview = self.service.conversation_sources_preview()
        except Exception as exc:
            self._show_error("无法检查 Agent 会话", exc)
            return
        if not preview["found"]:
            QMessageBox.information(self, "同步对话", "没有找到 Codex 或 Claude Code 会话文件。")
            return
        sources = preview.get("by_source", {})
        codex_count = int(sources.get("codex", {}).get("found", 0))
        claude_count = int(sources.get("claude-code", {}).get("found", 0))
        answer = QMessageBox.question(
            self,
            "建立原始会话档案",
            "找到 %d 段 Codex 会话、%d 段 Claude Code 主会话，共 %s。\n\n"
            "QMemory 会逐字节保留原始 JSONL，另建立可读消息索引。"
            "档案只存本机、不进入设备同步目录，但原文可能含敏感信息。"
            % (codex_count, claude_count, _format_bytes(preview["total_bytes"])),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.app_settings.setValue("agent_archive_enabled", True)
        self._run_source_operation("archive", self.service.conversation_sources_sync)

    def auto_archive_codex(self) -> None:
        self._run_source_operation("archive-auto", self.service.conversation_sources_sync)

    def _auto_extract_changed(self, enabled: bool) -> None:
        self.app_settings.setValue("auto_extract_enabled", enabled)
        if enabled:
            QTimer.singleShot(500, self._auto_extract_next)

    def _auto_extract_next(self) -> None:
        if self.source_busy or not self.auto_extract.isChecked():
            return
        if not any(
            project["sync_enabled"] and project["pending_count"]
            for project in self.service.projects()
        ):
            return
        self._run_source_operation(
            "extract-auto-all",
            lambda: CodexInsightExtractor(self.service).extract_pending_all(1),
        )

    def extract_codex_insights(self) -> None:
        if not self.project_path:
            return
        if not self.current_conversations:
            QMessageBox.information(self, "提炼洞察", "请先同步 Agent 原始会话。")
            return
        answer = QMessageBox.question(
            self,
            "从会话提炼候选洞察",
            "Codex 将读取本项目已脱敏的会话消息，提取决定、约束、事实与交接。\n\n"
            "新洞察都是“待确认”，并保留到原会话消息的引用。继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        project = self.project_path
        self._run_source_operation(
            "extract", lambda: CodexInsightExtractor(self.service).extract_pending(project, 10)
        )

    def _source_operation_done(self, operation: str, result: object) -> None:
        self._set_source_busy(False)
        data = result if isinstance(result, dict) else {}
        if operation == "memorycore-layer":
            task_id = str(data.get("task_id") or "")
            if task_id:
                self.memorycore_layers[task_id] = data
            self.memorycore_badge.setText("MemoryCore · 已连接")
            if task_id == str((self.current_task_detail or {}).get("task_id") or ""):
                if self.current_task_layer == 1:
                    self._render_task_memories()
                elif self.current_task_layer in (2, 3):
                    self._render_memorycore_layer(self.current_task_layer)
            return
        if operation == "memorycore-sync":
            self.memorycore_badge.setText(
                "MemoryCore · %s" % (
                    "新增 %d 条" % int(data.get("sent_messages", 0))
                    if int(data.get("sent_messages", 0)) else "已是最新"
                )
            )
            self.memorycore_layers.clear()
            self.statusBar().showMessage(
                "MemoryCore 已加工同步 · %d 个批次 · %d 条消息"
                % (int(data.get("sent_batches", 0)), int(data.get("sent_messages", 0))),
                6000,
            )
            return
        if operation.startswith("archive"):
            self._refresh_project_selector()
            self.refresh_conversations()
            project_count = int(data.get("projects", len(self.service.projects())))
            imported = int(data.get("imported", 0))
            self.archive_status_badge.setText(
                "自动归档 · %s" % ("新增 %d 项" % imported if imported else "已是最新")
            )
            self.archive_status_badge.setToolTip(
                "%s 完成 Agent 会话扫描" % datetime.now().astimezone().strftime("%m月%d日 %H:%M")
            )
            self.statusBar().showMessage(
                "已扫描 %d 个项目 · 更新 %d 段会话 · 索引 %d 条消息"
                % (
                    project_count,
                    int(data.get("imported", 0)),
                    int(data.get("messages_indexed", 0)),
                ),
                6000,
            )
            if self.auto_extract.isChecked():
                QTimer.singleShot(800, self._auto_extract_next)
            if self.memorycore_config.enabled:
                QTimer.singleShot(1200, self._auto_sync_memorycore)
        else:
            self.refresh_memories()
            self.refresh_conversations()
            self.workspace_tabs.setCurrentIndex(1)
            self.statusBar().showMessage(
                "已整理 %d 个项目、%d 段会话 · 提出 %d 条候选洞察"
                % (
                    int(data.get("projects", 1 if data.get("conversations") else 0)),
                    int(data.get("conversations", 0)),
                    int(data.get("proposals", 0)),
                ),
                7000,
            )
            if self.auto_extract.isChecked() and not int(data.get("failures", 0)):
                QTimer.singleShot(3000, self._auto_extract_next)

    def _source_operation_error(self, operation: str, message: str) -> None:
        self._set_source_busy(False)
        if operation.startswith("memorycore"):
            self.memorycore_badge.setText("MemoryCore · 需要处理")
            self.memorycore_badge.setToolTip(message)
            self.statusBar().showMessage("MemoryCore：%s" % message, 8000)
            return
        if operation.startswith("archive"):
            self.archive_status_badge.setText("自动归档 · 需要处理")
            self.archive_status_badge.setToolTip(message)
        self._show_error(
            "同步失败" if operation.startswith("archive") else "提炼失败",
            RuntimeError(message),
        )

    def _auto_sync_memorycore(self) -> None:
        if not self.memorycore_config.enabled or self.source_busy:
            return
        self.memorycore_badge.setText("MemoryCore · 正在同步")
        self._run_source_operation(
            "memorycore-sync", self.memorycore_bridge.sync_all
        )

    def choose_sync_directory(self) -> None:
        start = str(self.app_settings.value("sync_path", "") or Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选择记忆同步目录", start)
        if chosen:
            self.app_settings.setValue("sync_path", chosen)
            self.sync_path_label.setText(chosen)
            self.auto_sync()

    def auto_sync(self) -> None:
        sync_path = str(self.app_settings.value("sync_path", ""))
        if not sync_path or self.sync_busy:
            return
        directory = Path(sync_path)
        self.sync_busy = True
        self.sync_now_button.setEnabled(False)
        self.statusBar().showMessage("正在同步设备记忆……")
        def worker() -> None:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                self.device_sync_finished.emit(
                    str(directory),
                    {
                        "pulled": self.service.sync_pull(directory),
                        "pushed": self.service.sync_push(directory),
                    },
                )
            except Exception as exc:
                self.device_sync_failed.emit(str(directory), str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _device_sync_done(self, directory: str, result: Dict[str, Any]) -> None:
        pulled = result.get("pulled", {})
        imported = int(pulled.get("imported_events", 0))
        self.sync_path_label.setText(
            "%s\n上次同步成功 · 导入 %d 条" % (directory, imported)
        )
        self.statusBar().showMessage("同步完成 · 导入 %d 条" % imported, 5000)
        self.sync_busy = False
        self.sync_now_button.setEnabled(True)
        self.refresh_all()

    def _device_sync_error(self, directory: str, message: str) -> None:
        self.sync_path_label.setText("%s\n同步需要处理：%s" % (directory, message))
        self.statusBar().showMessage("同步失败", 5000)
        self.sync_busy = False
        self.sync_now_button.setEnabled(True)
        self._notify("QMemory 同步失败", message)

    def run_doctor(self) -> None:
        try:
            result = self.service.doctor()
            detail = json.dumps(result, ensure_ascii=False, indent=2)
            QMessageBox.information(
                self,
                "健康检查",
                ("所有检查通过。\n\n" if result["ok"] else "发现需要处理的问题。\n\n") + detail,
            )
            self.refresh_all()
        except Exception as exc:
            self._show_error("健康检查失败", exc)

    def configure_connections(self) -> None:
        dialog = ConnectionDialog(self.memorycore_config_path, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.open_agent_setup:
            self.configure_codex()
            return
        self.memorycore_config = dialog.config
        self.memorycore_bridge = MemoryCoreBridge(self.service, self.memorycore_config)
        self.memorycore_layers.clear()
        self.memorycore_badge.setText(
            "MemoryCore · %s" % ("已启用" if self.memorycore_config.enabled else "未启用")
        )
        if self.memorycore_config.enabled:
            QTimer.singleShot(100, self._auto_sync_memorycore)

    def configure_codex(self) -> None:
        executable = self._packaged_executable()
        if executable is None:
            source_executable = Path(sys.executable).with_name("qmemory")
            command = shlex.quote(str(source_executable))
            QMessageBox.information(
                self,
                "连接 Agent",
                "当前是源码运行模式。请先使用打包后的 QMemory.app，或继续使用：\n\n"
                "codex mcp add qmemory -- %s mcp\n" % command
                + "claude mcp add --scope user qmemory -- %s mcp" % command,
            )
            return
        codex = self._find_codex()
        claude = self._find_claude()
        codex_command = [str(codex or "codex"), "mcp", "add", "qmemory", "--", str(executable), "--mcp"]
        claude_command = [
            str(claude or "claude"), "mcp", "add", "--scope", "user", "qmemory", "--",
            str(executable), "--mcp",
        ]
        command_text = "\n".join(
            " ".join(shlex.quote(part) for part in command)
            for command in (codex_command, claude_command)
        )
        dialog = QMessageBox(self)
        dialog.setWindowTitle("连接 Agent")
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setText("把 QMemory 连接到 Codex 与 Claude Code？")
        dialog.setInformativeText(
            "这会修改当前用户的 Codex 与 Claude Code MCP 配置，"
            "让两个 Agent 读写同一个 QMemory。"
        )
        dialog.setDetailedText(command_text)
        connect_button = dialog.addButton(
            "连接两个 Agent",
            QMessageBox.ButtonRole.AcceptRole,
        )
        copy_button = dialog.addButton("复制命令", QMessageBox.ButtonRole.ActionRole)
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked == copy_button:
            QApplication.clipboard().setText(command_text)
            self.statusBar().showMessage("Agent 配置命令已复制", 4000)
            return
        if clicked != connect_button:
            return
        if codex is None or claude is None:
            QApplication.clipboard().setText(command_text)
            QMessageBox.warning(
                self,
                "Agent CLI 不完整",
                "没有同时找到 Codex 和 Claude Code CLI。配置命令已复制。",
            )
            return
        try:
            existing_state = self.codex_state.get("state")
            if existing_state not in ("not_connected", "codex_missing", "error", None):
                remove_result = run_text(
                    [str(codex), "mcp", "remove", "qmemory"],
                    stderr=subprocess.STDOUT,
                    timeout=30,
                )
                if remove_result.returncode != 0:
                    raise RuntimeError(remove_result.stdout.strip() or "无法移除旧连接")
            result = run_text(
                codex_command,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            output = result.stdout.strip()
            if result.returncode != 0:
                raise RuntimeError(output or "Codex CLI 返回未知错误")
            if self.claude_state.get("state") != "not_connected":
                run_text(
                    [str(claude), "mcp", "remove", "--scope", "user", "qmemory"],
                    stderr=subprocess.STDOUT,
                    timeout=30,
                )
            claude_result = run_text(
                claude_command,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            if claude_result.returncode != 0:
                raise RuntimeError(claude_result.stdout.strip() or "Claude Code CLI 返回未知错误")
            self._refresh_codex_state()
            self._refresh_setup_state()
            QMessageBox.information(
                self,
                "连接完成",
                "QMemory 已加入 Codex 与 Claude Code。请重启两个 Agent 客户端。",
            )
        except Exception as exc:
            QApplication.clipboard().setText(command_text)
            self._show_error("连接 Agent 失败（配置命令已复制）", exc)

    @staticmethod
    def _app_bundle() -> Optional[Path]:
        return find_app_bundle(Path(sys.executable))

    @classmethod
    def _packaged_executable(cls) -> Optional[Path]:
        bundle = cls._app_bundle()
        return bundle_executable(bundle) if bundle else None

    @staticmethod
    def _find_codex() -> Optional[Path]:
        candidates = [
            Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
            Path.home() / ".local" / "bin" / "codex",
        ]
        path_value = shutil_which("codex")
        if path_value:
            candidates.insert(0, Path(path_value))
        return next((candidate for candidate in candidates if candidate.exists()), None)

    @staticmethod
    def _find_claude() -> Optional[Path]:
        candidates = [
            Path.home() / ".npm-global" / "bin" / "claude",
            Path("/opt/homebrew/bin/claude"),
            Path("/usr/local/bin/claude"),
        ]
        path_value = shutil_which("claude")
        if path_value:
            candidates.insert(0, Path(path_value))
        return next((candidate for candidate in candidates if candidate.exists()), None)

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(64), self)
        menu = QMenu()
        show_action = menu.addAction("打开 QMemory")
        show_action.triggered.connect(self.show_from_tray)
        sync_action = menu.addAction("立即同步")
        sync_action.triggered.connect(self.auto_sync)
        menu.addSeparator()
        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self.quit_application)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.show_from_tray()
            if reason == QSystemTrayIcon.ActivationReason.Trigger
            else None
        )
        self.tray.setToolTip(APP_NAME)
        self.tray.show()

    def show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _notify(self, title: str, message: str) -> None:
        if self.tray:
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 4000)

    def _show_error(self, title: str, error: Exception) -> None:
        self.statusBar().showMessage(str(error), 5000)
        QMessageBox.critical(self, title, str(error))

    def quit_application(self) -> None:
        self.allow_close = True
        self.app_settings.setValue("window_geometry", self.saveGeometry())
        if self.tray:
            self.tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event: Any) -> None:
        self.app_settings.setValue("window_geometry", self.saveGeometry())
        if self.tray and not self.allow_close:
            event.ignore()
            self.hide()
            self._notify(APP_NAME, "QMemory 仍在菜单栏运行")
        else:
            event.accept()


def _html(value: Any) -> str:
    import html

    return html.escape(str(value)).replace("\n", "<br>")


def _compact(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _format_bytes(value: int) -> str:
    amount = float(value)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (amount, unit)
        amount /= 1024
    return "%d B" % value


def _friendly_time(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%m月%d日 %H:%M")
    except ValueError:
        return value[:16]


def shutil_which(command: str) -> Optional[str]:
    return shutil.which(command)


STYLE_SHEET = """
QMainWindow, QWidget {
    background: #F4F5F8;
    color: #16181F;
    font-family: "Noto Sans SC", "SF Pro Text";
    font-size: 13px;
}
QLabel { background: transparent; }
#contentCanvas { background: #F4F5F8; }
#sidebar { background: #FCFCFE; border-right: 1px solid #E1E3EA; }
#sidebarBrand {
    color: #16181F; font-family: "Space Grotesk", "Noto Sans SC";
    font-size: 22px; font-weight: 650;
}
#sidebarPromise { color: #7A7E8C; font-size: 11px; }
#sidebarMeta {
    color: #747887; border-top: 1px solid #E5E7EE; padding: 10px 2px 3px 2px;
    font-family: "JetBrains Mono", "Noto Sans SC"; font-size: 10px;
}
#navButton {
    background: transparent; color: #505462; border: none; border-radius: 8px;
    padding: 10px 12px; text-align: left;
}
#navButton:hover { background: #F0F1F6; color: #16181F; }
#navButton[selected="true"] { background: #EEECFF; color: #4A40CC; font-weight: 650; }
#sidebarAction {
    background: #16181F; color: #FFFFFF; border: 1px solid #16181F;
    border-radius: 8px; padding: 9px 10px;
}
#sidebarAction:hover { background: #323540; }
#pageTitle {
    font-family: "Space Grotesk", "Noto Sans SC"; font-size: 28px;
    font-weight: 600; color: #16181F;
}
#subtitle, #muted { color: #747887; }
#sectionTitle {
    font-family: "Space Grotesk", "Noto Sans SC"; font-size: 16px;
    font-weight: 600; color: #16181F;
}
#dialogTitle {
    font-family: "Space Grotesk", "Noto Sans SC"; font-size: 26px;
    font-weight: 600; color: #16181F;
}
#metricCard { background: #FFFFFF; border: 1px solid #E1E3EA; border-radius: 12px; }
#metricValue {
    color: #16181F; font-family: "Space Grotesk", "Noto Sans SC";
    font-size: 23px; font-weight: 600;
}
#metricLabel { color: #747887; font-size: 11px; }
#pipelineStatus {
    font-family: "JetBrains Mono", "Noto Sans SC"; color: #626574;
    background: #F7F7FA; border: 1px solid #E5E7EE; border-radius: 8px;
    padding: 9px 11px;
}
#setupPanel {
    background: #F0EEFF; border: 1px solid #D8D4FF; border-radius: 12px;
}
#setupTitle { color: #2E286E; font-size: 15px; font-weight: 650; }
#setupDetail { color: #625D8D; }
#setupStep { color: #817DA0; padding: 4px 0; }
#setupStep[done="true"] { color: #0E8560; }
#setupAction, #primaryButton {
    background: #5A4FE6; color: #FFFFFF; border: 1px solid #5A4FE6;
    border-radius: 8px; font-weight: 650; padding: 9px 14px;
}
#setupAction:hover, #primaryButton:hover { background: #4438C4; color: #FFFFFF; }
#projectBar { background: #FFFFFF; border: 1px solid #E1E3EA; border-radius: 12px; }
#panel { background: #FFFFFF; border: 1px solid #E1E3EA; border-radius: 12px; }
#syncBox { background: #F7F7FA; border: 1px solid #E5E7EE; border-radius: 10px; }
#projectLabel { font-size: 15px; font-weight: 650; }
#projectAutoState, #projectState {
    padding: 4px 9px; border-radius: 999px; color: #0E8560;
    background: #EAF7F1; font-size: 11px;
}
#projectAutoState[state="paused"], #projectState[projectState="paused"] { color: #8B5D1C; background: #FFF6E8; }
#projectState[projectState="ignored"] { color: #747887; background: #EFF0F4; }
#projectDetail { background: #F7F7FA; border: none; border-radius: 8px; }
#healthBadge {
    padding: 7px 11px; border-radius: 999px; background: #FFFFFF;
    color: #626574; border: 1px solid #D5D8E1;
}
#archiveStatusBadge {
    padding: 7px 11px; border-radius: 999px; color: #0E8560;
    background: #EAF7F1; margin-right: 6px;
}
#memorycoreBadge {
  color: #5148C8; border: 1px solid #C8C3FF; border-radius: 8px;
  padding: 6px 10px; background: #F4F2FF;
}
#healthBadge[state="ok"] { color: #0E8560; border-color: #8DD3BB; }
#healthBadge[state="warning"] { color: #8B5D1C; border-color: #E0BA7A; }
#layerSwitch { background: #F3F3F7; border: 1px solid #E1E3EA; border-radius: 10px; }
#layerButton {
    background: transparent; color: #747887; border: none; border-radius: 7px;
    padding: 8px 6px; font-size: 11px;
}
#layerButton:hover { color: #4A40CC; background: #F0EEFF; }
#layerButton[selected="true"] { color: #FFFFFF; background: #5A4FE6; font-weight: 650; }
#viewModeButton {
    background: transparent; color: #747887; border: 1px solid #D5D8E1;
    border-radius: 7px; padding: 6px 12px;
}
#viewModeButton[selected="true"] {
    color: #FFFFFF; background: #5A4FE6; border-color: #5A4FE6; font-weight: 650;
}
#memoryGraphCanvas {
    background: #FBFBFD; border: 1px solid #E1E3EA; border-radius: 10px;
}
#graphLegend {
    color: #747887; font-family: "JetBrains Mono", "Noto Sans SC"; font-size: 10px;
}
#graphNode {
    background: #FFFFFF; color: #363946; border: 1px solid #D5D8E1;
    border-radius: 9px; padding: 5px 8px; font-size: 11px; text-align: center;
}
#graphNode:hover { border-color: #7168EA; color: #4036B3; background: #F8F7FF; }
#graphNode[nodeKind="project"] {
    background: #F0EEFF; color: #2E286E; border: 2px solid #7168EA; font-weight: 650;
}
#graphNode[nodeKind="task"][selected="true"] {
    background: #F0EEFF; color: #4036B3; border: 2px solid #7168EA; font-weight: 650;
}
#graphNode[nodeKind="codex"] {
    background: #ECFAFC; color: #08778A; border-color: #79D3DF;
}
#graphNode[nodeKind="claude"] {
    background: #F4EFFF; color: #6940A8; border-color: #B79AE4;
}
#graphNode[nodeKind="branch"] {
    background: #F7F7FA; color: #626574; border-color: #D5D8E1;
}
#graphNode[nodeKind="layer"][selected="true"] {
    background: #5A4FE6; color: #FFFFFF; border: 2px solid #5A4FE6; font-weight: 650;
}
QTabWidget::pane { border: none; background: transparent; }
QTabBar::tab { background: transparent; border: none; }
QLineEdit, QComboBox, QTextEdit, QTextBrowser {
    background: #FFFFFF; border: 1px solid #D5D8E1; border-radius: 8px;
    padding: 8px 10px; selection-background-color: #D8D4FF;
}
QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QTextBrowser:focus { border: 1px solid #7168EA; }
QListWidget {
    background: transparent; border: none; border-top: 1px solid #E1E3EA;
    border-radius: 0; padding: 0; outline: none;
}
QListWidget::item { background: transparent; border: none; border-bottom: 1px solid #E8EAF0; padding: 0; margin: 0; }
QListWidget::item:selected { background: #F0EEFF; border-left: 3px solid #5A4FE6; color: #16181F; }
#typeChip, #statusChip, #sourceChip {
    padding: 2px 7px; border-radius: 999px;
    font-family: "JetBrains Mono", "Noto Sans SC"; font-size: 10px;
}
#typeChip, #sourceChip { background: #FFFFFF; color: #626574; border: 1px solid #D5D8E1; }
#mergedHint { color: #4A40CC; font-size: 11px; }
#analysisBox { background: #F7F7FA; border: 1px solid #E5E7EE; border-radius: 8px; }
#statusChip { background: #EAF7F1; color: #0E8560; }
#statusChip[memoryStatus="proposed"] { background: #FFF6E8; color: #9A651B; }
#statusChip[memoryStatus="superseded"], #statusChip[memoryStatus="archived"] { background: #EFF0F4; color: #747887; }
#cardStatement { color: #16181F; font-size: 14px; font-weight: 600; }
#dialogIntro { color: #747887; font-size: 13px; }
#fieldTitle { font-size: 15px; font-weight: 650; color: #16181F; }
QGroupBox { color: #626574; border: 1px solid #D5D8E1; border-radius: 8px; margin-top: 10px; padding-top: 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QPushButton {
    background: #FFFFFF; color: #363946; border: 1px solid #C8CBD5;
    border-radius: 8px; padding: 8px 13px;
}
QPushButton:hover { color: #4A40CC; border-color: #7168EA; background: #F8F7FF; }
QPushButton:disabled { color: #A8ABB5; background: #F1F2F5; border-color: #E1E3EA; }
QSplitter::handle { background: transparent; width: 12px; }
QStatusBar { background: #F4F5F8; border-top: 1px solid #E1E3EA; color: #747887; }
"""


def main() -> int:
    force_utf8_streams()
    QApplication.setOrganizationName(ORG_NAME)
    QApplication.setApplicationName(APP_NAME)
    application = QApplication(sys.argv)
    application.setQuitOnLastWindowClosed(False)
    application.setWindowIcon(app_icon())
    window = MainWindow()
    window.show()
    if "--smoke-test" in sys.argv:
        QTimer.singleShot(1200, window.quit_application)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
