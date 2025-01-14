---
id: getting_started
title: Getting Started
---

This section shows you how to get your feet wet with BoTorch.

Before jumping the gun, we recommend you start with the high-level
[Overview](overview) to learn about the basic concepts in BoTorch.


## Installing BoTorch

#### Installation Requirements:

- Python >= 3.6
- PyTorch >= 1.1
- gpytorch >= 0.3.4
- scipy

BoTorch is easily installed via
[Anaconda](https://www.anaconda.com/distribution/#download-section) (recommended)
or `pip`:

<!--DOCUSAURUS_CODE_TABS-->
<!--conda-->
```bash
conda install botorch -c pytorch
```
<!--pip-->
```bash
pip install botorch
```
<!--END_DOCUSAURUS_CODE_TABS-->

For more detailed installation instructions, please see the
[Project Readme](https://github.com/pytorch/botorch/blob/master/README.md)
on GitHub.

## Basic Components

Here's a quick run down of the main components of a Bayesian Optimization loop.

1. Fit a Gaussian Process model to data
    ```python
    import torch
    from botorch.models import SingleTaskGP
    from botorch.fit import fit_gpytorch_model
    from gpytorch.mlls import ExactMarginalLogLikelihood

    train_X = torch.rand(10, 2)
    Y = 1 - torch.norm(train_X - 0.5, dim=-1) + 0.1 * torch.rand(10)
    train_Y = (Y - Y.mean()) / Y.std()

    gp = SingleTaskGP(train_X, train_Y)
    mll = ExactMarginalLogLikelihood(gp.likelihood, gp)
    fit_gpytorch_model(mll);
    ```

2. Construct an acquisition function
    ```python
    from botorch.acquisition import UpperConfidenceBound

    UCB = UpperConfidenceBound(gp, beta=0.1)
    ```

3. Optimize the acquisition function
    ```python
    from botorch.optim import joint_optimize

    bounds = torch.stack([torch.zeros(2), torch.ones(2)])
    candidate = joint_optimize(
        UCB, bounds=bounds, q=1, num_restarts=5, raw_samples=20,
    )
    ```


## Tutorials

Our Jupyter notebook tutorials help you get off the ground with BoTorch.
View and download them [here](../tutorials).


## API Reference

For an in-depth reference of the various BoTorch internals, see our
[API Reference](../api).


## Contributing

You'd like to contribute to BoTorch? Great! Please see
[here](https://github.com/pytorch/botorch/blob/master/CONTRIBUTING.md)
for how to help out.
