import Orange
import orangecontrib.network as network

import time

def distances(adj, x, d, p=None):
    for y in adj[x]:
        if d[y] is not None: continue
        d[y] = d[x] + 1
        if p is not None: p[y] = x
        distances(adj, y, d, p)

def diameter(adj):
    n = len(adj)
    diam, dx, dy = 0, 0, 0
    for x in range(n):
        d = [None if i!=x else 0 for i in range(n)]
        distances(adj, x, d)
        mv, mi = max(zip(d, range(n)))
        if mv > diam:
            diam, dx, dy = mv, x, mi
    return dx, dy

def backbone(adj, x, y, t):
    n = len(adj)
    d = [None if i != x else 0 for i in range(n)]
    p = [None for i in range(n)]
    distances(adj, x, d, p)
    while y!=x:
        t[y] = d[y]
        y = p[y]
    t[x] = 0

def branches(adj, t):
    n = len(adj)
    for x in range(n):
        if t[x] is None: continue
        d = t[:]
        distances(adj, x, d)
        for y in range(n):
            if t[y] is None and d[y] is not None:
                t[y] = t[x]

def pseudo_time(n, edges):
    adj = [[] for i in range(n)]
    for a, b in edges:
        adj[a].append(b)
        adj[b].append(a)
    dx, dy = diameter(adj)
    t = [None for i in range(n)]
    backbone(adj, dx, dy, t)
    branches(adj, t)
    return t


def MST_Prim(data, dist):
    start = time.time()
    n = len(data)
    distance_to_tree = [float("inf") for i in range(n)]
    neighbour_in_tree = [None for i in range(n)]
    added = [False for i in range(n)]
    edges = []
    for r in range(n):  # repeat n times (add a new node each time)
        # find the node closest to the tree
        a = -1
        for i in range(n):
            if added[i]: continue
            if a==-1 or distance_to_tree[i] < distance_to_tree[a]:
                a = i
        # add node a to the tree
        if neighbour_in_tree[a] is not None:
            edges.append((a, neighbour_in_tree[a]))
        added[a] = True
        # update distances of nodes to tree
        if isinstance(dist, list):
            distance_a = dist[a]  # distance matrix
        else:
            distance_a = dist(data[a], data)[0]  # distance function
        for i in range(n):
            if added[i]: continue
            if distance_a[i] < distance_to_tree[i]:
                distance_to_tree[i] = distance_a[i]
                neighbour_in_tree[i] = a
    end = time.time()
    print("elapsed",end-start)

    times = pseudo_time(n, edges)
    ptime_var = Orange.data.ContinuousVariable("Pseudo time")
    new_domain = Orange.data.Domain(
        data.domain.attributes,
        data.domain.class_vars,
        data.domain.metas + (ptime_var,))
    new_table = data.transform(new_domain)
    new_table.get_column_view(ptime_var)[0][:] = times

    tree = network.Graph()
    tree.add_nodes_from(range(n))
    tree.add_edges_from(edges)
    tree.set_items(new_table)

    return new_table, tree


def Gephi_CSV(data, edges):
    f = open("nodes.csv", "w")
    lines = ["Id;Label\n"]
    for i,row in enumerate(data):
        if row.domain.class_var:
            lines.append("{};{}\n".format(i, str(row.get_class())))
        else:
            lines.append("{};{}\n".format(i, None))
    f.writelines(lines)
    f.close()

    f = open("edges.csv", "w")
    lines = ["Source;Target\n"]
    for a, b in edges:
        lines.append("{};{}\n".format(a,b))
    f.writelines(lines)
    f.close()


"""
tab = Orange.data.Table("iris")
tab, graph = MST_Prim(tab, Orange.distance.Euclidean)
#Gephi_CSV(tab, edges)
"""


tab = Orange.data.Table("brown-selected")
tab, graph = MST_Prim(tab, Orange.distance.Euclidean)
#Gephi_CSV(tab, edges)


"""
import numpy as np
f = open("jellyroll.txt","r")
X = np.array([list(map(float,line.split())) for line in f])
tab = Orange.data.Table.from_numpy(None, X)
tab, graph = MST_Prim(tab, Orange.distance.Euclidean)
#Gephi_CSV(tab, edges)
f.close()
"""

"""
import numpy as np
f = open("HSMM_adjacency.txt","r")
f.readline()
dist = [list(map(float,line.split()[1:])) for line in f.readlines()]
f.close()
f = open("HSMM_attributes.txt","r")
f.readline()
c = [int(line.split()[-1]) for line in f.readlines()]
f.close()
tab = Orange.data.Table.from_numpy(None, np.zeros((len(c),1)), c)
tab, graph = MST_Prim(tab, dist)
#edges = MST_Prim(tab, dist)
#Gephi_CSV(tab, edges)
"""


from AnyQt.QtWidgets import QApplication
from orangecontrib.network.widgets.OWNxExplorer import OWNxExplorer
import sys
a = QApplication(sys.argv)
ow = OWNxExplorer()
ow.set_graph(graph)
ow.show()
a.exec_()

