"""Inline website timeline and grouped, theme-aware browser settings."""
from datetime import date, timedelta
from pathlib import Path
from math import ceil
import re
import sys

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QToolTip,
    QScrollArea, QVBoxLayout, QWidget)

from core import config, site_categories, site_storage, storage
from browser.bridge import pairing_token


def _theme():
    # Import after QApplication exists, and read current theme rather than caching colors.
    from ui import dashboard
    return dashboard


def _label(text, secondary=False, size=10):
    t = _theme()
    label = QLabel(text)
    label.setFont(t._font(size))
    label.setWordWrap(True)
    label.setTextFormat(Qt.PlainText)
    label.setStyleSheet(f"color:{t.GRAY if secondary else t.TEXT};background:transparent;")
    return label


def _styles():
    t = _theme()
    return (
        f"QLabel{{color:{t.TEXT};background:transparent;}}"
        f"QLineEdit,QComboBox{{background:{t.BG};color:{t.TEXT};border:1px solid {t.BORDER};"
        "border-radius:8px;padding:8px 10px;selection-background-color:"
        f"{t.ACCENT};selection-color:white;}}"
        f"QLineEdit:focus,QComboBox:focus{{border-color:{t.ACCENT};}}"
        f"QComboBox QAbstractItemView{{background:{t.CARD};color:{t.TEXT};"
        f"selection-background-color:{t.ACCENT};selection-color:white;}}"
        f"QCheckBox{{color:{t.TEXT};spacing:10px;padding:5px 0;}}"
        f"QCheckBox::indicator{{width:17px;height:17px;border:2px solid {t.BORDER};"
        f"border-radius:5px;background:{t.BG};}}"
        f"QCheckBox::indicator:checked{{background:{t.ACCENT};border-color:{t.ACCENT};}}"
        f"QPushButton{{background:{t.BG};color:{t.TEXT};border:1px solid {t.BORDER};"
        "border-radius:8px;padding:7px 12px;}"
        f"QPushButton:hover{{background:{t.HOVER};}}"
    )


