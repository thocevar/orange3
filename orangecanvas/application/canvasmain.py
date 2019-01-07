"""
Orange Canvas Main Window

"""
import os
import sys
import logging
import operator
import io
import concurrent.futures
from functools import partial
from types import SimpleNamespace

import pkg_resources

import six

from AnyQt.QtWidgets import (
    QMainWindow, QWidget, QAction, QActionGroup, QMenu, QMenuBar, QDialog,
    QFileDialog, QMessageBox, QVBoxLayout, QSizePolicy, QToolBar, QToolButton,
    QDockWidget, QApplication, QShortcut, QPlainTextEdit,
    QPlainTextDocumentLayout, QFileIconProvider
)
from AnyQt.QtGui import (
    QColor, QIcon, QDesktopServices, QKeySequence, QTextDocument
)

from AnyQt.QtCore import (
    Qt, QObject, QEvent, QSize, QUrl, QTimer, QFile, QByteArray, QFileInfo,
    QSettings, QT_VERSION
)

try:
    from AnyQt.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineView = None
    try:
        from AnyQt.QtWebKitWidgets import QWebView
        from AnyQt.QtNetwork import QNetworkDiskCache
    except ImportError:
        QWebView = None


from AnyQt.QtCore import (
    pyqtProperty as Property, pyqtSignal as Signal, pyqtSlot as Slot
)


if QT_VERSION >= 0x50000:
    from AnyQt.QtCore import QStandardPaths
    def user_documents_path():
        """Return the users 'Documents' folder path."""
        return QStandardPaths.writableLocation(
            QStandardPaths.DocumentsLocation)
else:
    def user_documents_path():
        return QDesktopServices.storageLocation(
            QDesktopServices.DocumentsLocation)


# Compatibility with PyQt < v4.8.3
from ..utils.qtcompat import QSettings, qunwrap

from ..gui.dropshadow import DropShadowFrame
from ..gui.dock import CollapsibleDockWidget
from ..gui.quickhelp import QuickHelpTipEvent
from ..gui.utils import message_critical, message_question, \
                        message_warning, message_information

from ..help import HelpManager

from .canvastooldock import CanvasToolDock, QuickCategoryToolbar, \
                            CategoryPopupMenu, popup_position_from_source
from .aboutdialog import AboutDialog
from .schemeinfo import SchemeInfoDialog
from .outputview import OutputView, TextStream
from .settings import UserSettingsDialog, category_state
from ..document.schemeedit import SchemeEditWidget
from ..gui.itemmodels import FilterProxyModel
from ..scheme.readwrite import scheme_load

from . import welcomedialog
from . import addons

from ..preview import previewdialog, previewmodel

from .. import config

from . import tutorials

log = logging.getLogger(__name__)

# TODO: Orange Version in the base link

BASE_LINK = "http://orange.biolab.si/"

LINKS = \
    {"start-using": BASE_LINK + "start-using/",
     "tutorial": BASE_LINK + "tutorial/",
     "reference": BASE_LINK + "doc/"
     }


def canvas_icons(name):
    """Return the named canvas icon.
    """
    icon_file = QFile("canvas_icons:" + name)
    if icon_file.exists():
        return QIcon("canvas_icons:" + name)
    else:
        return QIcon(pkg_resources.resource_filename(
                      config.__name__,
                      os.path.join("icons", name))
                     )


class FakeToolBar(QToolBar):
    """A Toolbar with no contents (used to reserve top and bottom margins
    on the main window).

    """
    def __init__(self, *args, **kwargs):
        QToolBar.__init__(self, *args, **kwargs)
        self.setFloatable(False)
        self.setMovable(False)

        # Don't show the tool bar action in the main window's
        # context menu.
        self.toggleViewAction().setVisible(False)

    def paintEvent(self, event):
        # Do nothing.
        pass


def update_window_flags(widget, flags):
    currflags = widget.windowFlags()
    if int(flags) != int(currflags):
        hidden = widget.isHidden()
        widget.setWindowFlags(flags)
        # setting the flags hides the widget
        if not hidden:
            widget.show()

try:
    QKeySequence.Cancel
except AttributeError:  # < Qt 5.?
    QKeySequence.Cancel = QKeySequence(Qt.Key_Escape)


class DockWidget(QDockWidget):
    def __init__(self, *args, **kwargs):
        super(DockWidget, self).__init__(*args, **kwargs)

        for key in (QKeySequence.Close, QKeySequence.Cancel):
            QShortcut(key, self, self.close,
                      context=Qt.WidgetWithChildrenShortcut)


