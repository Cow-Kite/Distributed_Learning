import os
import time

import torch
from torch_geometric.datasets import Planetoid
import torch_geometric.loader as loader
import torch_geometric.transforms as T
import torch.nn as nn
from torch_geometric.nn import GCNConv
import torch.nn.functional as F
from filelock import FileLock

import horovod
import horovod.torch as hvd

start_time = time.time()

hvd.init()
torch.set_num_threads(1)

device = "cpu"

# dataset
data_dir = './data'
with FileLock(os.path.expanduser("~/.horovod_lock")):
    dataset = Planetoid(root=data_dir, name='Cora')
graph = dataset[0]

# graph clustering
cluster = loader.ClusterData(graph, num_parts=hvd.size())
clusterloader = loader.ClusterLoader(cluster)

clustered_datasets = []

for graph_data in clusterloader:
    clustered_datasets.append(graph_data)

# Model
class GCN(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = GCNConv(dataset.num_node_features, 16)
        self.conv2 = GCNConv(16, dataset.num_classes)

    def forward(self, data):
        x, edge_index = data.x, data.edge_index
        x = self.conv1(x, edge_index)
        x = F.relu(x)
        output = self.conv2(x, edge_index)
        return output

def graph_split(graph):
    split = T.RandomNodeSplit(num_val=0.1, num_test=0.2)
    graph = split(graph)
    return graph

# train 
def train_node_classifier(model, graph, optimizer, criterion, n_epochs=200):
    for epoch in range(1, n_epochs + 1):
        model.train()
        optimizer.zero_grad()
        out = model(graph)
        loss = criterion(out[graph.train_mask], graph.y[graph.train_mask])
        loss.backward()
        optimizer.step()

        if hvd.rank() == 0 and epoch % 10 == 0:
            print(f'Epoch: {epoch:03d}, Train Loss: {loss:.3f}')

gcn = GCN().to(device)
lr_scaler = hvd.size()
optimizer_gcn = torch.optim.Adam(gcn.parameters(), lr=0.01 * lr_scaler, weight_decay=5e-4)

hvd.broadcast_parameters(gcn.state_dict(), root_rank=0)
hvd.broadcast_optimizer_state(optimizer_gcn, root_rank=0)

optimizer_gcn = hvd.DistributedOptimizer(optimizer_gcn,
                                         named_parameters=gcn.named_parameters(),
                                         op=hvd.Average,
                                         gradient_predivide_factor=1.0)

criterion = nn.CrossEntropyLoss()

if hvd.rank() == 0:
    clustered_datasets[0] = graph_split(clustered_datasets[0])
    train_node_classifier(gcn, clustered_datasets[0], optimizer_gcn, criterion)
    print("총 소요 시간: %.3f초" %(time.time() - start_time))
if hvd.rank() == 1:
    clustered_datasets[1] = graph_split(clustered_datasets[1])
    train_node_classifier(gcn, clustered_datasets[1], optimizer_gcn, criterion)
if hvd.rank() == 2:
    clustered_datasets[2] = graph_split(clustered_datasets[2])
    train_node_classifier(gcn, clustered_datasets[2], optimizer_gcn, criterion)
if hvd.rank() == 3:
    clustered_datasets[3] = graph_split(clustered_datasets[3])
    train_node_classifier(gcn, clustered_datasets[3], optimizer_gcn, criterion)
