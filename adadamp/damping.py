from typing import Callable, Dict, Any, Tuple, Set, List, Optional
from pprint import pprint
from copy import copy
import itertools
from time import time

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset
from torch.utils.data import BatchSampler, RandomSampler, SequentialSampler
from torch.optim import Optimizer
import torch.nn.functional as F
import torch.nn as nn


def breakpoint():
    import pdb

    pdb.set_trace()


class BaseDamper(Optimizer):
    def __init__(
        self,
        model: nn.Module,
        dataset: Dataset,
        opt: Optimizer,
        loss: Callable = F.nll_loss,
        initial_batch_size: int = 1,
        device: str = "cpu",
        max_batch_size: Optional[int] = None,
        best_train_loss: Optional[float] = None,
        **kwargs,
    ):
        """
        Damp the noise in the gradient estimate.

        Arguments
        ---------
        model : nn.Module
            The model to train
        dataset : torch.Dataset
            Dataset to use for training
        opt : torch.optim.Optimizer
            The optimizer to use
        loss : callable (function), default=torch.nn.F.nll_loss
            The loss function to use. Must support the reduction keyword. Signature:

                loss(output, target, reduction="sum")

        initial_batch_size : int, default=1
            Initial batch size
        device : str, default="cpu"
            The device to use.
        max_batch_size : int, None, default=None
            The maximum batch size. If the batch size is larger than this
            value, the learning rate is decayed by an appropriate amount. If None, will automatically be set to be the size of the dataset.
        kwargs : dict
            Arguments to pass to the underlying torch.DataLoader

        Notes
        -----
        By default, this class does not perform any damping (but it's children
        do). If a function needs an instance of BaseDamper, this class can wrap
        any optimizer.

        """
        self._params: Set[str] = {
            "device_type",
            "initial_batch_size",
            "loss_name",
            "max_batch_size",
        }
        self.initial_batch_size = initial_batch_size
        self.loss = loss
        if max_batch_size is None:
            max_batch_size = len(dataset)
        self.max_batch_size = max_batch_size
        self.model = model

        self._meta: Dict[str, Any] = {
            "model_updates": 0,
            "num_examples": 0,
            "batch_loss": None,
            "num_params": sum([m.nelement() for m in model.parameters()]),
            "len_dataset": len(dataset),
            "damper": opt.__class__.__name__.lower(),
        }
        self._meta.update({f"opt_param_{k}": v for k, v in opt.defaults.items()})
        self.opt = opt
        self.dataset = dataset
        self.loss = loss
        self.param_groups = self.opt.param_groups
        self.device = torch.device(device)
        sampler = RandomSampler(dataset, replacement=True)
        self.loader = DataLoader(dataset, sampler=sampler, drop_last=True, **kwargs,)
        self._data_iter = iter(self.loader)
        self._initial_lr = self._get_lr()

    def step(self, **kwargs):
        start = time()
        damping = self.damping()
        self._meta["damping_time"] = time() - start
        self.loader.batch_sampler.batch_size = int(damping)

        # Is the batch size too large? If so, decay the learning rate
        current_bs = self.loader.batch_sampler.batch_size
        max_bs = self.max_batch_size
        if max_bs is not None and current_bs >= max_bs:
            self._set_lr(self._initial_lr * max_bs / current_bs)
            self.loader.batch_sampler.batch_size = max_bs

        batch_loss, num_examples = self._step(**kwargs)

        self._meta["model_updates"] += 1
        self._meta["time"] = time()
        self._meta["step_time"] = time() - start
        self._meta["damping"] = damping
        self._meta["lr_"] = self._get_lr()
        self._meta["num_examples"] += num_examples
        self._meta["batch_loss"] = batch_loss
        self._meta["damping"] = damping
        self._meta["batch_size"] = self.loader.batch_sampler.batch_size

    def damping(self):
        """
        Damp the noise in the gradient approximation.

        Notes
        -----
        - Should make use of self.initial_batch_size
        - This is the main class for subclasses to overwrite. By default, it
          wraps an optimizer with a static self.initial_batch_size

        """
        return self.initial_batch_size

    def _step(self, **kwargs):
        start = time()
        try:
            data, target = next(self._data_iter)
        except StopIteration:
            # self.loader.batch_sampler.batch_size < 0?
            self._data_iter = iter(self.loader)
            data, target = next(self._data_iter)
        assert self.loader.batch_sampler.batch_size > 0

        data, target = data.to(self.device), target.to(self.device)
        self.opt.zero_grad()
        output = self.model(data)
        loss = self.loss(output, target, reduction="sum")
        loss *= 1 / len(data)
        loss.backward()
        self.opt.step(**kwargs)
        self._meta["_step_time"] = time() - start
        return loss.item(), len(data)

    def _set_lr(self, lr):
        for group in self.opt.param_groups:
            group["lr"] = lr
        return self.opt

    def _get_lr(self):
        lrs = [group["lr"] for group in self.opt.param_groups]
        assert all(lr == lrs[0] for lr in lrs)
        return lrs[0]

    def get_params(self):
        params = {k: v for k, v in self.__dict__.items() if k in self._params}
        return params

    @property
    def meta(self):
        d = copy(self._meta)
        d.update(self.get_params())
        d["device_type"] = self.device.type
        d["loss_name"] = self.loss.__name__
        d["epochs"] = d["num_examples"] / d["len_dataset"]
        return d

    def get_loss(
        self, dataset: Optional[Dataset] = None, frac: Optional[float] = None,
    ):
        if dataset is None:
            dataset = self.dataset
        num_eg = len(dataset)
        if frac is not None:
            num_eg = int(frac * len(dataset))

        kwargs = (
            {"num_workers": 1, "pin_memory": True} if torch.cuda.is_available() else {}
        )
        loader = torch.utils.data.DataLoader(dataset, batch_size=1000, **kwargs)

        total_loss = 0
        _num_eg = 0
        with torch.no_grad():
            for data, target in loader:
                data, target = data.to(self.device), target.to(self.device)
                _num_eg += len(data)
                output = self.model(data)
                loss = self.loss(output, target, reduction="sum")
                total_loss += loss.item()
                if frac is not None and _num_eg >= num_eg:
                    break
        if frac is None:
            assert _num_eg == len(dataset)
        return total_loss / _num_eg


