import math
import heapq
import itertools

from dataclasses import dataclass
from functools import cached_property

import numpy as np
from scipy.special import roots_legendre


def cubature(f, a, b, rule, rtol=1e-05, atol=1e-08, max_subdivisions=10000,
             args=(), kwargs=None):
    """
    Adaptive cubature of multidimensional array-valued function.

    Given an arbitrary cubature rule with error estimation, this function returns an
    estimate of the integral over the hypercube defined by the arrays ``a`` and ``b`` to
    the required tolerance, although convergence is not guaranteed for all integrals.

    Parameters
    ----------
    f : callable
        Function to integrate. ``f`` must have the signature::
            f(x : ndarray, *args, **kwargs) -> ndarray
        If ``f`` accepts arrays ``x`` of shape ``(input_dim_1, ..., input_dim_n,
        num_eval_points)`` and outputs arrays of shape ``(output_dim_1, ...,
        output_dim_n, num_eval_points)``, then ``cub`` will return arrays of shape
        ``(output_dim_1, ..., output_dim_n)``.
    a, b : array_like
        Lower and upper limits of integration as rank-1 arrays specifying the left and
        right endpoints of the intervals being integrated over. Infinite limits are
        currently not supported.
    rule : Cubature
        Cubature rule to use when estimating the integral. The cubature rule specified
        must implement ``error_estimate``. See Examples.
    rtol : float, optional
        Relative tolerance. Default is 1e-05.
    atol : float, optional
        Absolute tolerance. Default is 1e-08.
    max_subdivisions : int, optional
        Upper bound on the number of subdivisions to perform to improve the estimate
        over a subregion. Default is 10,000.
    args : tuple, optional
        Additional positional args passed to ``f``, if any.
    kwargs : tuple, optional
        Additional keyword args passed to ``f``, if any.

    Returns
    -------
    res : CubatureResult
        Result of estimation. See CubatureResult.

    Examples
    --------
    A simple 1D integral with vector output:

    >>> import numpy as np
    >>> from scipy.integrate._cubature import *
    >>> def f(x, n):
    ...    # Make sure x and n are broadcastable
    ...    return x.reshape(1, -1)**n
    >>> res = cubature(
    ...     f=f,
    ...     a=np.array([0]),
    ...     b=np.array([1]),
    ...     rule=GaussKronrod(21), # 1D rule
    ...     args=(
    ...         # Since f accepts arrays of shape (ndim, num_eval_points) we need to
    ...         # make sure n is the right shape
    ...         np.arange(10).reshape(-1, 1),
    ...     )
    ... )
    >>> res.estimate
     array([1.        , 0.5       , 0.33333333, 0.25      , 0.2       ,
            0.16666667, 0.14285714, 0.125     , 0.11111111, 0.1       ])


    A 7D integral with arbitrary-shaped array output, function from Genz & Malik 1980.

    >>> import numpy as np
    >>> from scipy.integrate._cubature import *
    >>> def f(x, r, alphas):
    ...     ndim = x.shape[0]
    ...     num_eval_points = x.shape[-1]
    ...     r_reshaped = np.expand_dims(r, -1)
    ...     alphas_reshaped = np.expand_dims(alphas, -1)
    ...     x_reshaped = x.reshape(
    ...         ndim,
    ...         *([1]*(len(alphas.shape) - 1)),
    ...         num_eval_points
    ...     )
    ...     return np.cos(
    ...         2*np.pi*r_reshaped + np.sum(alphas_reshaped * x_reshaped, axis=0)
    ...     )
    >>> res = cubature(
    ...     f=f,
    ...     a=np.array([0, 0, 0, 0, 0, 0, 0]),
    ...     b=np.array([1, 1, 1, 1, 1, 1, 1]),
    ...     rule=GenzMalik(7),
    ...     kwargs={
    ...         "r": np.random.rand(2, 3),
    ...         "alphas": np.random.rand(7, 2, 3),
    ...     }
    ... )
    >>> res.estimate
     array([[ 0.69578554, -0.87958878, -0.90278537],
            [ 0.53436481, -0.34633208, -0.16061907]])
    """

    if max_subdivisions is None:
        max_subdivisions = np.inf

    if kwargs is None:
        kwargs = dict()

    a = np.asarray(a)
    b = np.asarray(b)

    if a.ndim != 1 or b.ndim != 1:
        raise ValueError("a and b should be 1D arrays")

    est = rule.estimate(f, a, b, args, kwargs)

    try:
        err = rule.error_estimate(f, a, b, args, kwargs)
    except NotImplementedError:
        raise ValueError("attempting cubature with a rule that doesn't implement error \
estimation.")

    regions = [CubatureRegion(est, err, a, b)]
    subdivisions = 0
    success = True

    while np.any(err > atol + rtol * np.abs(est)):
        # region_k is the region with highest estimated error
        region_k = heapq.heappop(regions)

        est_k = region_k.estimate
        err_k = region_k.error

        a_k, b_k = region_k.a, region_k.b

        # Subtract the estimate of the integral and its error over this region from the
        # current global estimates, since these will be refined in the loop over all
        # subregions.
        est -= est_k
        err -= err_k

        # Find all 2^ndim subregions formed by splitting region_k along each axis, e.g.
        # for 1D integrals this splits an estimate over an interval into an estimate
        # over two subintervals, for 3D integrals this splits an estimate over a cube
        # into 8 subcubes.
        #
        # For each of the new subregions, calculate an estimate for the integral and
        # the error there, and push these regions onto the heap for potential further
        # subdividing.
        for a_k_sub, b_k_sub in _subregion_coordinates(a_k, b_k):
            est_sub = rule.estimate(f, a_k_sub, b_k_sub, args, kwargs)
            err_sub = rule.error_estimate(f, a_k_sub, b_k_sub, args, kwargs)

            est += est_sub
            err += err_sub

            new_region = CubatureRegion(est_sub, err_sub, a_k_sub, b_k_sub)

            heapq.heappush(regions, new_region)

        subdivisions += 1

        if subdivisions >= max_subdivisions:
            success = False
            break

    status = "converged" if success else "not_converged"

    return CubatureResult(
        estimate=est,
        error=err,
        success=success,
        status=status,
        subdivisions=subdivisions,
        regions=regions,
        atol=atol,
        rtol=rtol,
    )


