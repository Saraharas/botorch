#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

r"""
Analytic Acquisition Functions that evaluate the posterior without performing
Monte-Carlo sampling.
"""

from abc import ABC
from copy import deepcopy
from typing import Dict, Optional, Tuple, Union

import torch
from torch import Tensor
from torch.distributions import Normal

from ..exceptions import UnsupportedError
from ..models.gp_regression import FixedNoiseGP
from ..models.gpytorch import GPyTorchModel
from ..models.model import Model
from ..posteriors.posterior import Posterior
from ..sampling.samplers import SobolQMCNormalSampler
from ..utils.transforms import convert_to_target_pre_hook, t_batch_mode_transform
from .acquisition import AcquisitionFunction
from .objective import ScalarizedObjective


class AnalyticAcquisitionFunction(AcquisitionFunction, ABC):
    r"""Base class for analytic acquisition functions."""

    def __init__(
        self, model: Model, objective: Optional[ScalarizedObjective] = None
    ) -> None:
        super().__init__(model=model)
        if objective is not None and not isinstance(objective, ScalarizedObjective):
            raise UnsupportedError(
                "Only objectives of type ScalarizedObjective are supported for "
                "analytic acquisition functions."
            )
        self.objective = objective

    def _get_posterior(self, X: Tensor, check_single_output: bool = True) -> Posterior:
        r"""Compute the posterior at the input candidate set X.

        Applies the objective if provided.

        Args:
            X: The input candidate set
            check_single_output: If True, Raise an error if the posterior is not
                single-output.

        Returns:
            The posterior at X.
        """
        posterior = self.model.posterior(X)
        if self.objective is not None:
            # Unlike MCAcquisitionObjectives (which transform samples), this
            # transforms the posterior
            posterior = self.objective(posterior)
        if check_single_output:
            if posterior.event_shape[-1] != 1:
                raise UnsupportedError(
                    "Multi-Output posteriors are not supported for acquisition "
                    f"functions of type {self.__class__.__name__}"
                )
        return posterior

    def set_X_pending(self, X_pending: Optional[Tensor] = None) -> None:
        raise UnsupportedError(
            f"Analytic acquisition functions do not account for X_pending yet."
        )


class ExpectedImprovement(AnalyticAcquisitionFunction):
    r"""Single-outcome Expected Improvement (analytic).

    Computes classic Expected Improvement over the current best observed value,
    using the analytic formula for a Normal posterior distribution. Unlike the
    MC-based acquisition functions, this relies on the posterior at single test
    point being Gaussian (and require the posterior to implement `mean` and
    `variance` properties). Only supports the case of `q=1`. The model must be
    single-outcome.

    `EI(x) = E(max(y - best_f, 0)), y ~ f(x)`

    Example:
        >>> model = SingleTaskGP(train_X, train_Y)
        >>> EI = ExpectedImprovement(model, best_f=0.2)
        >>> ei = EI(test_X)
    """

    def __init__(
        self,
        model: Model,
        best_f: Union[float, Tensor],
        objective: Optional[ScalarizedObjective] = None,
        maximize: bool = True,
    ) -> None:
        r"""Single-outcome Expected Improvement (analytic).

        Args:
            model: A fitted single-outcome model.
            best_f: Either a scalar or a `b`-dim Tensor (batch mode) representing
                the best function value observed so far (assumed noiseless).
            maximize: If True, consider the problem a maximization problem.
            objective: An ScalarizedObjective (optional).
        """
        super().__init__(model=model, objective=objective)
        self.maximize = maximize
        if not torch.is_tensor(best_f):
            best_f = torch.tensor(best_f)
        self.register_buffer("best_f", best_f)

    @t_batch_mode_transform(expected_q=1)
    def forward(self, X: Tensor) -> Tensor:
        r"""Evaluate Expected Improvement on the candidate set X.

        Args:
            X: A `b1 x ... bk x 1 x d`-dim batched tensor of `d`-dim design points.
                Expected Improvement is computed for each point individually,
                i.e., what is considered are the marginal posteriors, not the
                joint.

        Returns:
            A `b1 x ... bk`-dim tensor of Expected Improvement values at the
            given design points `X`.
        """
        self.best_f = self.best_f.to(X)
        posterior = self._get_posterior(X=X)
        mean = posterior.mean
        # deal with batch evaluation and broadcasting
        view_shape = mean.shape[:-2] if mean.dim() >= X.dim() else X.shape[:-2]
        mean = mean.view(view_shape)
        sigma = posterior.variance.clamp_min(1e-9).sqrt().view(view_shape)
        u = (mean - self.best_f.expand_as(mean)) / sigma
        if not self.maximize:
            u = -u
        normal = Normal(torch.zeros_like(u), torch.ones_like(u))
        ucdf = normal.cdf(u)
        updf = torch.exp(normal.log_prob(u))
        ei = sigma * (updf + u * ucdf)
        return ei


