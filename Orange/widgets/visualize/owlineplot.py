import sys

from types import SimpleNamespace as namespace

import numpy as np
from PyQt4 import QtGui, QtCore
from PyQt4.QtGui import QStyle, QGraphicsItem, QPen, QColor
from PyQt4.QtCore import Qt, QPointF

import pyqtgraph as pg

import Orange.data
from Orange.preprocess import Normalize

from Orange.widgets import widget, gui, settings
from Orange.widgets.utils import colorpalette


def is_discrete(var):
    return isinstance(var, Orange.data.DiscreteVariable)


def is_string(var):
    return isinstance(var, Orange.data.StringVariable)


def disconnected_curve_data(data, x=None):
    C, P = data.shape
    if x is not None:
        x = np.asarray(x)
        if x.shape != (P,):
            raise ValueError("x must have shape ({},)".format(P))
    else:
        x = np.arange(P)

    validmask = np.isfinite(data)
    validdata = data[validmask]
    row_count = np.sum(validmask, axis=1)
    connect = np.ones(np.sum(row_count), dtype=bool)
    connect[np.cumsum(row_count)[:-1] - 1] = False
    X = np.tile(x, C)[validmask.ravel()]
    return X, validdata, connect


def shape_from_path(path, width=1,):
    stroker = QtGui.QPainterPathStroker()
    stroker.setWidth(width)
    return stroker.createStroke(path)


class HoverCurve(pg.PlotCurveItem):
    def __init__(self, *args, **kwargs):
        self.__shape = None
        super().__init__(*args, **kwargs)
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setBoundingRegionGranularity(0.1)

    def shape(self):
        if self.__shape is None:
            path = self.getPath()
            d = self.pixelLength(QPointF(0, self.opts["mouseWidth"]))
            self.__shape = shape_from_path(path, width=d)
        return self.__shape

    def boundingRect(self):
        shape = self.shape()
        return shape.controlPointRect()

    def contains(self, point):
        return self.shape().contains(point)

    def collidesWithPath(self, path, mode=Qt.IntersectsItemShape):
        if mode == Qt.IntersectsItemShape:
            return path.intersects(self.shape())
        elif mode == Qt.ContainsItemShape:
            return path.contains(self.shape())
        elif mode == Qt.IntersectsItemBoundingRect:
            return path.contains(self.boundingRect())
        elif mode == Qt.ContainsItemBoundingRect:
            return path.contains(self.boundingRect())

    def hoverEnterEvent(self, event):
        event.accept()
        self.update()

    def hoverLeaveEvent(self, event):
        event.accept()
        self.update()

    def setData(self, *args, **kwargs):
        self.__shape = None
        super().setData(*args, **kwargs)

    def viewTransformChanged(self):
        self.__shape = None
        self.prepareGeometryChange()
        super().viewTransformChanged()

    def paint(self, painter, option, widget):
        if option.state & QStyle.State_MouseOver or self.isSelected():
            super().paint(painter, option, widget)


# TODO:
#  * Box plot item
#  * Speed up single profile hover and selection renders