@dataclass
class CubatureRegion:
    estimate: np.ndarray
    error: np.ndarray
    a: np.ndarray
    b: np.ndarray

    def __lt__(self, other):
        # Consider regions with higher error estimates as being "less than" regions with
        # lower order estimates, so that regions with high error estimates are placed at
        # the top of the heap.
        return _max_norm(self.error) > _max_norm(other.error)


@dataclass
class CubatureResult:
    estimate: np.ndarray
    error: np.ndarray
    success: bool
    status: str
    regions: list[CubatureRegion]
    subdivisions: int
    atol: float
    rtol: float


class Cubature:
    """
    A generic interface for numerical cubature algorithms.

    Finds an estimate for the integral of ``f`` over hypercube described by two arrays
    ``a`` and ``b`` via ``estimate``, and may also find an estimate for the error of
    this approximation via ``error_estimate``.

    If error estimation is not supported by default (as in the case of Gauss-Legendre
    or Newton-Cotes), then ``error_estimate`` will raise a NotImplementedError.
    """

    def estimate(self, f, a, b, args=(), kwargs=None):
        """
        Calculate estimate of integral of ``f`` in hypercube described by corners ``a``
        and ``b``.

        Parameters
        ----------
        f : callable
            Function to integrate. ``f`` must have the signature::
                f(x : ndarray, *args, **kwargs) -> ndarray
            If ``f`` accepts arrays ``x`` of shape ``(input_dim_1, ..., input_dim_n,
            num_eval_points)`` and outputs arrays of shape ``(output_dim_1, ...,
            output_dim_n, num_eval_points)``, then ``cub`` will return arrays of shape
            ``(output_dim_1, ..., output_dim_n)``.
        a, b : ndarray
            Lower and upper limits of integration as rank-1 arrays specifying the left
            and right endpoints of the intervals being integrated over. Infinite limits
            are currently not supported.
        args : tuple, optional
            Additional positional args passed to ``f``, if any.
        kwargs : tuple, optional
            Additional keyword args passed to ``f``, if any.

        Returns
        -------
        est : ndarray
            Result of estimation. If ``f`` returns arrays of shape ``(output_dim_1,
            ..., output_dim_n, eval_points)``, then ``err_est`` will be of shape
            ``(output_dim_1, ..., output_dim_n)``.
        """
        raise NotImplementedError

    def error_estimate(self, f, a, b, args=(), kwargs=None):
        """
        Calculate the error estimate of this cubature rule for the integral of ``f`` in
        the hypercube described by corners ``a`` and ``b``.

        If the cubature rule doesn't support error estimation, this will raise a
        NotImplementedError.

        Parameters
        ----------
        f : callable
            Function to integrate. ``f`` must have the signature::
                f(x : ndarray, *args, **kwargs) -> ndarray
            If ``f`` accepts arrays ``x`` of shape ``(input_dim_1, ..., input_dim_n,
            num_eval_points)`` and outputs arrays of shape ``(output_dim_1, ...,
            output_dim_n, num_eval_points)``, then ``cub`` will return arrays of shape
            ``(output_dim_1, ..., output_dim_n)``.
        a, b : ndarray
            Lower and upper limits of integration as rank-1 arrays specifying the left
            and right endpoints of the intervals being integrated over. Infinite limits
            are currently not supported.
        args : tuple, optional
            Additional positional args passed to ``f``, if any.
        kwargs : tuple, optional
            Additional keyword args passed to ``f``, if any.

        Returns
        -------
        err_est : ndarray
            Estimate of the error. If the cubature rule doesn't support error
           estimation, then a NotImplementedError will be raised instead. If error
           estimation is supported and ``f`` returns arrays of shape ``(output_dim_1,
           ..., output_dim_n, eval_points)``, then ``err_est`` will be of shape
           ``(output_dim_1, ..., output_dim_n)``.
        """
        raise NotImplementedError