class PosteriorMean(AnalyticAcquisitionFunction):
    r"""Single-outcome Posterior Mean.

    Only supports the case of q=1. Requires the model's posterior to have a
    `mean` property. The model must be single-outcome.

    Example:
        >>> model = SingleTaskGP(train_X, train_Y)
        >>> PM = PosteriorMean(model)
        >>> pm = PM(test_X)
    """

    @t_batch_mode_transform(expected_q=1)
    def forward(self, X: Tensor) -> Tensor:
        r"""Evaluate the posterior mean on the candidate set X.

        Args:
            X: A `(b) x 1 x d`-dim Tensor of `(b)` t-batches of `d`-dim design
                points each.

        Returns:
            A `(b)`-dim Tensor of Posterior Mean values at the given design
            points `X`.
        """
        posterior = self._get_posterior(X=X)
        return posterior.mean.view(X.shape[:-2])


class ProbabilityOfImprovement(AnalyticAcquisitionFunction):
    r"""Single-outcome Probability of Improvement.

    Probability of improvment over the current best observed value, computed
    using the analytic formula under a Normal posterior distribution. Only
    supports the case of q=1. Requires the posterior to be Gaussian. The model
    must be single-outcome.

    `PI(x) = P(y >= best_f), y ~ f(x)`

    Example:
        >>> model = SingleTaskGP(train_X, train_Y)
        >>> PI = ProbabilityOfImprovement(model, best_f=0.2)
        >>> pi = PI(test_X)
    """

    def __init__(
        self,
        model: Model,
        best_f: Union[float, Tensor],
        objective: Optional[ScalarizedObjective] = None,
        maximize: bool = True,
    ) -> None:
        r"""Single-outcome analytic Probability of Improvement.

        Args:
            model: A fitted single-outcome model.
            best_f: Either a scalar or a `b`-dim Tensor (batch mode) representing
                the best function value observed so far (assumed noiseless).
            maximize: If True, consider the problem a maximization problem.
        """
        super().__init__(model=model, objective=objective)
        self.maximize = maximize
        if not torch.is_tensor(best_f):
            best_f = torch.tensor(best_f)
        self.register_buffer("best_f", best_f)

    @t_batch_mode_transform(expected_q=1)
    def forward(self, X: Tensor) -> Tensor:
        r"""Evaluate the Probability of Improvement on the candidate set X.

        Args:
            X: A `(b) x 1 x d`-dim Tensor of `(b)` t-batches of `d`-dim design
                points each.

        Returns:
            A `(b)`-dim tensor of Probability of Improvement values at the given
            design points `X`.
        """
        self.best_f = self.best_f.to(X)
        posterior = self._get_posterior(X=X)
        mean, sigma = posterior.mean, posterior.variance.sqrt()
        batch_shape = X.shape[:-2]
        mean = posterior.mean.view(batch_shape)
        sigma = posterior.variance.sqrt().clamp_min(1e-9).view(batch_shape)
        u = (mean - self.best_f.expand_as(mean)) / sigma
        if not self.maximize:
            u = -u
        normal = Normal(torch.zeros_like(u), torch.ones_like(u))
        return normal.cdf(u)