class CanvasMainWindow(QMainWindow):
    SETTINGS_VERSION = 2

    def __init__(self, *args):
        super().__init__(*args)

        self.__scheme_margins_enabled = True
        self.__document_title = "untitled"
        self.__first_show = True
        self.__is_transient = True
        self.__executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # PyPi search results (Future)
        self.__f_pypi_addons = None
        self.__addon_items = None
        self.widget_registry = None
        # Proxy widget registry model
        self.__proxy_model = None

        # TODO: Help view and manager to separate singleton instance.
        self.help = None
        self.help_view = None
        self.help_dock = None

        # TODO: Log view to separate singleton instance.
        self.output_dock = None
        # TODO: sync between CanvasMainWindow instances?.
        settings = QSettings()
        recent = QSettings_readArray(
            settings, "mainwindow/recent-items",
            {"title": str, "path": str}
        )
        recent = [RecentItem(**item) for item in recent]
        recent = [item for item in recent if os.path.exists(item.path)]

        self.recent_schemes = recent

        self.num_recent_schemes = 15

        self.help = HelpManager(self)

        self.setup_actions()
        self.setup_ui()
        self.setup_menu()

        self.restore()

    def setup_ui(self):
        """Setup main canvas ui
        """

        log.info("Setting up Canvas main window.")

        # Two dummy tool bars to reserve space
        self.__dummy_top_toolbar = FakeToolBar(
                            objectName="__dummy_top_toolbar")
        self.__dummy_bottom_toolbar = FakeToolBar(
                            objectName="__dummy_bottom_toolbar")

        self.__dummy_top_toolbar.setFixedHeight(20)
        self.__dummy_bottom_toolbar.setFixedHeight(20)

        self.addToolBar(Qt.TopToolBarArea, self.__dummy_top_toolbar)
        self.addToolBar(Qt.BottomToolBarArea, self.__dummy_bottom_toolbar)

        self.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)
        self.setCorner(Qt.BottomRightCorner, Qt.RightDockWidgetArea)

        self.setDockOptions(QMainWindow.AnimatedDocks)
        # Create an empty initial scheme inside a container with fixed
        # margins.
        w = QWidget()
        w.setLayout(QVBoxLayout())
        w.layout().setContentsMargins(20, 0, 10, 0)

        self.scheme_widget = SchemeEditWidget()
        self.scheme_widget.setScheme(config.workflow_constructor(parent=self))

        dropfilter = UrlDropEventFilter(self)
        dropfilter.urlDropped.connect(self.open_scheme_file)
        self.scheme_widget.setAcceptDrops(True)
        self.scheme_widget.installEventFilter(dropfilter)

        w.layout().addWidget(self.scheme_widget)

        self.setCentralWidget(w)

        # Drop shadow around the scheme document
        frame = DropShadowFrame(radius=15)
        frame.setColor(QColor(0, 0, 0, 100))
        frame.setWidget(self.scheme_widget)

        # Window 'title'
        self.setWindowFilePath(self.scheme_widget.path())
        self.scheme_widget.pathChanged.connect(self.setWindowFilePath)
        self.scheme_widget.modificationChanged.connect(self.setWindowModified)

        def touch():
            # Mark the window as non transient on any change
            self.__is_transient = False
            self.scheme_widget.modificationChanged.disconnect(touch)
        self.scheme_widget.modificationChanged.connect(touch)

        # QMainWindow's Dock widget
        self.dock_widget = CollapsibleDockWidget(objectName="main-area-dock")
        self.dock_widget.setFeatures(QDockWidget.DockWidgetMovable | \
                                     QDockWidget.DockWidgetClosable)

        self.dock_widget.setAllowedAreas(Qt.LeftDockWidgetArea | \
                                         Qt.RightDockWidgetArea)

        # Main canvas tool dock (with widget toolbox, common actions.
        # This is the widget that is shown when the dock is expanded.
        canvas_tool_dock = CanvasToolDock(objectName="canvas-tool-dock")
        canvas_tool_dock.setSizePolicy(QSizePolicy.Fixed,
                                       QSizePolicy.MinimumExpanding)

        # Bottom tool bar
        self.canvas_toolbar = canvas_tool_dock.toolbar
        self.canvas_toolbar.setIconSize(QSize(24, 24))
        self.canvas_toolbar.setMinimumHeight(28)
        self.canvas_toolbar.layout().setSpacing(1)

        # Widgets tool box
        self.widgets_tool_box = canvas_tool_dock.toolbox
        self.widgets_tool_box.setObjectName("canvas-toolbox")
        self.widgets_tool_box.setTabButtonHeight(30)
        self.widgets_tool_box.setTabIconSize(QSize(26, 26))
        self.widgets_tool_box.setButtonSize(QSize(64, 84))
        self.widgets_tool_box.setIconSize(QSize(48, 48))

        self.widgets_tool_box.triggered.connect(
            self.on_tool_box_widget_activated
        )

        self.dock_help = canvas_tool_dock.help
        self.dock_help.setMaximumHeight(150)
        self.dock_help.document().setDefaultStyleSheet("h3, a {color: orange;}")
        default_help = "Select a widget to show its description." \
                       "<br><br>" \
                       "See <a href='orange://examples'>workflow examples</a>, " \
                       "<a href='orange://tutorials'>YouTube tutorials</a>, " \
                       "or open the <a href='orange://welcome'>welcome screen</a>."
        self.dock_help.setDefaultText(default_help)

        self.dock_help_action = canvas_tool_dock.toogleQuickHelpAction()
        self.dock_help_action.setText(self.tr("Show Help"))
        self.dock_help_action.setIcon(canvas_icons("Info.svg"))

        self.canvas_tool_dock = canvas_tool_dock

        # Dock contents when collapsed (a quick category tool bar, ...)
        dock2 = QWidget(objectName="canvas-quick-dock")
        dock2.setLayout(QVBoxLayout())
        dock2.layout().setContentsMargins(0, 0, 0, 0)
        dock2.layout().setSpacing(0)
        dock2.layout().setSizeConstraint(QVBoxLayout.SetFixedSize)

        self.quick_category = QuickCategoryToolbar()
        self.quick_category.setButtonSize(QSize(38, 30))
        self.quick_category.actionTriggered.connect(
            self.on_quick_category_action
        )

        tool_actions = self.current_document().toolbarActions()

        (self.zoom_in_action, self.zoom_out_action, self.zoom_reset_action,
         self.canvas_align_to_grid_action,
         self.canvas_text_action, self.canvas_arrow_action,) = tool_actions

        self.canvas_align_to_grid_action.setIcon(canvas_icons("Grid.svg"))
        self.canvas_text_action.setIcon(canvas_icons("Text Size.svg"))
        self.canvas_arrow_action.setIcon(canvas_icons("Arrow.svg"))

        dock_actions = [
            self.show_properties_action,
            self.canvas_align_to_grid_action,
            self.canvas_text_action,
            self.canvas_arrow_action,
            self.freeze_action,
            self.dock_help_action
        ]

        # Tool bar in the collapsed dock state (has the same actions as
        # the tool bar in the CanvasToolDock
        actions_toolbar = QToolBar(orientation=Qt.Vertical)
        actions_toolbar.setFixedWidth(38)
        actions_toolbar.layout().setSpacing(0)

        actions_toolbar.setToolButtonStyle(Qt.ToolButtonIconOnly)

        for action in dock_actions:
            self.canvas_toolbar.addAction(action)
            button = self.canvas_toolbar.widgetForAction(action)
            button.setPopupMode(QToolButton.DelayedPopup)

            actions_toolbar.addAction(action)
            button = actions_toolbar.widgetForAction(action)
            button.setFixedSize(38, 30)
            button.setPopupMode(QToolButton.DelayedPopup)

        dock2.layout().addWidget(self.quick_category)
        dock2.layout().addWidget(actions_toolbar)

        self.dock_widget.setAnimationEnabled(False)
        self.dock_widget.setExpandedWidget(self.canvas_tool_dock)
        self.dock_widget.setCollapsedWidget(dock2)
        self.dock_widget.setExpanded(True)
        self.dock_widget.expandedChanged.connect(self._on_tool_dock_expanded)

        self.addDockWidget(Qt.LeftDockWidgetArea, self.dock_widget)
        self.dock_widget.dockLocationChanged.connect(
            self._on_dock_location_changed
        )

        self.output_dock = DockWidget(
            self.tr("Log"), self, objectName="output-dock",
            allowedAreas=Qt.BottomDockWidgetArea,
            visible=self.show_output_action.isChecked(),
        )
        self.output_dock.setWidget(OutputView())
        self.output_dock.visibilityChanged[bool].connect(
            self.show_output_action.setChecked
        )
        self.addDockWidget(Qt.BottomDockWidgetArea, self.output_dock)

        self.help_dock = DockWidget(
            self.tr("Help"), self, objectName="help-dock",
            allowedAreas=Qt.RightDockWidgetArea |
                         Qt.BottomDockWidgetArea,
            visible=False
        )
        if QWebEngineView is not None:
            self.help_view = QWebEngineView()
        elif QWebView is not None:
            self.help_view = QWebView()
            manager = self.help_view.page().networkAccessManager()
            cache = QNetworkDiskCache()
            cache.setCacheDirectory(
                os.path.join(config.cache_dir(), "help", "help-view-cache")
            )
            manager.setCache(cache)

        self.help_dock.setWidget(self.help_view)
        self.addDockWidget(Qt.RightDockWidgetArea, self.help_dock)

        self.setMinimumSize(600, 500)

    def setup_actions(self):
        """Initialize main window actions.
        """

        self.new_action = \
            QAction(self.tr("New"), self,
                    objectName="action-new",
                    toolTip=self.tr("Open a new workflow."),
                    triggered=self.new_workflow_window,
                    shortcut=QKeySequence.New,
                    icon=canvas_icons("New.svg")
                    )

        self.open_action = \
            QAction(self.tr("Open"), self,
                    objectName="action-open",
                    toolTip=self.tr("Open a workflow."),
                    triggered=self.open_scheme,
                    shortcut=QKeySequence.Open,
                    icon=canvas_icons("Open.svg")
                    )

        self.open_and_freeze_action = \
            QAction(self.tr("Open and Freeze"), self,
                    objectName="action-open-and-freeze",
                    toolTip=self.tr("Open a new workflow and freeze signal "
                                    "propagation."),
                    triggered=self.open_and_freeze_scheme
                    )

        self.open_and_freeze_action.setShortcut(
            QKeySequence(Qt.ControlModifier | Qt.AltModifier | Qt.Key_O)
        )

        self.close_window_action = \
            QAction(self.tr("Close Window"), self,
                    objectName="action-close-window",
                    toolTip=self.tr("Close the window"),
                    shortcut=QKeySequence.Close,
                    triggered=self.close,
                    )

        self.save_action = \
            QAction(self.tr("Save"), self,
                    objectName="action-save",
                    toolTip=self.tr("Save current workflow."),
                    triggered=self.save_scheme,
                    shortcut=QKeySequence.Save,
                    )

        self.save_as_action = \
            QAction(self.tr("Save As ..."), self,
                    objectName="action-save-as",
                    toolTip=self.tr("Save current workflow as."),
                    triggered=self.save_scheme_as,
                    shortcut=QKeySequence.SaveAs,
                    )

        self.quit_action = \
            QAction(self.tr("Quit"), self,
                    objectName="quit-action",
                    toolTip=self.tr("Quit Orange Canvas."),
                    triggered=QApplication.closeAllWindows,
                    menuRole=QAction.QuitRole,
                    shortcut=QKeySequence.Quit,
                    )

        self.welcome_action = \
            QAction(self.tr("Welcome"), self,
                    objectName="welcome-action",
                    toolTip=self.tr("Show welcome screen."),
                    triggered=self.welcome_dialog,
                    )

        self.get_started_action = \
            QAction(self.tr("Get Started"), self,
                    objectName="get-started-action",
                    toolTip=self.tr("View a 'Get Started' introduction."),
                    triggered=self.get_started,
                    icon=canvas_icons("Get Started.svg")
                    )

        self.tutorials_action = \
            QAction(self.tr("Tutorials"), self,
                    objectName="tutorial-action",
                    toolTip=self.tr("Browse tutorials."),
                    triggered=self.tutorial_scheme,
                    icon=canvas_icons("Examples.svg")
                    )

        self.documentation_action = \
            QAction(self.tr("Documentation"), self,
                    objectName="documentation-action",
                    toolTip=self.tr("View reference documentation."),
                    triggered=self.documentation,
                    icon=canvas_icons("Documentation.svg")
                    )

        self.about_action = \
            QAction(self.tr("About"), self,
                    objectName="about-action",
                    toolTip=self.tr("Show about dialog."),
                    triggered=self.open_about,
                    menuRole=QAction.AboutRole,
                    )

        # Action group for for recent scheme actions
        self.recent_scheme_action_group = \
            QActionGroup(self, exclusive=False,
                         objectName="recent-action-group",
                         triggered=self._on_recent_scheme_action)

        self.recent_action = \
            QAction(self.tr("Browse Recent"), self,
                    objectName="recent-action",
                    toolTip=self.tr("Browse and open a recent workflow."),
                    triggered=self.recent_scheme,
                    shortcut=QKeySequence(Qt.ControlModifier | \
                                          (Qt.ShiftModifier | Qt.Key_R)),
                    icon=canvas_icons("Recent.svg")
                    )

        self.reload_last_action = \
            QAction(self.tr("Reload Last Workflow"), self,
                    objectName="reload-last-action",
                    toolTip=self.tr("Reload last open workflow."),
                    triggered=self.reload_last,
                    shortcut=QKeySequence(Qt.ControlModifier | Qt.Key_R)
                    )

        self.clear_recent_action = \
            QAction(self.tr("Clear Menu"), self,
                    objectName="clear-recent-menu-action",
                    toolTip=self.tr("Clear recent menu."),
                    triggered=self.clear_recent_schemes
                    )

        self.show_properties_action = \
            QAction(self.tr("Workflow Info"), self,
                    objectName="show-properties-action",
                    toolTip=self.tr("Show workflow properties."),
                    triggered=self.show_scheme_properties,
                    shortcut=QKeySequence(Qt.ControlModifier | Qt.Key_I),
                    icon=canvas_icons("Document Info.svg")
                    )

        self.canvas_settings_action = \
            QAction(self.tr("Settings"), self,
                    objectName="canvas-settings-action",
                    toolTip=self.tr("Set application settings."),
                    triggered=self.open_canvas_settings,
                    menuRole=QAction.PreferencesRole,
                    shortcut=QKeySequence.Preferences
                    )

        self.canvas_addons_action = \
            QAction(self.tr("&Add-ons..."), self,
                    objectName="canvas-addons-action",
                    toolTip=self.tr("Manage add-ons."),
                    triggered=self.open_addons,
                    )

        self.show_output_action = \
            QAction(self.tr("&Log"), self,
                    toolTip=self.tr("Show application standard output."),
                    checkable=True,
                    triggered=lambda checked: self.output_dock.setVisible(
                        checked),
                    )

        if sys.platform == "darwin":
            # Actions for native Mac OSX look and feel.
            self.minimize_action = \
                QAction(self.tr("Minimize"), self,
                        triggered=self.showMinimized,
                        shortcut=QKeySequence(Qt.ControlModifier | Qt.Key_M)
                        )

            self.zoom_action = \
                QAction(self.tr("Zoom"), self,
                        objectName="application-zoom",
                        triggered=self.toggleMaximized,
                        )

        self.freeze_action = \
            QAction(self.tr("Freeze"), self,
                    objectName="signal-freeze-action",
                    checkable=True,
                    toolTip=self.tr("Freeze signal propagation."),
                    toggled=self.set_signal_freeze,
                    icon=canvas_icons("Pause.svg")
                    )

        self.toggle_tool_dock_expand = \
            QAction(self.tr("Expand Tool Dock"), self,
                    objectName="toggle-tool-dock-expand",
                    checkable=True,
                    shortcut=QKeySequence(Qt.ControlModifier |
                                          (Qt.ShiftModifier | Qt.Key_D)),
                    triggered=self.set_tool_dock_expanded)
        self.toggle_tool_dock_expand.setChecked(True)

        # Gets assigned in setup_ui (the action is defined in CanvasToolDock)
        # TODO: This is bad (should be moved here).
        self.dock_help_action = None

        self.toogle_margins_action = \
            QAction(self.tr("Show Workflow Margins"), self,
                    checkable=True,
                    toolTip=self.tr("Show margins around the workflow view."),
                    )
        self.toogle_margins_action.setChecked(True)
        self.toogle_margins_action.toggled.connect(
            self.set_scheme_margins_enabled)

        self.float_widgets_on_top_action = \
            QAction(self.tr("Display Widgets on Top"), self,
                    checkable=True,
                    toolTip=self.tr("Widgets are always displayed above other windows."))
        self.float_widgets_on_top_action.toggled.connect(
            self.set_float_widgets_on_top_enabled)


    def setup_menu(self):
        menu_bar = QMenuBar()
        if sys.platform == "darwin" and QT_VERSION >= 0x50000:
            self.__menu_glob = QMenuBar(None)

        menu_bar = QMenuBar(self)

        # File menu
        file_menu = QMenu(self.tr("&File"), menu_bar)
        file_menu.addAction(self.new_action)
        file_menu.addAction(self.open_action)
        file_menu.addAction(self.open_and_freeze_action)
        file_menu.addAction(self.reload_last_action)

        # File -> Open Recent submenu
        self.recent_menu = QMenu(self.tr("Open Recent"), file_menu)
        file_menu.addMenu(self.recent_menu)
        file_menu.addAction(self.close_window_action)
        file_menu.addSeparator()
        file_menu.addAction(self.save_action)
        file_menu.addAction(self.save_as_action)
        file_menu.addSeparator()
        file_menu.addAction(self.show_properties_action)
        file_menu.addAction(self.quit_action)

        self.recent_menu.addAction(self.recent_action)

        # Store the reference to separator for inserting recent
        # schemes into the menu in `add_recent_scheme`.
        self.recent_menu_begin = self.recent_menu.addSeparator()

        icons = QFileIconProvider()
        # Add recent items.
        for item in self.recent_schemes:
            text = os.path.basename(item.path)
            if item.title:
                text = "{} ('{}')".format(text, item.title)
            icon = icons.icon(QFileInfo(item.path))
            action = QAction(
                icon, text, self, toolTip=item.path, iconVisibleInMenu=True
            )
            action.setData(item.path)
            self.recent_menu.addAction(action)
            self.recent_scheme_action_group.addAction(action)

        self.recent_menu.addSeparator()
        self.recent_menu.addAction(self.clear_recent_action)
        menu_bar.addMenu(file_menu)

        editor_menus = self.scheme_widget.menuBarActions()

        # WARNING: Hard coded order, should lookup the action text
        # and determine the proper order
        self.edit_menu = editor_menus[0].menu()
        self.widget_menu = editor_menus[1].menu()

        # Edit menu
        menu_bar.addMenu(self.edit_menu)

        # View menu
        self.view_menu = QMenu(self.tr("&View"), self)
        # find and insert window group presets submenu
        window_groups = self.scheme_widget.findChild(
            QAction, "window-groups-action"
        )
        if window_groups is not None:
            self.view_menu.addAction(window_groups)

        self.view_menu.addSeparator()
        self.view_menu.addAction(self.toggle_tool_dock_expand)
        self.view_menu.addAction(self.show_output_action)

        self.view_menu.addSeparator()

        self.view_menu.addAction(self.zoom_in_action)
        self.view_menu.addAction(self.zoom_out_action)
        self.view_menu.addAction(self.zoom_reset_action)

        self.view_menu.addSeparator()

        self.view_menu.addAction(self.toogle_margins_action)
        raise_widgets_action = self.scheme_widget.findChild(
            QAction, "bring-widgets-to-front-action"
        )
        if raise_widgets_action is not None:
            self.view_menu.addAction(raise_widgets_action)

        self.view_menu.addAction(self.float_widgets_on_top_action)
        menu_bar.addMenu(self.view_menu)

        # Options menu
        self.options_menu = QMenu(self.tr("&Options"), self)