class FixedCubature(Cubature):
    """
    A cubature rule implemented as the weighted sum of function evaluations.

    Attributes
    ----------
    TODO: rule attribute

    See Also
    --------
    NewtonCotes, GaussLegendre, ErrorFromDifference
    """

    @property
    def rule(self):
        raise NotImplementedError

    def estimate(self, f, a, b, args=(), kwargs=None):
        """
        Calculate estimate of integral of ``f`` in hypercube described by corners ``a``
        and ``b`` as ``sum(weights * f(nodes))``. Nodes and weights will automatically
        be adjusted from calculating integrals over :math:`[-1, 1]^n` to
        :math:`[a, b]^n`.

        Parameters
        ----------
        f : callable
            Function to integrate. ``f`` must have the signature::
                f(x : ndarray, *args, **kwargs) -> ndarray
            If ``f`` accepts arrays ``x`` of shape ``(input_dim_1, ..., input_dim_n,
            num_eval_points)`` and outputs arrays of shape ``(output_dim_1, ...,
            output_dim_n, num_eval_points)``, then ``cub`` will return arrays of shape
            ``(output_dim_1, ..., output_dim_n)``.
        a, b : ndarray
            Lower and upper limits of integration as rank-1 arrays specifying the left
            and right endpoints of the intervals being integrated over. Infinite limits
            are currently not supported.
        args : tuple, optional
            Additional positional args passed to ``f``, if any.
        kwargs : tuple, optional
            Additional keyword args passed to ``f``, if any.

        Returns
        -------
        est : ndarray
            Result of estimation. If ``f`` returns arrays of shape ``(output_dim_1,
            ..., output_dim_n, eval_points)``, then ``err_est`` will be of shape
            ``(output_dim_1, ..., output_dim_n)``.
        """
        return _apply_rule(f, a, b, self.rule, args, kwargs)


class ErrorFromDifference(FixedCubature):
    """
    A fixed cubature rule with error estimate given by the difference between two
    underlying fixed cubature rules.

    This can be used to give error estimation to cubature rules which don't have default
    error estimation, such as NewtonCotes or GaussLegendre. This is in contrast to rules
    like GaussKronrod which have default error estimation given by a special property of
    that cubature rule. See Examples.

    Attributes
    ----------
    TODO: rule attribute and lower_rule attribute

    See Also
    --------
    GaussKronrod, NewtonCotes

    Examples
    --------
    TODO: examples for ErrorFromDifference
    """

    def __init__(self, higher, lower):
        self.higher = higher
        self.lower = lower

    @property
    def rule(self):
        return self.higher.rule

    @property
    def lower_rule(self):
        return self.lower.rule

    def error_estimate(self, f, a, b, args=(), kwargs=None):
        """
        TODO: docstring for error_estimate in ErrorFromDifference
        """
        nodes, weights = self.rule
        lower_nodes, lower_weights = self.lower_rule

        error_nodes = np.concat([nodes, lower_nodes], axis=-1)
        error_weights = np.concat([weights, -lower_weights], axis=-1)
        error_rule = (error_nodes, error_weights)

        return np.abs(
            _apply_rule(f, a, b, error_rule, args, kwargs)
        )