class UpperConfidenceBound(AnalyticAcquisitionFunction):
    r"""Single-outcome Upper Confidence Bound (UCB).

    Analytic upper confidence bound that comprises of the posterior mean plus an
    additional term: the posterior standard deviation weighted by a trade-off
    parameter, `beta`. Only supports the case of `q=1` (i.e. greedy, non-batch
    selection of design points). The model must be single-outcome.

    `UCB(x) = mu(x) + sqrt(beta) * sigma(x)`, where `mu` and `sigma` are the
    posterior mean and standard deviation, respectively.

    Example:
        >>> model = SingleTaskGP(train_X, train_Y)
        >>> UCB = UpperConfidenceBound(model, beta=0.2)
        >>> ucb = UCB(test_X)
    """

    def __init__(
        self,
        model: Model,
        beta: Union[float, Tensor],
        objective: Optional[ScalarizedObjective] = None,
        maximize: bool = True,
    ) -> None:
        r"""Single-outcome Upper Confidence Bound.

        Args:
            model: A fitted single-outcome GP model (must be in batch mode if
                candidate sets X will be)
            beta: Either a scalar or a one-dim tensor with `b` elements (batch mode)
                representing the trade-off parameter between mean and covariance
            maximize: If True, consider the problem a maximization problem.
        """
        super().__init__(model=model, objective=objective)
        self.maximize = maximize
        if not torch.is_tensor(beta):
            beta = torch.tensor(beta)
        self.register_buffer("beta", beta)

    @t_batch_mode_transform(expected_q=1)
    def forward(self, X: Tensor) -> Tensor:
        r"""Evaluate the Upper Confidence Bound on the candidate set X.

        Args:
            X: A `(b) x 1 x d`-dim Tensor of `(b)` t-batches of `d`-dim design
                points each.

        Returns:
            A `(b)`-dim Tensor of Upper Confidence Bound values at the given
            design points `X`.
        """
        self.beta = self.beta.to(X)
        posterior = self._get_posterior(X=X)
        batch_shape = X.shape[:-2]
        mean = posterior.mean.view(batch_shape)
        variance = posterior.variance.view(batch_shape)
        delta = (self.beta.expand_as(mean) * variance).sqrt()
        if self.maximize:
            return mean + delta
        else:
            return mean - delta