#        self.options_menu.addAction("Add-ons")
#        self.options_menu.addAction("Developers")
#        self.options_menu.addAction("Run Discovery")
#        self.options_menu.addAction("Show Canvas Log")
#        self.options_menu.addAction("Attach Python Console")
        self.options_menu.addSeparator()
        self.options_menu.addAction(self.canvas_settings_action)
        self.options_menu.addAction(self.canvas_addons_action)

        # Widget menu
        menu_bar.addMenu(self.widget_menu)

        if sys.platform == "darwin":
            # Mac OS X native look and feel.
            self.window_menu = QMenu(self.tr("Window"), self)
            self.window_menu.addAction(self.minimize_action)
            self.window_menu.addAction(self.zoom_action)
            menu_bar.addMenu(self.window_menu)

        menu_bar.addMenu(self.options_menu)

        # Help menu.
        self.help_menu = QMenu(self.tr("&Help"), self)
        self.help_menu.addAction(self.about_action)
        self.help_menu.addAction(self.welcome_action)
        self.help_menu.addAction(self.tutorials_action)
        self.help_menu.addAction(self.documentation_action)
        menu_bar.addMenu(self.help_menu)

        self.setMenuBar(menu_bar)

    def restore(self):
        """Restore the main window state from saved settings.
        """
        QSettings.setDefaultFormat(QSettings.IniFormat)
        settings = QSettings()
        settings.beginGroup("mainwindow")

        self.dock_widget.setExpanded(
            settings.value("canvasdock/expanded", True, type=bool)
        )

        floatable = settings.value("toolbox-dock-floatable", False, type=bool)
        if floatable:
            self.dock_widget.setFeatures(self.dock_widget.features() | \
                                         QDockWidget.DockWidgetFloatable)

        self.widgets_tool_box.setExclusive(
            settings.value("toolbox-dock-exclusive", True, type=bool)
        )

        self.toogle_margins_action.setChecked(
            settings.value("scheme-margins-enabled", False, type=bool)
        )
        self.show_output_action.setChecked(
            settings.value("output-dock/is-visible", False, type=bool))

        self.canvas_tool_dock.setQuickHelpVisible(
            settings.value("quick-help/visible", True, type=bool)
        )

        self.float_widgets_on_top_action.setChecked(
            settings.value("widgets-float-on-top", False, type=bool)
        )

        self.__update_from_settings()

    def set_document_title(self, title):
        """Set the document title (and the main window title). If `title`
        is an empty string a default 'untitled' placeholder will be used.

        """
        if self.__document_title != title:
            self.__document_title = title

            if not title:
                # TODO: should the default name be platform specific
                title = self.tr("untitled")

            self.setWindowTitle(title + "[*]")

    def document_title(self):
        """Return the document title.
        """
        return self.__document_title

    def set_widget_registry(self, widget_registry):
        """Set widget registry.
        """
        if self.widget_registry is not None:
            # Clear the dock widget and popup.
            self.widgets_tool_box.setModel(None)
            self.quick_category.setModel(None)
            self.scheme_widget.setRegistry(None)
            self.help.set_registry(None)
            self.__proxy_model.deleteLater()
            self.__proxy_model = None

        self.widget_registry = widget_registry

        # Restore category hidden/sort order state
        proxy = FilterProxyModel(self)
        proxy.setSourceModel(widget_registry.model())
        self.__proxy_model = proxy
        self.__update_registry_filters()

        self.widgets_tool_box.setModel(proxy)
        self.quick_category.setModel(proxy)

        self.scheme_widget.setRegistry(widget_registry)
        self.scheme_widget.quickMenu().setModel(proxy)

        self.help.set_registry(widget_registry)

        # Restore possibly saved widget toolbox tab states
        settings = QSettings()
        state = settings.value("mainwindow/widgettoolbox/state",
                                defaultValue=QByteArray(),
                                type=QByteArray)
        if state:
            self.widgets_tool_box.restoreState(state)

    def set_quick_help_text(self, text):
        self.canvas_tool_dock.help.setText(text)

    def current_document(self):
        return self.scheme_widget

    def on_tool_box_widget_activated(self, action):
        """A widget action in the widget toolbox has been activated.
        """
        widget_desc = qunwrap(action.data())
        if widget_desc:
            scheme_widget = self.current_document()
            if scheme_widget:
                scheme_widget.createNewNode(widget_desc)

    def on_quick_category_action(self, action):
        """The quick category menu action triggered.
        """
        category = action.text()
        settings = QSettings()
        use_popover = settings.value(
            "mainwindow/toolbox-dock-use-popover-menu",
            defaultValue=True, type=bool)

        if use_popover:
            # Show a popup menu with the widgets in the category
            popup = CategoryPopupMenu(self.quick_category)
            reg = self.widget_registry.model()
            i = index(self.widget_registry.categories(), category,
                      predicate=lambda name, cat: cat.name == name)
            if i != -1:
                popup.setCategoryItem(reg.item(i))
                button = self.quick_category.buttonForAction(action)
                pos = popup_position_from_source(popup, button)
                action = popup.exec_(pos)
                if action is not None:
                    self.on_tool_box_widget_activated(action)

        else:
            # Expand the dock and open the category under the triggered button
            for i in range(self.widgets_tool_box.count()):
                cat_act = self.widgets_tool_box.tabAction(i)
                cat_act.setChecked(cat_act.text() == category)

            self.dock_widget.expand()

    def set_scheme_margins_enabled(self, enabled):
        """Enable/disable the margins around the scheme document.
        """
        if self.__scheme_margins_enabled != enabled:
            self.__scheme_margins_enabled = enabled
            self.__update_scheme_margins()

    def scheme_margins_enabled(self):
        return self.__scheme_margins_enabled

    scheme_margins_enabled = Property(bool,
                                      fget=scheme_margins_enabled,
                                      fset=set_scheme_margins_enabled)

    def __update_scheme_margins(self):
        """Update the margins around the scheme document.
        """
        enabled = self.__scheme_margins_enabled
        self.__dummy_top_toolbar.setVisible(enabled)
        self.__dummy_bottom_toolbar.setVisible(enabled)
        central = self.centralWidget()

        margin = 20 if enabled else 0

        if self.dockWidgetArea(self.dock_widget) == Qt.LeftDockWidgetArea:
            margins = (margin / 2, 0, margin, 0)
        else:
            margins = (margin, 0, margin / 2, 0)

        central.layout().setContentsMargins(*margins)

    #################
    # Action handlers
    #################
    def is_transient(self):
        """
        Is this window a transient window.

        I.e. a window that was created empty and does not contain any modified
        contents. In particular it can be reused to load a workflow model
        without any detrimental effects (like lost information).
        """
        return self.__is_transient

    # All instances created through the create_new_window below.
    # They are removed on `destroyed`
    _instances = []  # type: List[CanvasMainWindow]

    def create_new_window(self):
        # type: () -> CanvasMainWindow
        """
        Create a new top level CanvasMainWindow instance.

        The window is positioned slightly offset to the originating window
        (`self`).

        Note
        ----
        The window has `Qt.WA_DeleteOnClose` flag set. If this flag is unset
        it is the callers responsibility to explicitly delete the widget (via
        `deleteLater` or `sip.delete`).

        Returns
        -------
        window: CanvasMainWindow
        """
        window = CanvasMainWindow()
        window.setAttribute(Qt.WA_DeleteOnClose)
        window.setGeometry(self.geometry().translated(20, 20))
        window.setStyleSheet(self.styleSheet())
        window.set_widget_registry(self.widget_registry)
        window.restoreState(self.saveState(self.SETTINGS_VERSION),
                            self.SETTINGS_VERSION)
        window.set_tool_dock_expanded(self.dock_widget.expanded())
        window.set_float_widgets_on_top_enabled(self.float_widgets_on_top_action.isChecked())
        logview = window.output_view()  # type: OutputView

        te = logview.findChild(QPlainTextEdit)

        doc = self.output_view().findChild(QPlainTextEdit).document()
        # first clone the existing document and set it on the new instance
        doc = doc.clone(parent=te)  # type: QTextDocument
        doc.setDocumentLayout(QPlainTextDocumentLayout(doc))
        te.setDocument(doc)

        # route the stdout/err if possible
        stdout, stderr = sys.stdout, sys.stderr
        if isinstance(stdout, TextStream):
            stdout.stream.connect(logview.write)

        if isinstance(stderr, TextStream):
            err_formater = logview.formated(color=Qt.red)
            stderr.stream.connect(err_formater.write)

        CanvasMainWindow._instances.append(window)
        window.destroyed.connect(
            lambda: CanvasMainWindow._instances.remove(window))
        return window

    def new_workflow_window(self):
        # type: () -> None
        """
        Create and show a new CanvasMainWindow instance.
        """
        newwindow = self.create_new_window()
        newwindow.raise_()
        newwindow.show()
        newwindow.activateWindow()

        settings = QSettings()
        show = settings.value("schemeinfo/show-at-new-scheme", True,
                              type=bool)
        if show:
            newwindow.show_scheme_properties()

    def open_scheme_file(self, filename, **kwargs):
        """
        Open and load a scheme file.
        """
        if isinstance(filename, QUrl):
            filename = filename.toLocalFile()

        if self.is_transient():
            window = self
        else:
            window = self.create_new_window()
            window.show()
            window.raise_()
            window.activateWindow()

        if kwargs.get("freeze", False):
            window.freeze_action.setChecked(True)
        window.load_scheme(filename)

    def _open_workflow_dialog(self):
        # type: () -> QFileDialog
        """
        Create and return an initialized QFileDialog for opening a workflow
        file.

        The dialog is a child of this window and has the `Qt.WA_DeleteOnClose`
        flag set.
        """
        settings = QSettings()
        settings.beginGroup("mainwindow")
        start_dir = settings.value("last-scheme-dir", "", type=str)
        if not os.path.isdir(start_dir):
            start_dir = user_documents_path()

        dlg = QFileDialog(
            self, windowTitle=self.tr("Open Orange Workflow File"),
            acceptMode=QFileDialog.AcceptOpen,
            fileMode=QFileDialog.ExistingFile,
        )
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.setDirectory(start_dir)
        dlg.setNameFilters(["Orange Workflow (*.ows)"])

        def record_last_dir():
            path = dlg.directory().canonicalPath()
            settings.setValue("last-scheme-dir", path)

        dlg.accepted.connect(record_last_dir)
        return dlg

    def open_scheme(self):
        """
        Open a user selected workflow in a new window.
        """
        dlg = self._open_workflow_dialog()
        dlg.fileSelected.connect(self.open_scheme_file)
        dlg.exec()

    def open_and_freeze_scheme(self):
        """
        Open a user selected workflow file in a new window and freeze
        signal propagation.
        """
        dlg = self._open_workflow_dialog()
        dlg.fileSelected.connect(partial(self.open_scheme_file, freeze=True))
        dlg.exec()

    def load_scheme(self, filename):
        """
        Load a scheme from a file (`filename`) into the current
        document, updates the recent scheme list and the loaded scheme path
        property.
        """
        new_scheme = self.new_scheme_from(filename)
        if new_scheme is not None:
            self.set_new_scheme(new_scheme)

            scheme_doc_widget = self.current_document()
            scheme_doc_widget.setPath(filename)

            self.add_recent_scheme(new_scheme.title, filename)
            if not self.freeze_action.isChecked():
                # activate the default window group.
                scheme_doc_widget.activateDefaultWindowGroup()
            return QDialog.Accepted
        else:
            return QDialog.Rejected

    def new_scheme_from(self, filename):
        """Create and return a new :class:`scheme.Scheme`
        from a saved `filename`. Return `None` if an error occurs.

        """
        new_scheme = config.workflow_constructor(parent=self)
        new_scheme.set_runtime_env("basedir", os.path.dirname(filename))
        errors = []
        try:
            with open(filename, "rb") as f:
                scheme_load(new_scheme, f, error_handler=errors.append)

        except Exception:
            message_critical(
                 self.tr("Could not load an Orange Workflow file"),
                 title=self.tr("Error"),
                 informative_text=self.tr("An unexpected error occurred "
                                          "while loading '%s'.") % filename,
                 exc_info=True,
                 parent=self)
            return None
        if errors:
            message_warning(
                self.tr("Errors occurred while loading the workflow."),
                title=self.tr("Problem"),
                informative_text=self.tr(
                     "There were problems loading some "
                     "of the widgets/links in the "
                     "workflow."
                ),
                details="\n".join(map(repr, errors))
            )
        return new_scheme

    def reload_last(self):
        """
        Reload last opened scheme. Return QDialog.Rejected if the
        user canceled the operation and QDialog.Accepted otherwise.
        """
        # TODO: Search for a temp backup scheme with per process
        # locking.
        settings = QSettings()
        recent = QSettings_readArray(
            settings, "mainwindow/recent-items", {"path": str}
        )
        if recent:
            recent = recent[0]["path"]
            self.open_scheme_file(recent)

    def set_new_scheme(self, new_scheme):
        """
        Set new_scheme as the current shown scheme.

        The old scheme will be deleted.
        """
        self.__is_transient = False
        scheme_doc = self.current_document()
        old_scheme = scheme_doc.scheme()
        manager = getattr(new_scheme, "signal_manager", None)
        if self.freeze_action.isChecked() and manager is not None:
            manager.pause()
        wm = getattr(new_scheme, "widget_manager", None)
        if wm is not None:
            wm.set_float_widgets_on_top(
                self.float_widgets_on_top_action.isChecked()
            )
        scheme_doc.setScheme(new_scheme)

        # Send a close event to the Scheme, it is responsible for
        # closing/clearing all resources (widgets).
        QApplication.sendEvent(old_scheme, QEvent(QEvent.Close))

        old_scheme.deleteLater()

    def ask_save_changes(self):
        """Ask the user to save the changes to the current scheme.
        Return QDialog.Accepted if the scheme was successfully saved
        or the user selected to discard the changes. Otherwise return
        QDialog.Rejected.

        """
        document = self.current_document()
        title = document.scheme().title or "untitled"
        selected = message_question(
            self.tr('Do you want to save the changes you made to workflow "%s"?')
                    % title,
            self.tr("Save Changes?"),
            self.tr("Your changes will be lost if you do not save them."),
            buttons=QMessageBox.Save | QMessageBox.Cancel | \
                    QMessageBox.Discard,
            default_button=QMessageBox.Save,
            parent=self)

        if selected == QMessageBox.Save:
            return self.save_scheme()
        elif selected == QMessageBox.Discard:
            return QDialog.Accepted
        elif selected == QMessageBox.Cancel:
            return QDialog.Rejected

    def save_scheme(self):
        """Save the current scheme. If the scheme does not have an associated
        path then prompt the user to select a scheme file. Return
        QDialog.Accepted if the scheme was successfully saved and
        QDialog.Rejected if the user canceled the file selection.

        """
        document = self.current_document()
        curr_scheme = document.scheme()
        path = document.path()

        if path:
            if self.save_scheme_to(curr_scheme, path):
                document.setModified(False)
                self.add_recent_scheme(curr_scheme.title, document.path())
                return QDialog.Accepted
            else:
                return QDialog.Rejected
        else:
            return self.save_scheme_as()

    def save_scheme_as(self):
        """
        Save the current scheme by asking the user for a filename. Return
        `QFileDialog.Accepted` if the scheme was saved successfully and
        `QFileDialog.Rejected` if not.

        """
        document = self.current_document()
        curr_scheme = document.scheme()
        title = curr_scheme.title or "untitled"

        settings = QSettings()
        settings.beginGroup("mainwindow")

        if document.path():
            start_dir = document.path()
        else:
            start_dir = settings.value("last-scheme-dir", "", type=str)
            if not os.path.isdir(start_dir):
                start_dir = user_documents_path()

            start_dir = os.path.join(start_dir, title + ".ows")

        filename, _ = QFileDialog.getSaveFileName(
            self, self.tr("Save Orange Workflow File"),
            start_dir, self.tr("Orange Workflow (*.ows)")
        )

        if filename:
            settings.setValue("last-scheme-dir", os.path.dirname(filename))
            if self.save_scheme_to(curr_scheme, filename):
                document.setPath(filename)
                document.setModified(False)
                self.add_recent_scheme(curr_scheme.title, document.path())

                return QFileDialog.Accepted

        return QFileDialog.Rejected

    def save_scheme_to(self, scheme, filename):
        """
        Save a Scheme instance `scheme` to `filename`. On success return
        `True`, else show a message to the user explaining the error and
        return `False`.

        """
        dirname, basename = os.path.split(filename)
        title = scheme.title or "untitled"

        # First write the scheme to a buffer so we don't truncate an
        # existing scheme file if `scheme.save_to` raises an error.
        buffer = io.BytesIO()
        try:
            scheme.set_runtime_env("basedir", dirname)
            scheme.save_to(buffer, pretty=True, pickle_fallback=True)
        except Exception:
            log.error("Error saving %r to %r", scheme, filename, exc_info=True)
            message_critical(
                self.tr('An error occurred while trying to save workflow '
                        '"%s" to "%s"') % (title, basename),
                title=self.tr("Error saving %s") % basename,
                exc_info=True,
                parent=self
            )
            return False

        try:
            with open(filename, "wb") as f:
                f.write(buffer.getvalue())
            return True
        except (IOError, OSError) as ex:
            log.error("%s saving '%s'", type(ex).__name__, filename,
                      exc_info=True)
            if ex.errno == 2:
                # user might enter a string containing a path separator
                message_warning(
                    self.tr('Workflow "%s" could not be saved. The path does '
                            'not exist') % title,
                    title="",
                    informative_text=self.tr("Choose another location."),
                    parent=self
                )
            elif ex.errno == 13:
                message_warning(
                    self.tr('Workflow "%s" could not be saved. You do not '
                            'have write permissions.') % title,
                    title="",
                    informative_text=self.tr(
                        "Change the file system permissions or choose "
                        "another location."),
                    parent=self
                )
            else:
                message_warning(
                    self.tr('Workflow "%s" could not be saved.') % title,
                    title="",
                    informative_text=ex.strerror,
                    exc_info=True,
                    parent=self
                )
            return False

        except Exception:
            log.error("Error saving %r to %r", scheme, filename, exc_info=True)
            message_critical(
                self.tr('An error occurred while trying to save workflow '
                        '"%s" to "%s"') % (title, basename),
                title=self.tr("Error saving %s") % basename,
                exc_info=True,
                parent=self
            )
            return False

    def get_started(self, *args):
        """Show getting started video
        """
        url = QUrl(LINKS["start-using"])
        QDesktopServices.openUrl(url)

    def tutorial(self, *args):
        """Show tutorial.
        """
        url = QUrl(LINKS["tutorial"])
        QDesktopServices.openUrl(url)

    def documentation(self, *args):
        """Show reference documentation.
        """
        url = QUrl(LINKS["start-using"])
        QDesktopServices.openUrl(url)

    def recent_scheme(self):
        """
        Browse recent schemes.

        Return QDialog.Rejected if the user canceled the operation and
        QDialog.Accepted otherwise.
        """
        settings = QSettings()
        recent = QSettings_readArray(
            settings, "mainwindow/recent-items", {"title": str, "path": str}
        )
        recent = [RecentItem(**item) for item in recent]
        recent = [item for item in recent if os.path.exists(item.path)]
        items = [previewmodel.PreviewItem(name=item.title, path=item.path)
                 for item in recent]
        model = previewmodel.PreviewModel(items=items)

        dialog = previewdialog.PreviewDialog(self)
        title = self.tr("Recent Workflows")
        dialog.setWindowTitle(title)
        template = ('<h3 style="font-size: 26px">\n'
                    #'<img height="26" src="canvas_icons:Recent.svg">\n'
                    '{0}\n'
                    '</h3>')
        dialog.setHeading(template.format(title))
        dialog.setModel(model)

        model.delayedScanUpdate()

        status = dialog.exec_()

        index = dialog.currentIndex()

        dialog.deleteLater()
        model.deleteLater()

        if status == QDialog.Accepted:
            selected = model.item(index)
            self.open_scheme_file(selected.path())
        return status

    def tutorial_scheme(self):
        """
        Browse a collection of tutorial/example schemes.

        Returns QDialog.Rejected if the user canceled the dialog else loads
        the selected scheme into the canvas and returns QDialog.Accepted.
        """
        tutors = tutorials.tutorials()
        items = [previewmodel.PreviewItem(path=t.abspath()) for t in tutors]
        model = previewmodel.PreviewModel(items=items)
        dialog = previewdialog.PreviewDialog(self)
        title = self.tr("Tutorials")
        dialog.setWindowTitle(title)
        template = ('<h3 style="font-size: 26px">\n'
                    #'<img height="26" src="canvas_icons:Tutorials.svg">\n'
                    '{0}\n'
                    '</h3>')

        dialog.setHeading(template.format(title))
        dialog.setModel(model)

        model.delayedScanUpdate()
        status = dialog.exec_()
        index = dialog.currentIndex()

        dialog.deleteLater()

        if status == QDialog.Accepted:
            selected = model.item(index)
            self.open_scheme_file(selected.path())
        return status

    def welcome_dialog(self):
        """Show a modal welcome dialog for Orange Canvas.
        """

        dialog = welcomedialog.WelcomeDialog(self)
        dialog.setWindowTitle(self.tr("Welcome to Orange Data Mining"))

        def new_scheme():
            if not self.is_transient():
                self.new_workflow_window()
            dialog.accept()

        def open_scheme():
            dlg = self._open_workflow_dialog()
            dlg.setParent(dialog, Qt.Dialog)
            dlg.fileSelected.connect(self.open_scheme_file)
            dlg.accepted.connect(dialog.accept)
            dlg.exec()

        def open_recent():
            if self.recent_scheme() == QDialog.Accepted:
                dialog.accept()

        def tutorial():
            if self.tutorial_scheme() == QDialog.Accepted:
                dialog.accept()

        new_action = \
            QAction(self.tr("New"), dialog,
                    toolTip=self.tr("Open a new workflow."),
                    triggered=new_scheme,
                    shortcut=QKeySequence.New,
                    icon=canvas_icons("New.svg")
                    )

        open_action = \
            QAction(self.tr("Open"), dialog,
                    objectName="welcome-action-open",
                    toolTip=self.tr("Open a workflow."),
                    triggered=open_scheme,
                    shortcut=QKeySequence.Open,
                    icon=canvas_icons("Open.svg")
                    )

        recent_action = \
            QAction(self.tr("Recent"), dialog,
                    objectName="welcome-recent-action",
                    toolTip=self.tr("Browse and open a recent workflow."),
                    triggered=open_recent,
                    shortcut=QKeySequence(Qt.ControlModifier | \
                                          (Qt.ShiftModifier | Qt.Key_R)),
                    icon=canvas_icons("Recent.svg")
                    )

        tutorials_action = \
            QAction(self.tr("Tutorial"), dialog,
                    objectName="welcome-tutorial-action",
                    toolTip=self.tr("Browse tutorial workflows."),
                    triggered=tutorial,
                    icon=canvas_icons("Tutorials.svg")
                    )

        bottom_row = [self.get_started_action, tutorials_action,
                      self.documentation_action]

        self.new_action.triggered.connect(dialog.accept)
        top_row = [new_action, open_action, recent_action]

        dialog.addRow(top_row, background="light-grass")
        dialog.addRow(bottom_row, background="light-orange")

        settings = QSettings()

        dialog.setShowAtStartup(
            settings.value("startup/show-welcome-screen", True, type=bool)
        )

        status = dialog.exec_()

        settings.setValue("startup/show-welcome-screen",
                          dialog.showAtStartup())

        dialog.deleteLater()

        return status

    def scheme_properties_dialog(self):
        """Return an empty `SchemeInfo` dialog instance.
        """
        settings = QSettings()
        value_key = "schemeinfo/show-at-new-scheme"
        dialog = SchemeInfoDialog(
            self, windowTitle=self.tr("Workflow Info"),
        )
        dialog.setFixedSize(725, 450)
        dialog.setShowAtNewScheme(settings.value(value_key, True, type=bool))

        def onfinished():
            settings.setValue(value_key, dialog.showAtNewScheme())
        dialog.finished.connect(onfinished)
        return dialog

    def show_scheme_properties(self):
        """
        Show current scheme properties.
        """
        current_doc = self.current_document()
        scheme = current_doc.scheme()
        dlg = self.scheme_properties_dialog()
        dlg.setAutoCommit(False)
        dlg.setScheme(scheme)
        status = dlg.exec_()

        if status == QDialog.Accepted:
            editor = dlg.editor
            stack = current_doc.undoStack()
            stack.beginMacro(self.tr("Change Info"))
            current_doc.setTitle(editor.title())
            current_doc.setDescription(editor.description())
            stack.endMacro()
        return status

    def set_signal_freeze(self, freeze):
        scheme = self.current_document().scheme()
        manager = getattr(scheme, "signal_manager", None)
        if manager is not None:
            if freeze:
                manager.pause()
            else:
                manager.resume()

    def remove_selected(self):
        """Remove current scheme selection.
        """
        self.current_document().removeSelected()

    def select_all(self):
        self.current_document().selectAll()

    def open_widget(self):
        """Open/raise selected widget's GUI.
        """
        self.current_document().openSelected()

    def rename_widget(self):
        """Rename the current focused widget.
        """
        doc = self.current_document()
        nodes = doc.selectedNodes()
        if len(nodes) == 1:
            doc.editNodeTitle(nodes[0])

    def open_canvas_settings(self):
        """Open canvas settings/preferences dialog
        """
        dlg = UserSettingsDialog(self)
        dlg.setWindowTitle(self.tr("Preferences"))
        dlg.show()
        status = dlg.exec_()
        if status == 0:
            # TODO: Notify all instances
            self.__update_from_settings()

    def open_addons(self):
        """Open the add-on manager dialog.
        """
        if self.__f_pypi_addons is None:
            self.__f_pypi_addons = self.__executor.submit(
                addons.pypi_search,
                config.default.addon_pypi_search_spec(),
                timeout=20,
            )

        dlg = addons.AddonManagerDialog(
            self, windowTitle=self.tr("Add-ons"), modal=True)
        dlg.setAttribute(Qt.WA_DeleteOnClose)

        if self.__addon_items is not None:
            pypi_distributions = self.__f_pypi_addons.result()
            installed = [ep.dist for ep in config.default.addon_entry_points()]
            items = addons.installable_items(pypi_distributions, installed)
            self.__addon_items = items
            dlg.setItems(items)
        else:
            # Use the dialog's own progress dialog
            progress = dlg.progressDialog()
            dlg.show()
            progress.show()
            progress.setLabelText(
                self.tr("Retrieving package list")
            )
            self.__f_pypi_addons.add_done_callback(
                addons.method_queued(self.__on_pypi_search_done, (object,))
            )
            close_dialog = addons.method_queued(dlg.close, ())

            self.__f_pypi_addons.add_done_callback(
                lambda f:
                    close_dialog() if f.exception() else None)

            self.__p_addon_items_available.connect(progress.hide)
            self.__p_addon_items_available.connect(dlg.setItems)

        return dlg.exec_()

    def set_float_widgets_on_top_enabled(self, enabled):
        if self.float_widgets_on_top_action.isChecked() != enabled:
            self.float_widgets_on_top_action.setChecked(enabled)

        wm = getattr(self.current_document().scheme(), "widget_manager", None)
        if wm is not None:
            wm.set_float_widgets_on_top(enabled)

    __p_addon_items_available = Signal(object)

    @Slot(object)
    def __on_pypi_search_done(self, f):
        if f.exception():
            exc = f.exception()
            log.error("Error querying PyPi", exc_info=(type(exc), exc, None))

            message_warning(
                "Could not retrieve package list",
                title="Error",
                informative_text=str(exc),
                parent=self
            )

            self.__f_pypi_addons = None
            self.__addon_items = None
            return

        pypi_distributions = f.result()
        installed = [ep.dist for ep in config.default.addon_entry_points()]
        items = addons.installable_items(pypi_distributions, installed)

        self.__addon_items = items
        self.__p_addon_items_available.emit(items)

    def output_view(self):
        """Return the output text widget.
        """
        return self.output_dock.widget()

    def open_about(self):
        """Open the about dialog.
        """
        dlg = AboutDialog(self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.exec_()

    def add_recent_scheme(self, title, path):
        """Add an entry (`title`, `path`) to the list of recent schemes.
        """
        if not path:
            # No associated persistent path so we can't do anything.
            return

        text = os.path.basename(path)
        if title:
            text = "{} ('{}')".format(text, title)

        settings = QSettings()
        settings.beginGroup("mainwindow")
        recent = QSettings_readArray(
            settings, "recent-items", {"title": str, "path": str}
        )
        recent = [RecentItem(**d) for d in recent]
        filename = os.path.abspath(os.path.realpath(path))
        filename = os.path.normpath(filename)

        actions_by_filename = {}
        for action in self.recent_scheme_action_group.actions():
            path = six.text_type(qunwrap(action.data()))
            actions_by_filename[path] = action

        if filename in actions_by_filename:
            # reuse/update the existing action
            action = actions_by_filename[filename]
            self.recent_menu.removeAction(action)
            self.recent_scheme_action_group.removeAction(action)
            action.setText(text)
        else:
            icons = QFileIconProvider()
            icon = icons.icon(QFileInfo(filename))
            action = QAction(
                icon, text, self, toolTip=filename, iconVisibleInMenu=True
            )
            action.setData(filename)

        # Find the separator action in the menu (after 'Browse Recent')
        recent_actions = self.recent_menu.actions()
        begin_index = index(recent_actions, self.recent_menu_begin)
        action_before = recent_actions[begin_index + 1]

        self.recent_menu.insertAction(action_before, action)
        self.recent_scheme_action_group.addAction(action)

        recent.insert(0, RecentItem(title=title, path=filename))

        for i in reversed(range(1, len(recent))):
            try:
                same = os.path.samefile(recent[i].path, filename)
            except OSError:
                same = False
            if same:
                del recent[i]

        recent = recent[:self.num_recent_schemes]

        QSettings_writeArray(
            settings, "recent-items",
            [{"title": item.title, "path": item.path} for item in recent]
        )

    def clear_recent_schemes(self):
        """Clear list of recent schemes
        """
        actions = self.recent_scheme_action_group.actions()
        for action in actions:
            self.recent_menu.removeAction(action)
            self.recent_scheme_action_group.removeAction(action)

        settings = QSettings()
        QSettings_writeArray(settings, "mainwindow/recent-items", [])

    def _on_recent_scheme_action(self, action):
        """
        A recent scheme action was triggered by the user
        """
        filename = str(action.data())
        self.open_scheme_file(filename)

    def _on_dock_location_changed(self, location):
        """Location of the dock_widget has changed, fix the margins
        if necessary.

        """
        self.__update_scheme_margins()

    def set_tool_dock_expanded(self, expanded):
        """
        Set the dock widget expanded state.
        """
        self.dock_widget.setExpanded(expanded)

    def _on_tool_dock_expanded(self, expanded):
        """
        'dock_widget' widget was expanded/collapsed.
        """
        if expanded != self.toggle_tool_dock_expand.isChecked():
            self.toggle_tool_dock_expand.setChecked(expanded)

    def createPopupMenu(self):
        # Override the default context menu popup (we don't want the user to
        # be able to hide the tool dock widget).
        return None

    def closeEvent(self, event):
        """
        Close the main window.
        """
        document = self.current_document()
        if document.isModifiedStrict():
            if self.ask_save_changes() == QDialog.Rejected:
                # Reject the event
                event.ignore()
                return

        old_scheme = document.scheme()

        # Set an empty scheme to clear the document
        document.setScheme(config.workflow_constructor(parent=self))
        QApplication.sendEvent(old_scheme, QEvent(QEvent.Close))

        old_scheme.deleteLater()

        config.save_config()

        geometry = self.saveGeometry()
        state = self.saveState(version=self.SETTINGS_VERSION)
        settings = QSettings()
        settings.beginGroup("mainwindow")
        settings.setValue("geometry", geometry)
        settings.setValue("state", state)
        settings.setValue("canvasdock/expanded",
                          self.dock_widget.expanded())
        settings.setValue("scheme-margins-enabled",
                          self.scheme_margins_enabled)

        settings.setValue("widgettoolbox/state",
                          self.widgets_tool_box.saveState())

        settings.setValue("quick-help/visible",
                          self.canvas_tool_dock.quickHelpVisible())
        settings.setValue("widgets-float-on-top",
                          self.float_widgets_on_top_action.isChecked())

        settings.endGroup()
        self.help_dock.close()
        self.output_dock.close()
        self.__executor.shutdown(wait=False)
        super().closeEvent(event)

    __did_restore = False

    def restoreState(self, state, version=0):
        # type: (Union[QByteArray, bytes, bytearray], int) -> bool
        restored = super().restoreState(state, version)
        self.__did_restore = self.__did_restore or restored
        return restored

    def showEvent(self, event):
        if self.__first_show:
            settings = QSettings()
            settings.beginGroup("mainwindow")

            # Restore geometry if not already positioned
            if not (self.testAttribute(Qt.WA_Moved) or
                    self.testAttribute(Qt.WA_Resized)):
                geom_data = settings.value("geometry", QByteArray(),
                                           type=QByteArray)
                if geom_data:
                    self.restoreGeometry(geom_data)

            state = settings.value("state", QByteArray(), type=QByteArray)
            # Restore dock/toolbar state is not already done so
            if state and not self.__did_restore:
                self.restoreState(state, version=self.SETTINGS_VERSION)

            self.__first_show = False

        return super().showEvent(event)

    def event(self, event):
        if event.type() == QEvent.StatusTip and \
                isinstance(event, QuickHelpTipEvent):
            # Using singleShot to update the text browser.
            # If updating directly the application experiences strange random
            # segfaults (in ~StatusTipEvent in QTextLayout or event just normal
            # event loop), but only when the contents are larger then the
            # QTextBrowser's viewport.
            if event.priority() == QuickHelpTipEvent.Normal:
                QTimer.singleShot(0, partial(self.dock_help.showHelp,
                                             event.html()))
            elif event.priority() == QuickHelpTipEvent.Temporary:
                QTimer.singleShot(0, partial(self.dock_help.showHelp,
                                             event.html(), event.timeout()))
            elif event.priority() == QuickHelpTipEvent.Permanent:
                QTimer.singleShot(0, partial(self.dock_help.showPermanentHelp,
                                             event.html()))

            return True

        elif event.type() == QEvent.WhatsThisClicked:
            ref = event.href()
            url = QUrl(ref)

            if url.scheme() == "help" and url.authority() == "search":
                try:
                    url = self.help.search(url)
                    self.show_help(url)
                except KeyError:
                    log.info("No help topic found for %r", url)
                    message_information(
                        self.tr("There is no documentation for this widget yet."),
                        parent=self)
            elif url.scheme() == "orange":
                target = url.host()
                if target == "examples":
                    self.tutorial_scheme()
                elif target == "tutorials":
                    self.tutorial()
                elif target == "welcome":
                    self.welcome_dialog()
                else:
                    log.error("No target found for %r", url)

            return True

        return QMainWindow.event(self, event)

    def show_help(self, url):
        """
        Show `url` in a help window.
        """
        log.info("Setting help to url: %r", url)
        settings = QSettings()
        use_external = settings.value(
            "help/open-in-external-browser", defaultValue=False, type=bool)
        if use_external or self.help_view is None:
            url = QUrl(url)
            if not QDesktopServices.openUrl(url):
                # Try fixing some common problems.
                url = QUrl.fromUserInput(url.toString())
                # 'fromUserInput' includes possible fragment into the path
                # (which prevents it to open local files) so we reparse it
                # again.
                url = QUrl(url.toString())
                QDesktopServices.openUrl(url)
        else:
            self.help_view.load(QUrl(url))
            self.help_dock.show()
            self.help_dock.raise_()

    # Mac OS X
    if sys.platform == "darwin":
        def toggleMaximized(self):
            """Toggle normal/maximized window state.
            """
            if self.isMinimized():
                # Do nothing if window is minimized
                return

            if self.isMaximized():
                self.showNormal()
            else:
                self.showMaximized()

    def sizeHint(self):
        """
        Reimplemented from QMainWindow.sizeHint
        """
        hint = QMainWindow.sizeHint(self)
        return hint.expandedTo(QSize(1024, 720))

    def __update_from_settings(self):
        settings = QSettings()
        settings.beginGroup("mainwindow")
        toolbox_floatable = settings.value("toolbox-dock-floatable",
                                           defaultValue=False,
                                           type=bool)

        features = self.dock_widget.features()
        features = updated_flags(features, QDockWidget.DockWidgetFloatable,
                                 toolbox_floatable)
        self.dock_widget.setFeatures(features)

        toolbox_exclusive = settings.value("toolbox-dock-exclusive",
                                           defaultValue=True,
                                           type=bool)
        self.widgets_tool_box.setExclusive(toolbox_exclusive)

        self.num_recent_schemes = settings.value("num-recent-schemes",
                                                 defaultValue=15,
                                                 type=int)

        float_widgets_on_top = settings.value("widgets-float-on-top",
                                              defaultValue=False,
                                              type=bool)
        self.set_float_widgets_on_top_enabled(float_widgets_on_top)

        settings.endGroup()
        settings.beginGroup("quickmenu")

        triggers = 0
        dbl_click = settings.value("trigger-on-double-click",
                                   defaultValue=True,
                                   type=bool)
        if dbl_click:
            triggers |= SchemeEditWidget.DoubleClicked

        right_click = settings.value("trigger-on-right-click",
                                    defaultValue=True,
                                    type=bool)
        if right_click:
            triggers |= SchemeEditWidget.RightClicked

        space_press = settings.value("trigger-on-space-key",
                                     defaultValue=True,
                                     type=bool)
        if space_press:
            triggers |= SchemeEditWidget.SpaceKey

        any_press = settings.value("trigger-on-any-key",
                                   defaultValue=False,
                                   type=bool)
        if any_press:
            triggers |= SchemeEditWidget.AnyKey

        self.scheme_widget.setQuickMenuTriggers(triggers)

        settings.endGroup()
        settings.beginGroup("schemeedit")
        show_channel_names = settings.value("show-channel-names",
                                            defaultValue=True,
                                            type=bool)
        self.scheme_widget.setChannelNamesVisible(show_channel_names)

        node_animations = settings.value("enable-node-animations",
                                         defaultValue=False,
                                         type=bool)
        self.scheme_widget.setNodeAnimationEnabled(node_animations)
        settings.endGroup()

        self.__update_registry_filters()

    def __update_registry_filters(self):
        if self.widget_registry is None:
            return

        settings = QSettings()
        visible_state = {}
        for cat in self.widget_registry.categories():
            visible, _ = category_state(cat, settings)
            visible_state[cat.name] = visible

        self.__proxy_model.setFilters([
            FilterProxyModel.Filter(
                0, qt.QtWidgetRegistry.CATEGORY_DESC_ROLE,
                category_filter_function(visible_state))
        ])


def updated_flags(flags, mask, state):
    if state:
        flags |= mask
    else:
        flags &= ~mask
    return flags


def identity(item):
    return item


def index(sequence, *what, **kwargs):
    """index(sequence, what, [key=None, [predicate=None]])

    Return index of `what` in `sequence`.

    """
    what = what[0]
    key = kwargs.get("key", identity)
    predicate = kwargs.get("predicate", operator.eq)
    for i, item in enumerate(sequence):
        item_key = key(item)
        if predicate(what, item_key):
            return i
    raise ValueError("%r not in sequence" % what)

from ..registry import qt


def category_filter_function(state):
    def category_filter(desc):
        if not isinstance(desc, qt.CategoryDescription):
            # Is not a category item
            return True
        return state.get(desc.name, not desc.hidden)
    return category_filter


class UrlDropEventFilter(QObject):
    urlDropped = Signal(QUrl)

    def eventFilter(self, obj, event):
        etype = event.type()
        if  etype == QEvent.DragEnter or etype == QEvent.DragMove:
            mime = event.mimeData()
            if mime.hasUrls() and len(mime.urls()) == 1:
                url = mime.urls()[0]
                if url.scheme() == "file":
                    filename = six.text_type(url.toLocalFile())
                    _, ext = os.path.splitext(filename)
                    if ext == ".ows":
                        event.acceptProposedAction()
                        return True

        elif etype == QEvent.Drop:
            mime = event.mimeData()
            urls = mime.urls()
            if urls:
                url = urls[0]
                self.urlDropped.emit(url)
                return True

        return QObject.eventFilter(self, obj, event)


class RecentItem(SimpleNamespace):
    title = ""  # type: str
    path = ""  # type: str


def QSettings_readArray(settings, key, scheme):
    """
    Read the whole array from a QSettings instance

    Parameters
    ----------
    settings : QSettings
    key : str
    scheme : Dict[str, type]

    Example
    -------
    >>> s = QSettings("./login.ini")
    >>> QSettings_readArray(s, "array", {"username": str, "password": str})
    [{"username": "darkhelmet", "password": "1234"}}
    """
    from collections import Mapping
    items = []
    if not isinstance(scheme, Mapping):
        scheme = {key: None for key in scheme}

    count = settings.beginReadArray(key)
    for i in range(count):
        settings.setArrayIndex(i)
        keys = settings.allKeys()
        item = {}
        for key in keys:
            if key in scheme:
                vtype = scheme.get(key, None)
                if vtype is not None:
                    value = settings.value(key, type=vtype)
                else:
                    value = settings.value(key)
                item[key] = value
        items.append(item)
    settings.endArray()
    return items


def QSettings_writeArray(settings, key, values):
    # type: (QSettings, str, List[Dict[str, Any]]) -> None
    """
    Write an array of values to a QSettings instance.

    Parameters
    ----------
    settings : QSettings
    key : str
    values : List[Dict[str, Any]]

    Examples
    --------
    >>> s = QSettings("./login.ini")
    >>> QSettings_writeArray(
    ...     s, "array", [{"username": "darkhelmet", "password": "1234"}]
    ... )
    """
    settings.beginWriteArray(key, len(values))
    for i in range(len(values)):
        settings.setArrayIndex(i)
        for key_, val in values[i].items():
            settings.setValue(key_, val)
    settings.endArray()