class Product(ErrorFromDifference):
    """
    Find the n-dimensional cubature rule constructed from the Cartesian product of 1D
    cubature rules.

    Given a list of N 1-dimensional cubature rules which support error estimation using
    ErrorFromDifference, this will find the N-dimensional ErrorFromDifference cubature
    rule obtained by taking the Cartesian product of their nodes, and estimating the
    error by taking the difference with a lower-accuracy N-dimensional cubature rule
    obtained using the `.lower` rule in each of the base 1-dimensional rules.

    Parameters
    ----------
    base_rules : list of ErrorFromDifference
        List of base 1-dimensional ErrorFromDifference cubature rules.

    Attributes
    ----------
    base_rules : list of DualEstimateCubature
        List of base 1-dimensional ErrorFromDifference cubature rules.

    Example
    -------

    Evaluate a 2D integral by taking the product of two 1D rules:

    >>> import numpy as np
    >>> from scipy.integrate._cubature import *
    >>> def f(x):
    ...     # f(x) = cos(x_1) + cos(x_2)
    ...     return np.sum(np.cos(x), axis=0)
    >>> rule = Product([GaussKronrod(15), GaussKronrod(15)]) # Use 15-point GaussKronrod
    >>> a, b = np.array([0, 0]), np.array([1, 1])
    >>> rule.estimate(f, a, b) # True value 2*sin(1), approximately 1.6829
     np.float64(1.682941969615793)
    >>> rule.error_estimate(f, a, b)
     np.float64(2.220446049250313e-16)
    """

    def __init__(self, base_cubatures):
        self.base_cubatures = base_cubatures

    @cached_property
    def rule(self):
        underlying_nodes = [cubature.rule[0] for cubature in self.base_cubatures]

        to_product = []

        for node_group in underlying_nodes:
            if len(node_group.shape) == 1:
                to_product.append(node_group)
            else:
                to_product.extend(node_group)

        nodes = _cartesian_product(
            [cubature.rule[0] for cubature in self.base_cubatures]
        )

        weights = np.prod(
            _cartesian_product(
                [cubature.rule[1] for cubature in self.base_cubatures]
            ),
            axis=0
        )

        return nodes, weights

    @cached_property
    def lower_rule(self):
        nodes = _cartesian_product(
            [cubature.lower_rule[0] for cubature in self.base_cubatures]
        )

        weights = np.prod(
            _cartesian_product(
                [cubature.lower_rule[1] for cubature in self.base_cubatures]
            ),
            axis=0
        )

        return nodes, weights


class NewtonCotes(FixedCubature):
    """
    Newton-Cotes cubature. Newton-Cotes rules consist of function evaluations at equally
    spaced nodes.

    Newton-Cotes has no error estimator. It is possible to give it an error estimator
    by using DualEstimateCubature to estimate the error as the difference between two
    Newton-Cotes rules. See Examples.

    Newton-Cotes is a 1D rule. To use it for multidimensional integrals, it will be
    necessary to take the product of multiple Newton-Cotes rules. See Examples.

    Parameters
    ----------
    npoints : int
        Number of equally spaced evaluation points.

    open : bool, default=False
        Whether this should be an open rule. Open rules do not include the endpoints
        as nodes.

    Attributes
    ----------
    npoints : int
        Number of equally spaced evaluation points.

    open : bool, default=False
        Whether this is an open rule. Open rules do not include the endpoints as nodes.

    Examples
    --------
    Evaluating a simple integral, first without error estimation and then with error
    estimation:

    >>> import numpy as np
    >>> from scipy.integrate._cubature import *
    >>> def f(x):
    ...     return np.sin(np.sqrt(x))
    >>> rule = NewtonCotes(10)
    >>> a, b = np.array([0]), np.array([np.pi * 2])
    >>> rule.estimate(f, a, b) # Very inaccuracte
     array([0.60003617])
    >>> rule.error_estimate(f, a, b) is None
     True
    >>> rule_with_err_est = DualEstimateCubature(NewtonCotes(10), NewtonCotes(8))
    >>> rule_with_err_est.error_estimate(f, a, b) # Error is high
     array([6.21045267])

    Evaluating a 2D integral, using the product of NewtonCotes with an error estimator:

    >>> import numpy as np
    >>> from scipy.integrate._cubature import *
    >>> def f(x):
    ...     # f(x) = cos(x_1) + cos(x_2)
    ...     return np.sum(np.cos(x), axis=0)
    >>> rule_with_err_est = DualEstimateCubature(NewtonCotes(10), NewtonCotes(8))
    >>> rule_2d = Product([rule_with_err_est, rule_with_err_est])
    >>> a, b = np.array([0, 0]), np.array([1, 1])
    >>> rule_2d.estimate(f, a, b) # True value 2*sin(1), approximately 1.6829
     np.float64(1.6829419696151342)
    >>> rule_2d.error_estimate(f, a, b)
     np.float64(6.823492881835591e-10)
    """

    def __init__(self, npoints, open=False):
        if npoints < 2:
            raise ValueError(
                "at least 2 points required for Newton-Cotes cubature"
            )

        self.npoints = npoints
        self.open = open

    @cached_property
    def rule(self):
        if self.open:
            h = 2/self.npoints
            nodes = np.linspace(-1 + h, 1 - h, num=self.npoints).reshape(1, -1)
        else:
            nodes = np.linspace(-1, 1, num=self.npoints).reshape(1, -1)

        weights = _newton_cotes_weights(nodes.reshape(-1))

        return nodes, weights


