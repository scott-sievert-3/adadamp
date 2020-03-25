from typing import List, Dict, Tuple, Any, Union, Optional, Callable
import itertools
from pprint import pprint
from time import time

import pandas as pd
import torch.nn as nn
import torch

from .damping import AdaDamp, GeoDamp, PadaDamp, BaseDamper, ConvergenceError

Number = Union[int, float]


def breakpoint():
    import pdb

    pdb.set_trace()


def run(
    model=None,
    opt=None,
    train_set=None,
    test_set=None,
    args=None,
    test_freq: Optional[Number] = None,
    train_stats: bool = True,
    verbose: bool = False,
    device: str = "cpu",
):
    data = []
    train_data = []
    for k in itertools.count():
        test_kwargs = dict(model=model, loss=opt._loss, device=device)
        train_stats = {}
        if train_stats:
            train_stats = test(dataset=train_set, prefix="train", **test_kwargs)
        test_stats = test(dataset=test_set, prefix="test", **test_kwargs)
        data.append({**args, **opt.meta, **train_stats, **test_stats})
        if verbose:
            _s = {
                k: v
                for k, v in data[-1].items()
                if k in data[-1]
                and k
                in [
                    "damper",
                    "lr_",
                    "model_updates",
                    "batch_size",
                    "train_loss",
                    "best_train_loss",
                    "epochs",
                    "damping",
                    "test_accuracy",
                    "train_accuracy",
                ]
            }
            pprint(_s)
        epoch = data[-1]["epochs"]
        if epoch >= args["epochs"]:
            break
        try:
            model, opt, epoch_meta, epoch_data = train(
                model,
                opt,
                verbose=args["verbose"],
                epochs=1 if test_freq is None or epoch > 5 else test_freq,
            )
        except ConvergenceError as e:
            print(e)
            break
        train_data += epoch_data
        data[-1].update(epoch_meta)

    return data, train_data


def train(
    model: nn.Module,
    opt: BaseDamper,
    verbose: Optional[Union[int, bool]] = None,
    epochs=1,
) -> Tuple[nn.Module, BaseDamper, Dict[str, Any], List[Dict]]:
    """
    Function to train for at least one epoch.

    Arguments
    ---------
    model : nn.Module
        PyTorch model.
    opt : Union[AdaDamp, PadaDamp]
        Optimizer. Must be a subclass of BaseDamper
    verbose : int, float, None, default=None
        Controls printing. Higher values print more frequently, specifically
        approximately every ``1 / verbose`` fraction of the dataset;
        setting ``verbose == 10`` will mean it prints 10 times per epoch.

    Returns
    -------
    model : nn.Module
        The update model.

    """
    if not isinstance(opt, BaseDamper):
        raise ValueError(
            "Argument ``opt`` is not an instance of BaseDamper. "
            "(passing AdaDamp, PadaDamp or GeoDamp will resolve this issue)"
        )
    if verbose:
        verbose = int(verbose) if isinstance(verbose, bool) else verbose
        print_eg = int(len(opt._dataset) / verbose)
    start_examples = opt._meta["num_examples"]
    old_examples = opt._meta["num_examples"]
    data = []
    _loop_start = time()
    while True:
        num_examples_so_far = opt._meta["num_examples"] - start_examples
        if num_examples_so_far >= epochs * len(opt._dataset):
            break
        opt.step()
        data.append(opt.meta)
        if verbose and opt._meta["num_examples"] >= old_examples + print_eg:
            frac = opt._meta["num_examples"] / opt._meta["len_dataset"]
            print(f"Epochs: {frac:0.2f}")
            pprint(opt._meta)
            old_examples = opt._meta["num_examples"]
    meta = {
        "_epochs": epochs,
        "_num_examples": num_examples_so_far,
        "_train_time": time() - _loop_start,
    }
    return model, opt, meta, data


def test(
    model=None, loss=None, dataset=None, device: str="cpu", batch_size=1000, prefix=""
):
    assert isinstance(device, str)
    def _test(model):
        test_loss = 0
        correct = 0
        kwargs = {"num_workers": 1, "pin_memory": True} if "cuda" in device else {}
        loader = torch.utils.data.DataLoader(dataset, batch_size=batch_size, **kwargs)
        _device = torch.device(device)
        model = model.to(_device)
        for data, target in loader:
            data, target = data.to(_device), target.to(_device)
            output = model(data)
            test_loss += loss(
                output, target, reduction="sum"
            ).item()  # sum up batch loss
            if "mse" in loss.__name__:
                continue
            pred = output.argmax(
                dim=1, keepdim=True
            )  # get the index of the max log-probability
            correct += pred.eq(target.view_as(pred)).sum().item()

        test_loss /= len(dataset)
        acc = correct / len(dataset)
        return {"loss": test_loss, "accuracy": acc}

    ret = {"loss": 0}
    model.eval()
    with torch.no_grad():
        ret = _test(model)
        ret.update({"batch_size": batch_size, "device": device, "prefix": prefix})
    return {f"{prefix}_{k}": v for k, v in ret.items()}
