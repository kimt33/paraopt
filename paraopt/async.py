# -*- coding: utf-8 -*-
# Paraopt is a simple parallel optimization toolbox.
# Copyright (C) 2012-2013 Toon Verstraelen <Toon.Verstraelen@UGent.be>
#
# This file is part of Paraopt.
#
# Paraopt is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 3
# of the License, or (at your option) any later version.
#
# Paraopt is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <http://www.gnu.org/licenses/>
#
#--


import numpy as np
import time, bisect

from paraopt.context import context
from paraopt.common import WorkerWrapper


__all__ = [
    'fmin_async',
]


class Population(object):
    def __init__(self, m0, sigma0, npop=None, loss_rate=0.0):
        self.ndof = len(m0)

        # Population size
        if npop is None:
            npop = 8 + 2*self.ndof
        if npop <= self.ndof:
            raise RuntimeError('Population too small.')
        self.npop = npop

        self.m = m0
        self.sigma0 = sigma0
        self.loss_rate = loss_rate
        self.members = []

        self.weights = np.log(self.npop+1) - np.log(np.arange(self.npop)+1)
        #self.weights = np.ones(self.npop)
        assert (self.weights > 0).all()
        self.weights /= self.weights.sum()

    def _get_complete(self):
        return len(self.members) == self.npop

    complete = property(_get_complete)

    def _get_best(self):
        return self.members[0][1]

    best = property(_get_best)

    def sample(self):
        if self.complete:
            xs = np.random.normal(0, 1.0, self.ndof)
            xs *= self.sigmas
            xs = np.dot(self.evecs, xs)
            xs += self.m
            return xs
        else:
            return self.m + np.random.uniform(-self.sigma0, self.sigma0, self.m.shape)

    def add_new(self, f, x, m):
        # Add new member
        bisect.insort(self.members, (f, x, m))

        # Throw one out if needed
        if len(self.members) > self.npop:
            # This is a minimization, i.e. remove largest
            if np.random.uniform(0,1) > self.loss_rate:
                del self.members[-1]
            else:
                del self.members[np.random.randint(self.npop)]

        # Build a new model for the next sample
        if len(self.members) == 1:
            return [0]
        else:
            # determine weights
            ws = self.weights[:len(self.members)]

            # New mean
            if self.complete:
                xs = np.array([x for f, x, m in self.members])
                self.m = np.dot(ws, xs)/ws.sum()

            # New covariance
            ys = np.array([x-m for f, x, m in self.members])
            cm = np.dot(ys.T*ws, ys)/ws.sum()
            if self.complete:
                evals, self.evecs = np.linalg.eigh(cm)
            else:
                if cm.shape == ():
                    evals = np.array([cm])
                else:
                    evals = np.linalg.eigvalsh(cm)

            self.sigmas = np.sqrt(abs(evals))

            return self.sigmas


def fmin_async(fun, x0, sigma0, npop=None, nworker=None, max_iter=100, stol=1e-6, smax=1e6, cnmax=1e6, verbose=False, callback=None, reject_errors=False, loss_rate=0.0):
    '''Minimize a function with an experimental asynchronous CMA variant

       **Arguments:**

       fun
           The function to be minimized. It is recommended to use scoop for
           internal parallelization.

       x0
            The initial guess. (numpy vector, shape=n)

       sigma0
            The initial value of the step size


       **Optional arguments:**

       npop
            The size of the sample population. By default, this is
            8 + 2*ndof.

       max_iter
            The maximum number of iterations

       cnmax
            When the condition number of the covariance goes above this
            threshold, the minimum is considered degenerate and the optimizer
            stops.

       stol
            When the largest sqrt(covariance eigenvalue) drops below this value,
            the solution is sufficiently close to the real optimum and the
            optimization has converged.

       smax
            When the largest sqrt(covariance eigenvalue) exceeds this value,
            the CMA algorithm is terminated due to divergence.

       verbose
            When set to True, some convergence info is printed on screen. When
            set to an integer larger than one, it is interpreted as the interval
            with wich the convergence info must be printed.

       callback
            If given, this routine is called after each update of the covariance
            model. One argument is given, i.e. the covariance model.

       reject_errors
            When set to True, exceptions in fun will be caught and the
            corresponding trials will be rejected. If there are too many
            rejected attempts in one iteration, such that the number of
            successful ones is below cm.nselect, the algorithm will still fail.

       loss_rate
            The probability that a random member from the current population is
            discarded.
    '''
    workers = []
    p = Population(x0, sigma0, npop, loss_rate)

    if nworker is None:
        nworker = p.npop

    counter = 0
    time0 = time.time()
    if verbose:
        print 'Async CMA parameters'
        print '  Number of unknowns:    %10i' % p.ndof
        print '  Population size:       %10i' % p.npop
        print '  Sigma tolerance:       %10.3e' % stol
        print '  Sigma maximum:         %10.3e' % smax
        print '  Condition maximum:     %10.3e' % cnmax
        print '  Loss rate:             %10.3f' % loss_rate

        print 'Iteration       Current          Best         Worst  Pop     max(sigmas)    cn(sigmas)        walltime[s]'
        print '---------------------------------------------------------------------------------------------------------'

    while counter < max_iter:
        # make sure there are enough workers
        # TODO: add optional argument for number of workers
        while len(workers) < nworker:
            if reject_errors:
                worker = context.submit(WorkerWrapper(fun), p.sample())
            else:
                worker = context.submit(fun, p.sample())
            worker.m = p.m.copy()
            workers.append(worker)
        # wait until one is ready
        done, todo = context.wait_first(workers)
        for worker in done:
            x = worker.args[0]
            f = worker.result()
            m = worker.m
            counter += 1
            print_now = (verbose > 0) and (counter % verbose == 0)
            if f == 'FAILED' and print_now:
                print '%9i  FAILED' % counter
            else:
                evals = p.add_new(f, x, m)
                status = None
                if p.complete:
                    if evals[-1] > evals[0]*cnmax:
                        return p, 'FAILED_DEGENERATE'
                    elif evals[-1] > smax:
                        return p, 'FAILED_DIVERGENCE'
                    elif evals[-1] < stol:
                        return p, 'CONVERGED_SIGMA'
                if print_now:
                    print '%9i  %12.5e  %12.5e  %12.5e  %3i    %12.5e  %12.5e   %16.3f' % (
                        counter, f, p.members[0][0], p.members[-1][0], len(p.members),
                        evals[-1], evals[-1]/evals[0], time.time()-time0
                    )
            if callback is not None:
                callback(p)
        workers = list(todo)

    return p, 'FAILED_MAX_ITER'