class AdaDamp(BaseDamper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._meta["damper"] = "adadamp"

    def damping(self):
        loss = self.get_loss()
        if self._meta["model_updates"] == 0:
            self._meta["_initial_loss"] = loss
        self._meta["_complete_loss"] = loss
        if np.isnan(loss):
            return 1
        initial_loss = self._meta["_initial_loss"]
        if self._meta["best_train_loss"] is not None:
            initial_loss -= self._meta["best_train_loss"]
            loss -= self._meta["best_train_loss"]
        bs = _ceil(self.initial_batch_size * initial_loss / loss)
        return bs


class PadaDamp(BaseDamper):
    def __init__(self, *args, rate=None, **kwargs):
        """
        Parameters
        ----------
        args : list
            Passed to BaseDamper

        rate : float
            The rate to increase the damping by. That is, set the batch size to be

            .. math::

                B_0 + ceil(rate * k)

            where k is the number of model updates.

        Notes
        -----
        The number of epochs is

        .. math::

            uB_0 + \sum_{i=1}^u ceil(rate * k)

        for u model updates.

        """
        self.rate = rate
        super().__init__(*args, **kwargs)
        self._meta["damper"] = "padadamp"

    def damping(self):
        k = self.meta["model_updates"]
        bs = self.initial_batch_size + _ceil(self.rate * k)
        return bs


class GeoDamp(BaseDamper):
    def __init__(self, *args, dampingdelay=5, dampingfactor=2, **kwargs):
        self.dampingdelay = dampingdelay
        self.dampingfactor = dampingfactor
        super().__init__(*args, **kwargs)
        self._meta["damper"] = "geodamp"

    def damping(self):
        assert self.dampingfactor >= 1
        epochs = self.meta["num_examples"] / self.meta["len_dataset"]
        factor = self.dampingfactor ** (epochs // self.dampingdelay)
        return self.initial_batch_size * factor


class GeoDampLR(GeoDamp):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._meta["damper"] = "geodamplr"
        self._last_factor = None
        self.max_batch_size = self.initial_batch_size


class CntsDampLR(BaseDamper):
    def __init__(self, *args, dampingfactor=0.02, **kwargs):
        super().__init__(*args, **kwargs)
        self._meta["damper"] = "cntsdamplr"
        self.dampingfactor = dampingfactor
        self.max_batch_size = self.initial_batch_size

    def damping(self):
        k = self._meta["model_updates"]
        bs = np.round(self.initial_batch_size + 1 + self.dampingfactor * (k + 1))
        return bs


def _ceil(x):
    return int(x) + 1