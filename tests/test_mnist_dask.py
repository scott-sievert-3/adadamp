import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torchvision import datasets, transforms
from torch.utils.data import Subset
import numpy as np
from distributed.utils_test import gen_cluster

from adadamp import DaskBaseDamper


class Model(nn.Module):
    """ modified from [1]
    [1]:https://github.com/pytorch/examples/blob/0f0c9131ca5c79d1332dce1f4c06fe942fbdc665/mnist/main.py
    """

    def __init__(self):
        super().__init__()
        self.conv = nn.Conv2d(1, 8, 3, 1)
        self.fc = nn.Linear(8 * 26 * 26, 10)

    def forward(self, x):
        x = F.relu(self.conv(x))
        x = torch.flatten(x, 1)
        x = F.relu(self.fc(x))
        output = F.log_softmax(x, dim=1)
        return output


@gen_cluster(client=True)
def test_mnist_w_dask(c, s, a, b):
    N = 4096
    batch_size = 1024
    net = DaskBaseDamper(
        module=Model,
        loss=nn.NLLLoss,
        optimizer=optim.Adadelta,
        optimizer__lr=1.0,
        batch_size=batch_size,
        grads_per_worker=batch_size // 2,
        max_epochs=1,
    )

    transform = transforms.Compose(
        [transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]
    )
    dataset = datasets.MNIST("./data", train=True, download=True, transform=transform)
    trimmed = Subset(dataset, np.arange(N).astype(int))
    #  n, d = 100, 10
    #  X = np.random.uniform(size=(n, d))
    #  y = np.random.uniform(size=n)
    #  _ = net.fit(X, y)
    _ = net.fit(trimmed)
    assert True  # sanity check