class _WebsiteWeekChart(QWidget):
    """Seven daily columns, stacked by website; hover exposes each segment."""

    _COLORS = ("#8CB9E8", "#A7D8C5", "#F4C69B", "#C7B6E8",
               "#E8AEBB", "#A8D4D8", "#DBCF9D", "#B6C8EA")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(190)
        self.setMouseTracking(True)
        self._days = []
        self._colors = {}
        self._segments = []
        self._columns = []
        self._hovered = None

    def set_data(self, start, end, rows):
        by_day = {}
        totals = {}
        for row in rows:
            seconds = max(0, float(row["total_seconds"]))
            if not seconds:
                continue
            host = row["host"]
            by_day.setdefault(str(row["date"]), []).append((host, seconds))
            totals[host] = totals.get(host, 0) + seconds
        hosts = sorted(totals, key=lambda host: (-totals[host], host))
        self._colors = {host: QColor(self._COLORS[i % len(self._COLORS)])
                        for i, host in enumerate(hosts)}
        self._days = []
        day = start
        while day <= end:
            items = sorted(by_day.get(day.isoformat(), []),
                           key=lambda item: (-totals[item[0]], item[0]))
            self._days.append((day, items))
            day += timedelta(days=1)
        self._hovered = None
        self.update()

    def _hit(self, point):
        for rect, day, host, seconds in self._segments:
            if rect.contains(point):
                return (day, host, seconds)
        for rect, day, items in self._columns:
            if rect.contains(point):
                return (day, None, items)
        return None

    def _tooltip(self, hit):
        day, host, detail = hit
        heading = f"日期：{day:%Y年%m月%d日}"
        if host is not None:
            return f"{heading}\n网站：{host}\n用时：{storage.format_duration_short(detail)}"
        if not detail:
            return f"{heading}\n暂无网站记录"
        lines = [heading, f"合计 {storage.format_duration_short(sum(s for _, s in detail))}"]
        lines.extend(f"{site} · {storage.format_duration_short(seconds)}"
                     for site, seconds in detail)
        return "\n".join(lines)

    def mouseMoveEvent(self, event):
        hit = self._hit(event.position())
        if hit != self._hovered:
            self._hovered = hit
            self.update()
        if hit:
            QToolTip.showText(event.globalPosition().toPoint(), self._tooltip(hit), self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, event):
        self._hovered = None
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        t = _theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._segments = []
        self._columns = []
        if not self._days:
            painter.end()
            return
        left, right, top, bottom = 43, 9, 16, 30
        plot_w = max(1, self.width() - left - right)
        plot_h = max(1, self.height() - top - bottom)
        baseline = top + plot_h
        maximum = max((sum(seconds for _, seconds in items)
                       for _, items in self._days), default=0)
        tick = 60 if maximum <= 900 else (900 if maximum <= 3600 else 3600)
        scale = max(tick, ceil(maximum / tick) * tick)
        painter.setFont(t._font(8))
        for fraction in (0, .5, 1):
            y = baseline - round(plot_h * fraction)
            painter.setPen(QPen(QColor(t.BORDER), 1, Qt.DotLine))
            painter.drawLine(left, y, self.width() - right, y)
            painter.setPen(QColor(t.GRAY))
            painter.drawText(0, y - 8, left - 6, 16, Qt.AlignRight | Qt.AlignVCenter,
                             storage.format_duration_short(scale * fraction))
        step = plot_w / len(self._days)
        bar_w = min(36, max(12, step * .56))
        for index, (day, items) in enumerate(self._days):
            x = left + step * (index + .5) - bar_w / 2
            self._columns.append((QRectF(x, top, bar_w, plot_h), day, items))
            y = float(baseline)
            for host, seconds in items:
                height = seconds / scale * plot_h
                y -= height
                rect = QRectF(x, y, bar_w, height)
                color = QColor(self._colors[host])
                if self._hovered and self._hovered[:2] == (day, host):
                    color = color.lighter(115)
                painter.setPen(Qt.NoPen)
                painter.setBrush(color)
                painter.drawRect(rect)
                self._segments.append((rect, day, host, seconds))
            painter.setPen(QColor(t.TEXT if items else t.GRAY))
            painter.drawText(round(left + step * index), baseline + 5,
                             round(step), 18, Qt.AlignCenter, f"{day:%m/%d}")
        painter.end()