class OWLinePlot(widget.OWWidget):
    name = "Line Plot"
    description = "Visualization of data profiles (e.g., time series)."
    icon = "icons/LinePlot.svg"
    priority = 1030

    inputs = [("Data", Orange.data.Table, "set_data")]
    outputs = [("Selected Data", Orange.data.Table)]
    settingsHandler = settings.DomainContextHandler()

    group_var = settings.Setting("")                #: Group by group_var's values
    selected_classes = settings.Setting([])         #: List of selected class indices
    display_individual = settings.Setting(False)    #: Show individual profiles
    display_average = settings.Setting(True)        #: Show average profile
    display_quartiles = settings.Setting(True)      #: Show data quartiles
    normalize_data = settings.Setting(False)        #: Scale data to range [0,1]
    annot_index = settings.ContextSetting(0)        #: Profile label/id colum
    auto_commit = settings.Setting(True)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.classes = []

        self.data = None
        self.annotation_variables = []
        self.group_variables = []
        self.graph_variables = []
        self.__groups = None
        self.__selected_data_indices = []

        # Setup GUI
        infobox = gui.widgetBox(self.controlArea, "Info")
        self.infoLabel = gui.widgetLabel(infobox, "No data on input.")
        displaybox = gui.widgetBox(self.controlArea, "Display")
        gui.checkBox(displaybox, self, "display_individual",
                     "Expression Profiles",
                     callback=self.__update_visibility)
        gui.checkBox(displaybox, self, "display_quartiles", "Quartiles",
                     callback=self.__update_visibility)
        gui.checkBox(displaybox, self, "normalize_data", "Normalized data",
                     callback=self._setup_plot)

        group_box = gui.widgetBox(self.controlArea, "Classes")
        self.cb_attr = gui.comboBox(
            group_box, self, "group_var", sendSelectedValue=True,
            callback=self.update_group_var)
        self.group_listbox = gui.listBox(
            group_box, self, "selected_classes", "classes",
            selectionMode=QtGui.QListWidget.MultiSelection,
            callback=self.__on_class_selection_changed)
        self.unselectAllClassedQLB = gui.button(
            group_box, self, "Unselect all",
            callback=self.__select_all_toggle)

        self.annot_cb = gui.comboBox(
            self.controlArea, self, "annot_index", box="Profile Labels",
            callback=self.__update_tooltips)

        gui.rubber(self.controlArea)
        gui.auto_commit(self.controlArea, self, "auto_commit", "Commit")

        self.graph = pg.PlotWidget(background="w", enableMenu=False)
        self.graph.setRenderHint(QtGui.QPainter.Antialiasing, True)
        self.graph.scene().selectionChanged.connect(
            self.__on_curve_selection_changed)
        self.mainArea.layout().addWidget(self.graph)

    def sizeHint(self):
        return QtCore.QSize(800, 600)

    def clear(self):
        """
        Clear/reset the widget state.
        """
        self.cb_attr.clear()
        self.group_listbox.clear()
        self.annot_cb.clear()
        self.data = None
        self.annotation_variables = []
        self.__groups = None
        self.__selected_data_indices = []
        self.graph.clear()

    def set_data(self, data):
        """
        Set the input profile dataset.
        """
        self.closeContext()
        self.clear()

        self.data = data
        if data is not None:
            n_instances = len(data)
            n_attrs = len(data.domain.attributes)
            self.infoLabel.setText("%i instances on input\n%i attributes"%(n_instances, n_attrs))

            annotvars = [var for var in data.domain.variables + data.domain.metas
                         if is_discrete(var) or is_string(var)]
            for var in annotvars:
                self.annot_cb.addItem(*gui.attributeItem(var))
            if data.domain.class_var in annotvars:
                self.annot_index = annotvars.index(data.domain.class_var)
            self.annotation_variables = annotvars

            self.graph_variables = [var for var in data.domain.attributes
                                    if var.is_continuous]

            groupvars = [var for var in data.domain.variables + data.domain.metas
                        if is_discrete(var)]
            if len(groupvars) > 0:
                self.cb_attr.addItems([str(var) for var in groupvars])
                self.group_var = str(groupvars[0])
                self.group_variables = groupvars
                self.update_group_var()

            self.openContext(data)

        self.commit()

    def _setup_plot(self):
        """Setup the plot with new curve data."""
        assert self.data is not None
        self.graph.clear()

        data, domain = self.data, self.data.domain
        var = domain[self.group_var]
        class_col_data, _ = data.get_column_view(var)
        group_indices = [np.flatnonzero(class_col_data == i)
                         for i in range(len(self.classes))]

        self.graph.getAxis('bottom').setTicks([
            [(i+1, str(a)) for i, a in enumerate(self.graph_variables)]
        ])

        X = np.arange(1, len(self.graph_variables)+1)
        groups = []

        for i, indices in enumerate(group_indices):
            if len(indices) == 0:
                groups.append(None)
            else:
                if self.classes:
                    color = self.class_colors[i]
                else:
                    color = QColor(Qt.darkGray)
                group_data = data[indices, self.graph_variables]
                if self.normalize_data:
                    group_data = Normalize(group_data, norm_type=Normalize.NormalizeBySpan)
                plot_x, plot_y, connect = disconnected_curve_data(group_data.X, x=X)

                color.setAlpha(200)
                lightcolor = QColor(color.lighter(factor=150))
                lightcolor.setAlpha(150)
                pen = QPen(color, 2)
                pen.setCosmetic(True)

                lightpen = QPen(lightcolor, 1)
                lightpen.setCosmetic(True)
                hoverpen = QPen(pen)
                hoverpen.setWidth(2)

                curve = pg.PlotCurveItem(
                    x=plot_x, y=plot_y, connect=connect,
                    pen=lightpen, symbolSize=2, antialias=True,
                )
                self.graph.addItem(curve)

                hovercurves = []
                for index, profile in zip(indices, group_data.X):
                    hcurve = HoverCurve(x=X, y=profile, pen=hoverpen,
                                        antialias=True)
                    hcurve.setToolTip('{}'.format(index))
                    hcurve._data_index = index
                    hovercurves.append(hcurve)
                    self.graph.addItem(hcurve)

                mean = np.nanmean(group_data.X, axis=0)

                meancurve = pg.PlotDataItem(
                    x=X, y=mean, pen=pen, size=5, symbol="o", pxMode=True,
                    symbolSize=5, antialias=True
                )
                hoverpen = QPen(hoverpen)
                hoverpen.setWidth(5)

                meanhover = HoverCurve(x=X, y=mean, pen=hoverpen, antialias=True)
                meanhover.setFlag(QGraphicsItem.ItemIsSelectable, False)
                self.graph.addItem(meanhover)

                self.graph.addItem(meancurve)
                q1, q2, q3 = np.nanpercentile(group_data.X, [25, 50, 75], axis=0)
                # TODO: implement and use a box plot item
                errorbar = pg.ErrorBarItem(
                    x=X, y=mean,
                    bottom=np.clip(mean - q1, 0, mean - q1),
                    top=np.clip(q3 - mean, 0, q3 - mean),
                    beam=0.5
                )
                self.graph.addItem(errorbar)
                groups.append(
                    namespace(
                        data=group_data, indices=indices,
                        profiles=curve, hovercurves=hovercurves,
                        mean=meancurve, meanhover=meanhover,
                        boxplot=errorbar)
                )

        self.__groups = groups
        self.__update_visibility()
        self.__update_tooltips()

    def __update_visibility(self):
        if self.__groups is None:
            return

        if self.classes:
            selected = lambda i: i in self.selected_classes
        else:
            selected = lambda i: True
        for i, group in enumerate(self.__groups):
            if group is not None:
                isselected = selected(i)
                group.profiles.setVisible(isselected and self.display_individual)
                group.mean.setVisible(isselected)
                group.meanhover.setVisible(isselected)
                group.boxplot.setVisible(isselected and self.display_quartiles)
                for hc in group.hovercurves:
                    hc.setVisible(isselected and self.display_individual)

    def __update_tooltips(self):
        if self.__groups is None:
            return

        if 0 <= self.annot_index < len(self.annotation_variables):
            annotvar = self.annotation_variables[self.annot_index]
            column, _ = self.data.get_column_view(annotvar)
            column = [annotvar.str_val(val) for val in column]
        else:
            annotvar = None
            column = [str(i) for i in range(len(self.data))]

        for group in self.__groups:
            if group is not None:
                for hcurve in group.hovercurves:
                    if hasattr(hcurve, '_data_index'):
                        value = column[hcurve._data_index]
                        hcurve.setToolTip(value)

    def __select_all_toggle(self):
        allselected = len(self.selected_classes) == len(self.classes)
        if allselected:
            self.selected_classes = []
        else:
            self.selected_classes = list(range(len(self.classes)))

        self.__on_class_selection_changed()

    def __on_class_selection_changed(self):
        mask = [i in self.selected_classes
                for i in range(self.group_listbox.count())]
        self.unselectAllClassedQLB.setText(
            "Select all" if not all(mask) else "Unselect all")

        self.__update_visibility()

    def __on_annotation_index_changed(self):
        self.__update_tooltips()

    def __on_curve_selection_changed(self):
        if self.data is not None:
            selected = self.graph.scene().selectedItems()
            indices = [item._data_index for item in selected]
            self.__selected_data_indices = np.array(indices, dtype=int)
            self.commit()

    def commit(self):
        subset = None
        if self.data is not None and len(self.__selected_data_indices) > 0:
            subset = self.data[self.__selected_data_indices]

        self.send("Selected Data", subset)

    def update_group_var(self):
        data_attr, _ = self.data.get_column_view(self.group_var)
        class_vals = self.data.domain[self.group_var].values
        self.classes = list(class_vals)
        self.class_colors = \
            colorpalette.ColorPaletteGenerator(len(class_vals))
        self.selected_classes = list(range(len(class_vals)))
        for i in range(len(class_vals)):
            item = self.group_listbox.item(i)
            item.setIcon(colorpalette.ColorPixmap(self.class_colors[i]))

        self._setup_plot()
        self.__on_class_selection_changed()


def test_main(argv=sys.argv):
    a = QtGui.QApplication(argv)
    if len(argv) > 1:
        filename = argv[1]
    else:
        filename = "brown-selected"
    w = OWLinePlot()
    d = Orange.data.Table(filename)

    w.set_data(d)
    w.show()
    r = a.exec_()
    w.saveSettings()
    return r

if __name__ == "__main__":
    sys.exit(test_main())