class ConstrainedExpectedImprovement(AnalyticAcquisitionFunction):
    r"""Constrained Expected Improvement (feasibility-weighted).

    Computes the analytic expected improvement for a Normal posterior
    distribution, weighted by a probability of feasibility. The objective and
    constraints are assumed to be independent and have Gaussian posterior
    distributions. Only supports the case `q=1`. The model should be
    multi-outcome, with the index of the objective and constraints passed to
    the constructor.

    `Constrained_EI(x) = EI(x) * Product_i P(y_i \in [lower_i, upper_i])`,
    where `y_i ~ constraint_i(x)` and `lower_i`, `upper_i` are the lower and
    upper bounds for the i-th constraint, respectively.

    Example:
        >>> # example where 0th output has a non-negativity constraint and
        ... # 1st output is the objective
        >>> model = SingleTaskGP(train_X, train_Y)
        >>> constraints = {0: (0.0, None)}
        >>> cEI = ConstrainedExpectedImprovement(model, 0.2, 1, constraints)
        >>> cei = cEI(test_X)
    """

    def __init__(
        self,
        model: Model,
        best_f: Union[float, Tensor],
        objective_index: int,
        constraints: Dict[int, Tuple[Optional[float], Optional[float]]],
        maximize: bool = True,
    ) -> None:
        r"""Analytic Constrained Expected Improvement.

        Args:
            model: A fitted single-outcome model.
            best_f: Either a scalar or a `b`-dim Tensor (batch mode) representing
                the best function value observed so far (assumed noiseless).
            objective_index: The index of the objective.
            constraints: A dictionary of the form `{i: [lower, upper]}`, where
                `i` is the output index, and `lower` and `upper` are lower and upper
                bounds on that output (resp. interpreted as -Inf / Inf if None)
            maximize: If True, consider the problem a maximization problem.
        """
        super().__init__(model=model)
        self.maximize = maximize
        self.objective_index = objective_index
        self.constraints = constraints
        self.register_buffer("best_f", torch.as_tensor(best_f))
        self._preprocess_constraint_bounds(constraints=constraints)
        self.register_forward_pre_hook(convert_to_target_pre_hook)

    @t_batch_mode_transform(expected_q=1)
    def forward(self, X: Tensor) -> Tensor:
        r"""Evaluate Constrained Expected Improvement on the candidate set X.

        Args:
            X: A `(b) x 1 x d`-dim Tensor of `(b)` t-batches of `d`-dim design
                points each.

        Returns:
            A `(b)`-dim Tensor of Expected Improvement values at the given
            design points `X`.
        """
        posterior = self._get_posterior(X=X, check_single_output=False)
        means = posterior.mean.squeeze(dim=-2)  # (b) x t
        sigmas = posterior.variance.squeeze(dim=-2).sqrt().clamp_min(1e-9)  # (b) x t

        # (b) x 1
        mean_obj = means[..., [self.objective_index]]
        sigma_obj = sigmas[..., [self.objective_index]]
        u = (mean_obj - self.best_f.expand_as(mean_obj)) / sigma_obj
        if not self.maximize:
            u = -u
        normal = Normal(
            torch.zeros(1, device=u.device, dtype=u.dtype),
            torch.ones(1, device=u.device, dtype=u.dtype),
        )
        ei_pdf = torch.exp(normal.log_prob(u))  # (b) x 1
        ei_cdf = normal.cdf(u)
        ei = sigma_obj * (ei_pdf + u * ei_cdf)
        prob_feas = self._compute_prob_feas(X=X, means=means, sigmas=sigmas)
        ei = ei.mul(prob_feas)
        return ei.squeeze(dim=-1)

    def _preprocess_constraint_bounds(
        self, constraints: Dict[int, Tuple[Optional[float], Optional[float]]]
    ) -> None:
        r"""Set up constraint bounds.

        Args:
            constraints: A dictionary of the form `{i: [lower, upper]}`, where
                `i` is the output index, and `lower` and `upper` are lower and upper
                bounds on that output (resp. interpreted as -Inf / Inf if None)
        """
        constraint_lower, self.constraint_lower_inds = [], []
        constraint_upper, self.constraint_upper_inds = [], []
        constraint_both, self.constraint_both_inds = [], []
        constraint_indices = list(constraints.keys())
        if len(constraint_indices) == 0:
            raise ValueError("There must be at least one constraint.")
        if self.objective_index in constraint_indices:
            raise ValueError(
                "Output corresponding to objective should not be a constraint."
            )
        for k in constraint_indices:
            if constraints[k][0] is not None and constraints[k][1] is not None:
                if constraints[k][1] <= constraints[k][0]:
                    raise ValueError("Upper bound is less than the lower bound.")
                self.constraint_both_inds.append(k)
                constraint_both.append([constraints[k][0], constraints[k][1]])
            elif constraints[k][0] is not None:
                self.constraint_lower_inds.append(k)
                constraint_lower.append(constraints[k][0])
            elif constraints[k][1] is not None:
                self.constraint_upper_inds.append(k)
                constraint_upper.append(constraints[k][1])
        self.register_buffer(
            "constraint_both", torch.tensor(constraint_both, dtype=torch.float)
        )
        self.register_buffer(
            "constraint_lower", torch.tensor(constraint_lower, dtype=torch.float)
        )
        self.register_buffer(
            "constraint_upper", torch.tensor(constraint_upper, dtype=torch.float)
        )

    def _compute_prob_feas(self, X: Tensor, means: Tensor, sigmas: Tensor) -> Tensor:
        r"""Compute feasibility probability for each batch of X.

        Args:
            X: A `(b) x 1 x d`-dim Tensor of `(b)` t-batches of `d`-dim design
                points each.
            means: A `(b) x t`-dim Tensor of means.
            sigmas: A `(b) x t`-dim Tensor of standard deviations.
        Returns:
            A `(b) x 1 x 1`-dim tensor of feasibility probabilities

        Note: This function does case-work for upper bound, lower bound, and both-sided
        bounds. Another way to do it would be to use 'inf' and -'inf' for the
        one-sided bounds and use the logic for the both-sided case. But this
        causes an issue with autograd since we get 0 * inf.
        TODO: Investigate further.
        """
        output_shape = list(X.shape)
        output_shape[-1] = 1
        prob_feas = torch.ones(output_shape, device=X.device, dtype=X.dtype)

        if len(self.constraint_lower_inds) > 0:
            normal_lower = _construct_dist(means, sigmas, self.constraint_lower_inds)
            prob_l = 1 - normal_lower.cdf(self.constraint_lower)
            prob_feas = prob_feas.mul(torch.prod(prob_l, dim=-1, keepdim=True))
        if len(self.constraint_upper_inds) > 0:
            normal_upper = _construct_dist(means, sigmas, self.constraint_upper_inds)
            prob_u = normal_upper.cdf(self.constraint_upper)
            prob_feas = prob_feas.mul(torch.prod(prob_u, dim=-1, keepdim=True))
        if len(self.constraint_both_inds) > 0:
            normal_both = _construct_dist(means, sigmas, self.constraint_both_inds)
            prob_u = normal_both.cdf(self.constraint_both[:, 1])
            prob_l = normal_both.cdf(self.constraint_both[:, 0])
            prob_feas = prob_feas.mul(torch.prod(prob_u - prob_l, dim=-1, keepdim=True))
        return prob_feas