class GaussLegendre(FixedCubature):
    """
    Gauss-Legendre cubature.

    Gauss-Legendre has no error estimator. It is possible to give it an error estimator
    by using DualEstimateCubature to estimate the error as the difference between two
    Gauss-Legendre rules. See Examples.

    Gauss-Legendre is a 1D rule. To use it for multidimensional integrals, it will be
    necessary to take the product of multiple Gauss-Legendre rules. See Examples.

    Parameters
    ----------
    npoints : int
        Number of nodes.

    Attributes
    ----------
    npoints : int
        Number of nodes

    Examples
    --------
    Evaluating a simple integral, first without error estimation and then with error
    estimation:

    TODO
    """

    def __init__(self, npoints):
        if npoints < 2:
            raise ValueError(
                "At least 2 nodes required for Gauss-Legendre cubature"
            )

        self.npoints = npoints

    @cached_property
    def rule(self):
        nodes, weights = roots_legendre(self.npoints)

        # roots_legendre returns nodes as array of shape (self.npoints,), need to
        # reshape so the spacial dimension is first
        return nodes.reshape(1, -1), weights


class GaussKronrod(ErrorFromDifference):
    """
    Gauss-Kronrod cubature. Gauss-Kronrod rules consist of two cubature rules, one
    higher-order and one lower-order. The higher-order rule is used as the estimate of
    the integral and the difference between them is used as an estimate for the error.

    Gauss-Kronrod is a 1D rule. To use it for multidimensional integrals, it will be
    necessary to take the Product of multiple Gauss-Kronrod rules. See Examples.

    For n-node Gauss-Kronrod, the lower-order rule has ``n//2`` nodes, which are the
    ordinary Gauss-Legendre nodes with corresponding weights. The higher-order rule has
    ``n`` nodes, ``n//2`` of which are the same as the lower-order rule and the
    remaining nodes are the Kronrod extension of those nodes.

    Parameters
    ----------
    npoints : int
        Number of nodes for the higher-order rule.

    Attributes
    ----------
    lower : Cubature
        Lower-order rule.

    References
    ----------
    [1] R. Piessens, E. de Doncker, QUADPACK, files: dqk21.f, dqk15.f (1983).

    Examples
    --------
    Evaluate a 1D integral. Note in this example that ``f`` returns an array, so the
    estimates will also be arrays, despite the fact that this is a 1D problem.

    >>> import numpy as np
    >>> from scipy.integrate._cubature import *
    >>> def f(x):
    ...     return np.cos(x)
    >>> rule = GaussKronrod(21) # Use 21-point GaussKronrod
    >>> a, b = np.array([0]), np.array([1])
    >>> rule.estimate(f, a, b) # True value sin(1), approximately 0.84147
     array([0.84147098])
    >>> rule.error_estimate(f, a, b)
     array([1.11022302e-16])

    Evaluate a 2D integral. Note that in this example ``f`` returns a float, so the
    estimates will also be floats.

    >>> import numpy as np
    >>> from scipy.integrate._cubature import *
    >>> def f(x):
    ...     # f(x) = cos(x_1) + cos(x_2)
    ...     return np.sum(np.cos(x), axis=0)
    >>> rule = Product([GaussKronrod(15), GaussKronrod(15)]) # Use 15-point GaussKronrod
    >>> a, b = np.array([0, 0]), np.array([1, 1])
    >>> rule.estimate(f, a, b) # True value 2*sin(1), approximately 1.6829
     np.float64(1.682941969615793)
    >>> rule.error_estimate(f, a, b)
     np.float64(2.220446049250313e-16)
    """

    def __init__(self, npoints):
        # TODO: nodes and weights are currently hard-coded for values 15 and 21, but in
        # the future it would be best to compute the Kronrod extension of the lower rule
        if npoints != 15 and npoints != 21:
            raise ValueError("Gauss-Kronrod quadrature is currently only supported for \
15 or 21 nodes")

        self.npoints = npoints
        self.lower = GaussLegendre(npoints//2)

    @cached_property
    def rule(self):
        if self.npoints == 21:
            nodes = np.array([[
                0.995657163025808080735527280689003,
                0.973906528517171720077964012084452,
                0.930157491355708226001207180059508,
                0.865063366688984510732096688423493,
                0.780817726586416897063717578345042,
                0.679409568299024406234327365114874,
                0.562757134668604683339000099272694,
                0.433395394129247190799265943165784,
                0.294392862701460198131126603103866,
                0.148874338981631210884826001129720,
                0,
                -0.148874338981631210884826001129720,
                -0.294392862701460198131126603103866,
                -0.433395394129247190799265943165784,
                -0.562757134668604683339000099272694,
                -0.679409568299024406234327365114874,
                -0.780817726586416897063717578345042,
                -0.865063366688984510732096688423493,
                -0.930157491355708226001207180059508,
                -0.973906528517171720077964012084452,
                -0.995657163025808080735527280689003
            ]])

            weights = np.array([
                0.011694638867371874278064396062192,
                0.032558162307964727478818972459390,
                0.054755896574351996031381300244580,
                0.075039674810919952767043140916190,
                0.093125454583697605535065465083366,
                0.109387158802297641899210590325805,
                0.123491976262065851077958109831074,
                0.134709217311473325928054001771707,
                0.142775938577060080797094273138717,
                0.147739104901338491374841515972068,
                0.149445554002916905664936468389821,
                0.147739104901338491374841515972068,
                0.142775938577060080797094273138717,
                0.134709217311473325928054001771707,
                0.123491976262065851077958109831074,
                0.109387158802297641899210590325805,
                0.093125454583697605535065465083366,
                0.075039674810919952767043140916190,
                0.054755896574351996031381300244580,
                0.032558162307964727478818972459390,
                0.011694638867371874278064396062192,
            ])
        elif self.npoints == 15:
            nodes = np.array([[
                0.991455371120812639206854697526329,
                0.949107912342758524526189684047851,
                0.864864423359769072789712788640926,
                0.741531185599394439863864773280788,
                0.586087235467691130294144838258730,
                0.405845151377397166906606412076961,
                0.207784955007898467600689403773245,
                0.000000000000000000000000000000000,
                -0.207784955007898467600689403773245,
                -0.405845151377397166906606412076961,
                -0.586087235467691130294144838258730,
                -0.741531185599394439863864773280788,
                -0.864864423359769072789712788640926,
                -0.949107912342758524526189684047851,
                -0.991455371120812639206854697526329
            ]])

            weights = np.array([
                0.022935322010529224963732008058970,
                0.063092092629978553290700663189204,
                0.104790010322250183839876322541518,
                0.140653259715525918745189590510238,
                0.169004726639267902826583426598550,
                0.190350578064785409913256402421014,
                0.204432940075298892414161999234649,
                0.209482141084727828012999174891714,
                0.204432940075298892414161999234649,
                0.190350578064785409913256402421014,
                0.169004726639267902826583426598550,
                0.140653259715525918745189590510238,
                0.104790010322250183839876322541518,
                0.063092092629978553290700663189204,
                0.022935322010529224963732008058970
            ])

        return nodes, weights


class GenzMalik(ErrorFromDifference):
    """
    Genz-Malik cubature. Genz-Malik cubature is a true cubature rule in that it is not
    constructed as the product of 1D rules.

    Genz-Malik is only defined for integrals of dimension >= 2.

    Parameters
    ----------
    ndim : int
        The spacial dimension of the integrand.

    Attributes
    ----------
    higher : Cubature
        Higher-order rule.

    lower : Cubature
        Lower-order rule.

    References
    ----------
    [1] A.C. Genz, A.A. Malik, Remarks on algorithm 006: An adaptive algorithm for
        numerical integration over an N-dimensional rectangular region, Journal of
        Computational and Applied Mathematics, Volume 6, Issue 4, 1980, Pages 295-302,
        ISSN 0377-0427, https://doi.org/10.1016/0771-050X(80)90039-X.

    Examples
    --------
    Evaluate a 3D integral:

    >>> import numpy as np
    >>> from scipy.integrate._cubature import *
    >>> def f(x):
    ...     # f(x) = cos(x_1) + cos(x_2) + cos(x_3)
    ...     return np.sum(np.cos(x), axis=0)
    >>> rule = GenzMalik(3) # Use 3D Genz-Malik
    >>> a, b = np.array([0, 0, 0]), np.array([1, 1, 1])
    >>> rule.estimate(f, a, b) # True value 3*sin(1), approximately 2.5244
     np.float64(2.5244129547230862)
    >>> rule.error_estimate(f, a, b)
     np.float64(1.378269656626685e-06)
    """

    def __init__(self, ndim, degree=7, lower_degree=5):
        if ndim < 2:
            raise ValueError("Genz-Malik cubature is only defined for ndim >= 2")

        if degree != 7 or lower_degree != 5:
            raise NotImplementedError

        self.ndim = ndim
        self.degree = degree
        self.lower_degree = lower_degree

    @cached_property
    def rule(self):
        # TODO: Currently only support for degree 7 Genz-Malik cubature, should aim to
        # support arbitrary degree
        l_2 = math.sqrt(9/70)
        l_3 = math.sqrt(9/10)
        l_4 = math.sqrt(9/10)
        l_5 = math.sqrt(9/19)

        its = itertools.chain(
            [(0,) * self.ndim],
            _distinct_permutations((l_2,) + (0,) * (self.ndim - 1)),
            _distinct_permutations((-l_2,) + (0,) * (self.ndim - 1)),
            _distinct_permutations((l_3,) + (0,) * (self.ndim - 1)),
            _distinct_permutations((-l_3,) + (0,) * (self.ndim - 1)),
            _distinct_permutations((l_4, l_4) + (0,) * (self.ndim - 2)),
            _distinct_permutations((l_4, -l_4) + (0,) * (self.ndim - 2)),
            _distinct_permutations((-l_4, -l_4) + (0,) * (self.ndim - 2)),
            itertools.product((l_5, -l_5), repeat=self.ndim)
        )

        nodes_size = 1 + 2 * (self.ndim + 1) * self.ndim + 2**self.ndim

        nodes = np.fromiter(
            itertools.chain.from_iterable(zip(*its)),
            dtype=float,
            count=self.ndim * nodes_size
        )

        nodes.shape = (self.ndim, nodes_size)

        w_1 = (2**self.ndim) * (12824 - 9120 * self.ndim + 400 * self.ndim**2) \
            / 19683
        w_2 = (2**self.ndim) * 980/6561
        w_3 = (2**self.ndim) * (1820 - 400 * self.ndim) / 19683
        w_4 = (2**self.ndim) * (200 / 19683)
        w_5 = 6859 / 19683

        weights = np.repeat(
            [w_1, w_2, w_3, w_4, w_5],
            [
                1,
                2 * self.ndim,
                2*self.ndim,
                2*(self.ndim - 1)*self.ndim,
                2**self.ndim,
            ]
        )

        return nodes, weights

    @cached_property
    def lower_rule(self):
        # TODO: Currently only support for the degree 5 lower rule, in the future it
        # would be worth supporting arbitrary degree

        # Nodes are almost the same as the full rule, but there are no nodes
        # corresponding to l_5.
        l_2 = math.sqrt(9/70)
        l_3 = math.sqrt(9/10)
        l_4 = math.sqrt(9/10)

        its = itertools.chain(
            [(0,) * self.ndim],
            _distinct_permutations((l_2,) + (0,) * (self.ndim - 1)),
            _distinct_permutations((-l_2,) + (0,) * (self.ndim - 1)),
            _distinct_permutations((l_3,) + (0,) * (self.ndim - 1)),
            _distinct_permutations((-l_3,) + (0,) * (self.ndim - 1)),
            _distinct_permutations((l_4, l_4) + (0,) * (self.ndim - 2)),
            _distinct_permutations((l_4, -l_4) + (0,) * (self.ndim - 2)),
            _distinct_permutations((-l_4, -l_4) + (0,) * (self.ndim - 2)),
        )

        nodes_size = 1 + 2 * (self.ndim + 1) * self.ndim

        nodes = np.fromiter(
            itertools.chain.from_iterable(zip(*its)),
            dtype=float,
            count=self.ndim * nodes_size
        )

        nodes.shape = (self.ndim, nodes_size)

        # Weights are different from those in the full rule.
        w_1 = (2**self.ndim) * (729 - 950*self.ndim + 50*self.ndim**2) / 729
        w_2 = (2**self.ndim) * (245 / 486)
        w_3 = (2**self.ndim) * (265 - 100*self.ndim) / 1458
        w_4 = (2**self.ndim) * (25 / 729)

        weights = np.repeat(
            [w_1, w_2, w_3, w_4],
            [1, 2 * self.ndim, 2*self.ndim, 2*(self.ndim - 1)*self.ndim]
        )

        return nodes, weights


def _apply_rule(f, a, b, rule, args=(), kwargs=None):
    if kwargs is None:
        kwargs = dict()

    orig_nodes, orig_weights = rule

    rule_ndim = orig_nodes.shape[0]
    a_ndim = len(a)
    b_ndim = len(b)

    if rule_ndim != a_ndim or rule_ndim != b_ndim:
        raise ValueError(f"cubature rule and function are of incompatible dimension, \
nodes have ndim {rule_ndim}, while limit of integration have ndim \
a_ndim={a_ndim}, b_ndim={b_ndim}")

    # Since f accepts arrays of shape (ndim, eval_points), it is necessary to
    # add an extra axis to a and b so that ``f`` can be evaluated there.
    a = a[:, np.newaxis]
    b = b[:, np.newaxis]
    lengths = b - a

    # The underlying cubature rule is for the hypercube [-1, 1]^n.
    #
    # To handle arbitrary regions of integration, it's necessary to apply a linear
    # change of coordinates to map each interval [a[i], b[i]] to [-1, 1].
    nodes = (orig_nodes + 1) * (lengths / 2) + a

    # Also need to multiply the weights by a scale factor equal to the determinant
    # of the Jacobian for this coordinate change.
    weight_scale_factor = math.prod(lengths / 2)
    weights = orig_weights * weight_scale_factor

    # f(nodes) will have shape (output_dim_1, ..., output_dim_n, num_nodes)
    # Summing along the last axis means estimate will shape (output_dim_1, ...,
    # output_dim_n)
    est = np.sum(
        weights * f(nodes, *args, **kwargs),
        axis=-1
    )

    return est


def _cartesian_product(points):
    """
    Takes a list of arrays such as `[ [[x_1, x_2]], [[y_1, y_2]] ]` and
    returns all combinations of these points, such as
    `[[x_1, x_1, x_2, x_2], [y_1, y_2, y_1, y_2]]`

    Note that this is assuming that the spatial dimension is the first axis, as opposed
    to the last axis.
    """
    out = np.stack(np.meshgrid(*points, indexing='ij'), axis=0)
    out = out.reshape(len(points), -1)
    return out


def _subregion_coordinates(a, b):
    """
    Given the coordinates of a region like a=[0, 0] and b=[1, 1], yield the coordinates
    of all subregions, which in this case would be::

        ([0, 0], [1/2, 1/2]),
        ([0, 1/2], [1/2, 1]),
        ([1/2, 0], [1, 1/2]),
        ([1/2, 1/2], [1, 1])
    """

    m = (a + b)/2

    for a_sub, b_sub in zip(
        itertools.product(*np.array([a, m]).T),
        itertools.product(*np.array([m, b]).T)
    ):
        yield np.array(a_sub), np.array(b_sub)


def _max_norm(x):
    return np.max(np.abs(x))


def _distinct_permutations(iterable):
    """
    Find the number of distinct permutations of elements of `iterable`.
    """

    # Algorithm: https://w.wiki/Qai

    items = sorted(iterable)
    size = len(items)

    while True:
        # Yield the permutation we have
        yield tuple(items)

        # Find the largest index i such that A[i] < A[i + 1]
        for i in range(size - 2, -1, -1):
            if items[i] < items[i + 1]:
                break

        #  If no such index exists, this permutation is the last one
        else:
            return

        # Find the largest index j greater than j such that A[i] < A[j]
        for j in range(size - 1, i, -1):
            if items[i] < items[j]:
                break

        # Swap the value of A[i] with that of A[j], then reverse the
        # sequence from A[i + 1] to form the new permutation
        items[i], items[j] = items[j], items[i]
        items[i+1:] = items[:i-size:-1]  # A[i + 1:][::-1]


def _newton_cotes_weights(points):
    order = len(points) - 1

    a = np.vander(points, increasing=True)
    a = np.transpose(a)

    i = np.arange(order + 1)
    b = (1 - np.power(-1, i + 1)) / (2 * (i + 1))

    # TODO: figure out why this 2x is necessary
    return 2*np.linalg.solve(a, b)
