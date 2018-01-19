import Orange

import time

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
        distance_a = dist(data[a], data)[0]
        for i in range(n):
            if added[i]: continue
            if distance_a[i] < distance_to_tree[i]:
                distance_to_tree[i] = distance_a[i]
                neighbour_in_tree[i] = a
    end = time.time()
    print("elapsed",end-start)
    return edges

def Gephi_CSV(data, edges):
    f = open("nodes.csv", "w")
    lines = ["Id;Label\n"]
    for i,row in enumerate(data):
        lines.append("{};{}\n".format(i, str(row.get_class())))
    f.writelines(lines)
    f.close()

    f = open("edges.csv", "w")
    lines = ["Source;Target\n"]
    for a, b in edges:
        lines.append("{};{}\n".format(a,b))
    f.writelines(lines)
    f.close()

tab = Orange.data.Table("brown-selected")
edges = MST_Prim(tab, Orange.distance.Euclidean)
Gephi_CSV(tab, edges)