class _WebsiteCategoryPie(QWidget):
    """Local domain classification; shares the mail palette and never inspects URLs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(188)
        self.setMouseTracking(True)
        self._values = []
        self._wedges = []

    def set_data(self, rows):
        totals = site_categories.totals(rows)
        self._values = [(name, seconds, color) for name, color in site_categories.CATEGORIES
                        if (seconds := totals.get(name, 0)) > 0]
        self.update()

    def _hit(self, point):
        import math
        for center_x, center_y, radius, begin, end, name, seconds, total in self._wedges:
            dx, dy = point.x() - center_x, point.y() - center_y
            if dx * dx + dy * dy > radius * radius:
                continue
            angle = math.degrees(math.atan2(-dy, dx)) % 360
            if begin <= angle < end or end > 360 and angle < end - 360:
                return name, seconds, total
        return None

    def mouseMoveEvent(self, event):
        hit = self._hit(event.position())
        if hit:
            name, seconds, total = hit
            QToolTip.showText(event.globalPosition().toPoint(),
                              f"类型：{name}\n用时：{storage.format_duration_short(seconds)}\n占比：{seconds / total:.1%}",
                              self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)

    def paintEvent(self, event):
        import math
        t = _theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._wedges = []
        if not self._values:
            painter.setPen(QColor(t.GRAY))
            painter.drawText(self.rect(), Qt.AlignCenter, "暂无可分类的网站记录")
            painter.end()
            return
        total = sum(seconds for _, seconds, _ in self._values)
        diameter = min(158, self.height() - 24, max(90, self.width() * .32))
        x, y = 6.0, (self.height() - diameter) / 2
        rect = QRectF(x, y, diameter, diameter)
        center_x, center_y, radius = x + diameter / 2, y + diameter / 2, diameter / 2
        angle = 90.0
        for name, seconds, color in self._values:
            sweep = seconds / total * 360
            base = QColor(color)
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0, base.lighter(116))
            gradient.setColorAt(1, base)
            painter.setBrush(gradient)
            painter.setPen(QPen(QColor(t.BG), 2))
            painter.drawPie(rect, round((angle % 360) * 16), round(sweep * 16))
            self._wedges.append((center_x, center_y, radius, angle % 360,
                                 angle % 360 + sweep, name, seconds, total))
            angle += sweep
        painter.setPen(QColor(t.TEXT))
        painter.setFont(t._font(9))
        legend_x = round(x + diameter + 20)
        columns = 2 if self.width() - legend_x >= 350 else 1
        col_width = max(140, (self.width() - legend_x) // columns)
        for index, (name, seconds, color) in enumerate(self._values):
            col, row = index % columns, index // columns
            lx, ly = legend_x + col * col_width, 28 + row * 36
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(lx, ly - 9, 11, 11, 4, 4)
            painter.setPen(QColor(t.TEXT))
            painter.drawText(lx + 17, ly - 13, col_width - 20, 18,
                             Qt.AlignLeft | Qt.AlignVCenter, f"{name}  {seconds / total:.0%}")
            painter.setPen(QColor(t.GRAY))
            painter.drawText(lx + 17, ly + 3, col_width - 20, 17,
                             Qt.AlignLeft | Qt.AlignVCenter,
                             storage.format_duration_short(seconds))
        painter.end()


class WebsiteUsagePanel(QWidget):
    """Dashboard-owned view: date navigation comes from the dashboard, never a popup."""

    def __init__(self, parent=None):
        super().__init__(parent)
        t = _theme()
        self.setStyleSheet(_styles())
        self._range = None
        self._timeline_day = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        header = QHBoxLayout()
        self.title = _label("网站使用时间线", size=11)
        self.title.setFont(t._font(11, True))
        header.addWidget(self.title)
        header.addStretch()
        export = QPushButton("导出")
        export.setToolTip("导出当前日期范围的网站汇总 CSV")
        export.clicked.connect(self.export)
        header.addWidget(export)
        layout.addLayout(header)
        self.summary = _label("尚无网站记录", secondary=True)
        layout.addWidget(self.summary)
        self.timeline = t._TimelineView(label_key="host", empty_text="这一天暂无网站记录")
        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:vertical{background:transparent;width:6px;}"
            f"QScrollBar::handle:vertical{{background:{t.BORDER};border-radius:3px;min-height:20px;}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.setWidget(self.timeline)
        self.timeline.setAutoFillBackground(False)
        self.scroll.setMinimumHeight(110)
        self.scroll.setMaximumHeight(270)
        layout.addWidget(self.scroll)
        self.week_chart = _WebsiteWeekChart()
        layout.addWidget(self.week_chart)
        self.category_title = _label("网站类型占比", size=11)
        self.category_title.setFont(t._font(11, True))
        layout.addWidget(self.category_title)
        self.category_chart = _WebsiteCategoryPie()
        layout.addWidget(self.category_chart)
        layout.addWidget(_label("按域名本地归类，未知网站列为“其他”；不读取页面内容。",
                                secondary=True, size=9))
        self.timeline_hint = _label("Ctrl + 滚轮缩放 · 拖动平移 · 双击还原", secondary=True, size=9)
        layout.addWidget(self.timeline_hint)
        self.week_hint = _label("悬浮色段查看日期、网站和时长；柱体其余区域可查看当日全部网站。",
                                secondary=True, size=9)
        layout.addWidget(self.week_hint)
        self.top_sites = _label("开启本地网站统计并连接扩展后，记录会显示在这里。", secondary=True, size=9)
        layout.addWidget(self.top_sites)
        self.top_sites.setToolTip("网站时长已包含在浏览器应用时长中，不额外加入屏幕总时长。")

    def set_range(self, start, end):
        start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
        self._range = (start, end)
        daily = start == end
        self.title.setText("网站使用时间线" if daily else "每日网站时长")
        self.scroll.setVisible(daily)
        self.timeline_hint.setVisible(daily)
        self.week_chart.setVisible(not daily)
        self.week_hint.setVisible(not daily)
        rows = site_storage.get_site_stats(end, start)
        self.category_chart.set_data(rows)
        total = sum(r["total_seconds"] for r in rows)
        prefix = "当天" if start == end else f"{(end-start).days + 1} 天合计"
        self.summary.setText(f"{prefix} · {len(rows)} 个网站 · {storage.format_duration_short(total)}")
        self.top_sites.setText("网站时长已包含在浏览器应用中，不重复计入总时长。" if rows else
                               "暂无记录，请在设置中开启本地网站统计并连接浏览器扩展。")
        self.summary.setToolTip("\n".join(f"{r['host']}：{storage.format_duration_short(r['total_seconds'])}"
                                          for r in rows))
        if daily:
            day = start.isoformat()
            self.timeline.set_data(site_storage.get_site_fragments_for_date(day),
                                   reset_view=day != self._timeline_day)
            self._timeline_day = day
        else:
            self.week_chart.set_data(start, end, site_storage.get_site_summary_range(start, end))

    def export(self):
        if self._range is None:
            return
        start, end = self._range
        path, _ = QFileDialog.getSaveFileName(self, "导出网站汇总", f"website_{start}_{end}.csv", "CSV (*.csv)")
        if path:
            site_storage.export_sites(start, end, path)


class BrowserSettingsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        t = _theme()
        self.setStyleSheet(_styles())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        heading = _label("网站记录", size=13)
        heading.setFont(t._font(13, True))
        layout.addWidget(heading)
        layout.addWidget(_label("在首页查看每个网站的使用时段，仅保存域名和时长。", secondary=True))
        self.status = _label("")
        self.status.setObjectName("browserConnectionStatus")
        layout.addWidget(self.status)
        self.checkboxes = {}
        for key, title, detail in (
                ("web_tracking_enabled", "启用本地网站统计", "扩展自动连接，仅统计前台、非隐身网页。"),
                ("web_export_enabled", "自动导出网站 CSV", "按日、周、月保存独立的网站汇总文件。"),
                ("web_upload_enabled", "邮件中加入网站图表", "上传域名的每日汇总、真实分时用量和起止时段，需同时开启自动上传。"),
                ("timeline_upload_enabled", "邮件中加入应用时间线", "允许上传每 15 分钟的软件使用分布，不含窗口标题。")):
            row = QVBoxLayout()
            row.setSpacing(2)
            checkbox = QCheckBox(title)
            checkbox.setFont(t._font(11))
            checkbox.setChecked(storage.get_setting(key, "0") == "1")
            checkbox.toggled.connect(lambda checked, k=key: self._set(k, checked))
            self.checkboxes[key] = checkbox
            row.addWidget(checkbox)
            hint = _label(detail, secondary=True, size=9)
            hint.setContentsMargins(28, 0, 0, 0)
            row.addWidget(hint)
            layout.addLayout(row)

        install = self._disclosure(layout, "扩展安装与连接")
        root = (Path(sys.executable).parent / "browser-extension" if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parents[1] / "browser_extension" / "chromium")
        install.addWidget(_label("在 Chrome / Edge 中加载下方文件夹，无需手动配对。", secondary=True))
        location = QLineEdit(str(root))
        location.setReadOnly(True)
        location.setAccessibleName("浏览器扩展安装目录")
        location.setCursorPosition(0)
        install.addWidget(location)
        install.addWidget(_label("自定义或旧版扩展", secondary=True))
        self.ids = QLineEdit(storage.get_setting("web_extension_ids", ""))
        self.ids.setPlaceholderText("扩展 ID，多个用英文逗号分隔")
        self.ids.setAccessibleName("自定义扩展 ID")
        install.addWidget(self.ids)
        buttons = QHBoxLayout()
        for title, callback in (("保存 ID", self.save_ids), ("复制配对码", self.copy_token), ("重置配对码", self.reset_token)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        install.addLayout(buttons)
        self.diagnostics = _label("", secondary=True, size=9)
        install.addWidget(self.diagnostics)

        manage = self._disclosure(layout, "忽略网站与清理数据")
        manage.addWidget(_label("只忽略之后的记录；输入域名，不含协议和路径。", secondary=True))
        self.ignored = QLineEdit(",".join(site_storage.get_ignored_sites()))
        self.ignored.setPlaceholderText("例如 example.com, docs.example.com")
        self.ignored.setAccessibleName("忽略的网站")
        manage.addWidget(self.ignored)
        actions = QHBoxLayout()
        save = QPushButton("保存忽略列表")
        save.clicked.connect(self.save_ignored)
        actions.addWidget(save)
        clear = QPushButton("清除本地网站记录")
        clear.setStyleSheet(f"QPushButton{{color:{t.RED};}}")
        clear.clicked.connect(self.clear)
        actions.addWidget(clear)
        manage.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def _disclosure(self, layout, title):
        button = QPushButton("+  " + title)
        button.setCheckable(True)
        button.setStyleSheet("QPushButton{text-align:left;padding:10px 12px;}")
        content = QWidget()
        content.setVisible(False)
        inner = QVBoxLayout(content)
        inner.setContentsMargins(10, 2, 10, 10)
        inner.setSpacing(10)
        def toggle(checked):
            content.setVisible(checked)
            button.setText(("-  " if checked else "+  ") + title)
        button.toggled.connect(toggle)
        layout.addWidget(button)
        layout.addWidget(content)
        return inner

    def _set(self, key, checked):
        storage.set_setting(key, "1" if checked else "0")
        self.refresh()

    def refresh(self):
        t = _theme()
        bridge = getattr(QApplication.instance(), "browser_bridge", None)
        enabled = storage.get_setting("web_tracking_enabled", "0") == "1"
        if not enabled:
            text, color = "●  本地网站统计未开启", t.GRAY
        elif bridge and "失败" in bridge.status:
            text, color = "●  " + bridge.status, t.RED
        elif bridge and getattr(bridge, "clients", None):
            count = sum(bool(p.get("profile")) for p in bridge.clients.values())
            text, color = (f"●  已连接 {count} 个浏览器", t.GREEN) if count else ("●  等待浏览器连接", t.AMBER)
        else:
            text, color = "●  等待浏览器连接", t.AMBER
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{color};background:{t.BG};border-radius:8px;padding:10px 12px;")
        lines = [bridge.status if bridge else "桥接尚未启动", f"服务端：{config.base_url()}"]
        for kind, name in (("web", "网站"), ("timeline", "时间线")):
            lines.append(f"{name}上传：{storage.get_setting(kind + '_upload_status', '尚未上传')}")
        self.diagnostics.setText("\n".join(lines))

    def save_ids(self):
        values = [v.strip() for v in self.ids.text().split(",") if v.strip()]
        if not all(re.fullmatch(r"[a-p]{32}", v) for v in values):
            QMessageBox.warning(self, "扩展 ID", "ID 必须为扩展管理页显示的 32 个 a-p 小写字母")
            return
        storage.set_setting("web_extension_ids", ",".join(values))

    def copy_token(self):
        QApplication.clipboard().setText(pairing_token())

    def reset_token(self):
        if QMessageBox.question(self, "重置配对", "自定义/旧版扩展需要重新填写配对码；随附新版会自动重连。继续？") == QMessageBox.Yes:
            pairing_token(reset=True)

    def save_ignored(self):
        from browser.sanitizer import normalize_host
        try:
            new = {normalize_host(v.strip()) for v in self.ignored.text().split(",") if v.strip()}
        except ValueError:
            QMessageBox.warning(self, "网站", "请输入有效域名，不含协议或路径")
            return
        old = set(site_storage.get_ignored_sites())
        for host in old - new:
            site_storage.set_ignored_site(host, False)
        for host in new - old:
            site_storage.set_ignored_site(host)

    def clear(self):
        if QMessageBox.question(self, "清除网站记录", "清除本机网站记录。仍开启上传时，最近日期的空快照也会清除云端对应数据；较早云端历史保留。继续？") == QMessageBox.Yes:
            site_storage.clear_site_data()