class NoisyExpectedImprovement(ExpectedImprovement):
    r"""Single-outcome Noisy Expected Improvement (via fantasies).

    This computes Noisy Expected Improvement by averaging over the Expected
    Improvemnt values of a number of fantasy models. Only supports the case
    `q=1`. Assumes that the posterior distribution of the model is Gaussian.
    The model must be single-outcome.

    `NEI(x) = E(max(y - max Y_baseline), 0)), (y, Y_baseline) ~ f((x, X_baseline))`,
    where `X_baseline` are previously observed points.

    Note: This acquisition function currently relies on using a FixedNoiseGP (required
    for noiseless fantasies).

    Example:
        >>> model = FixedNoiseGP(train_X, train_Y, train_Yvar=train_Yvar)
        >>> NEI = NoisyExpectedImprovement(model, train_X)
        >>> nei = NEI(test_X)
    """

    def __init__(
        self,
        model: GPyTorchModel,
        X_observed: Tensor,
        num_fantasies: int = 20,
        maximize: bool = True,
    ) -> None:
        r"""Single-outcome Noisy Expected Improvement (via fantasies).

        Args:
            model: A fitted single-outcome model.
            X_observed: A `m x d` Tensor of observed points that are likely to
                be the best observed points so far.
            num_fantasies: The number of fantasies to generate. The higher this
                number the more accurate the model (at the expense of model
                complexity and performance).
            maximize: If True, consider the problem a maximization problem.
        """
        if not isinstance(model, FixedNoiseGP):
            raise UnsupportedError(
                "Only FixedNoiseGPs are currently supported for fantasy NEI"
            )
        # sample fantasies
        with torch.no_grad():
            posterior = model.posterior(X=X_observed)
            sampler = SobolQMCNormalSampler(num_fantasies)
            Y_fantasized = sampler(posterior).squeeze(-1)
        batch_X_observed = X_observed.expand(num_fantasies, *X_observed.shape)
        # The fantasy model will operate in batch mode
        fantasy_model = _get_noiseless_fantasy_model(
            model=model, batch_X_observed=batch_X_observed, Y_fantasized=Y_fantasized
        )

        if maximize:
            best_f = Y_fantasized.max(dim=-1)[0]
        else:
            best_f = Y_fantasized.min(dim=-1)[0]

        super().__init__(model=fantasy_model, best_f=best_f, maximize=maximize)

    def forward(self, X: Tensor) -> Tensor:
        r"""Evaluate Expected Improvement on the candidate set X.

        Args:
            X: A `b1 x ... bk x 1 x d`-dim batched tensor of `d`-dim design points.

        Returns:
            A `b1 x ... bk`-dim tensor of Noisy Expected Improvement values at
            the given design points `X`.
        """
        # add batch dimension for broadcasting to fantasy models
        return super().forward(X.unsqueeze(-3)).mean(dim=-1)


def _construct_dist(means: Tensor, sigmas: Tensor, inds: Tensor) -> Normal:
    mean = means[..., inds]
    sigma = sigmas[..., inds]
    return Normal(loc=mean, scale=sigma)


def _get_noiseless_fantasy_model(
    model: FixedNoiseGP, batch_X_observed: Tensor, Y_fantasized: Tensor
) -> FixedNoiseGP:
    r"""Construct a fantasy model from a fitted model and provided fantasies.

    The fantasy model uses the hyperparameters from the original fitted model and
    assumes the fantasies are noiseless.

    Args:
        model: a fitted FixedNoiseGP
        batch_X_observed: A `b x m x d` tensor of inputs where `b` is the number of
            fantasies.
        Y_fantasized: A `b x m` tensor of fantasized targets where `b` is the number of
            fantasies.

    Returns:
        The fantasy model.
    """
    # initialize a copy of FixedNoiseGP on the original training inputs
    # this makes FixedNoiseGP a non-batch GP, so that the same hyperparameters
    # are used across all batches (by default, a GP with batched training data
    # uses independent hyperparameters for each batch).
    fantasy_model = FixedNoiseGP(
        train_X=model.train_inputs[0],
        train_Y=model.train_targets,
        train_Yvar=model.likelihood.noise_covar.noise,
    )
    # update training inputs/targets to be batch mode fantasies
    fantasy_model.set_train_data(
        inputs=batch_X_observed, targets=Y_fantasized, strict=False
    )
    # use noiseless fantasies
    fantasy_model.likelihood.noise_covar.noise = torch.full_like(Y_fantasized, 1e-7)
    # load hyperparameters from original model
    state_dict = deepcopy(model.state_dict())
    fantasy_model.load_state_dict(state_dict)
    return fantasy_model
