r"""
Post Stack Inversion - 3D with NCCL
===================================
This tutorial is an extension of the :ref:`sphx_glr_tutorials_poststack.py`
tutorial where PyLops-MPI is run in multi-GPU setting with GPUs communicating
via NCCL.
"""

import numpy as np
import cupy as cp
from scipy.signal import filtfilt
from matplotlib import pyplot as plt
from mpi4py import MPI

from pylops.utils.wavelets import ricker
from pylops.basicoperators import Transpose
from pylops.avo.poststack import PoststackLinearModelling

import pylops_mpi

###############################################################################
# NCCL communication can be easily initialized with
# :py:func:`pylops_mpi.utils._nccl.initialize_nccl_comm` operator.
# One can think of this as GPU-counterpart of :code:`MPI.COMM_WORLD`

plt.close("all")
nccl_comm = pylops_mpi.utils._nccl.initialize_nccl_comm()
rank = MPI.COMM_WORLD.Get_rank()

###############################################################################
# Let's start by defining all the parameters required by the
# :py:func:`pylops.avo.poststack.PoststackLinearModelling` operator.
# Note that this section is exactly the same as the one in the MPI example as 
# we will keep using MPI for transfering metadata (i.e., shapes, dims, etc.)

# Model
model = np.load("../testdata/avo/poststack_model.npz")
x, z, m = model['x'][::3], model['z'], np.log(model['model'])[:, ::3]

# Making m a 3D model
ny_i = 20  # size of model in y direction for rank i
y = np.arange(ny_i)
m3d_i = np.tile(m[:, :, np.newaxis], (1, 1, ny_i)).transpose((2, 1, 0))
ny_i, nx, nz = m3d_i.shape

# Size of y at all ranks
ny = MPI.COMM_WORLD.allreduce(ny_i)

# Smooth model
nsmoothy, nsmoothx, nsmoothz = 5, 30, 20
mback3d_i = filtfilt(np.ones(nsmoothy) / float(nsmoothy), 1, m3d_i, axis=0)
mback3d_i = filtfilt(np.ones(nsmoothx) / float(nsmoothx), 1, mback3d_i, axis=1)
mback3d_i = filtfilt(np.ones(nsmoothz) / float(nsmoothz), 1, mback3d_i, axis=2)

# Wavelet
dt = 0.004
t0 = np.arange(nz) * dt
ntwav = 41
wav = ricker(t0[:ntwav // 2 + 1], 15)[0]

# Collecting all the m3d and mback3d at all ranks
m3d = np.concatenate(MPI.COMM_WORLD.allgather(m3d_i))
mback3d = np.concatenate(MPI.COMM_WORLD.allgather(mback3d_i))

###############################################################################
# We are now ready to initialize various :py:class:`pylops_mpi.DistributedArray`
# objects. Compared to the MPI tutorial, we need to make sure that we pass 
# :code:`base_comm_nccl = nccl_comm`, set ``cupy`` as the engine, and fill the
# distributed arrays with CuPy arrays.

m3d_dist = pylops_mpi.DistributedArray(global_shape=ny * nx * nz, 
                                       base_comm_nccl=nccl_comm, 
                                       engine="cupy")
m3d_dist[:] = cp.asarray(m3d_i.flatten())

# Do the same thing for smooth model
mback3d_dist = pylops_mpi.DistributedArray(global_shape=ny * nx * nz, 
                                           base_comm_nccl=nccl_comm, 
                                           engine="cupy")
mback3d_dist[:] = cp.asarray(mback3d_i.flatten())

###############################################################################
# For PostStackLinearModelling, there is no change needed to have it run 
# with NCCL. This PyLops operator has GPU-support 
# (https://pylops.readthedocs.io/en/stable/gpu.html) so it can operate on a 
# distributed arrays with engine set to  CuPy.

PPop = PoststackLinearModelling(wav=cp.asarray(wav), nt0=nz, 
                                spatdims=(ny_i, nx))
Top = Transpose((ny_i, nx, nz), (2, 0, 1))
BDiag = pylops_mpi.basicoperators.MPIBlockDiag(ops=[Top.H @ PPop @ Top, ])

###############################################################################
# This computation will be done on the GPU(s). The call :code:`asarray()` 
# triggers the NCCL communication (gather results from each GPU).
# Note that the array :code:`d` and :code:`d_0` still live in GPU memory.

d_dist = BDiag @ m3d_dist
d_local = d_dist.local_array.reshape((ny_i, nx, nz))
d = d_dist.asarray().reshape((ny, nx, nz))
d_0_dist = BDiag @ mback3d_dist
d_0 = d_dist.asarray().reshape((ny, nx, nz))

###############################################################################
# Inversion using CGLS solver - There is no code change to have run the solver
# with NCCL (this is handledby the MPI operator and DistributedArray)
# In this particular case, the local computation will be done in GPU. 
# Collective communication calls will be carried through NCCL GPU-to-GPU.

# Inversion using CGLS solver
minv3d_iter_dist = pylops_mpi.optimization.basic.cgls(BDiag, d_dist, 
                                                      x0=mback3d_dist, 
                                                      niter=100, show=True)[0]
minv3d_iter = minv3d_iter_dist.asarray().reshape((ny, nx, nz))

###############################################################################

# Regularized inversion with normal equations
epsR = 1e2
LapOp = pylops_mpi.MPILaplacian(dims=(ny, nx, nz), axes=(0, 1, 2), 
                                weights=(1, 1, 1),
                                sampling=(1, 1, 1), 
                                dtype=BDiag.dtype)
NormEqOp = BDiag.H @ BDiag + epsR * LapOp.H @ LapOp
dnorm_dist = BDiag.H @ d_dist
minv3d_ne_dist = pylops_mpi.optimization.basic.cg(NormEqOp, dnorm_dist, 
                                                  x0=mback3d_dist, 
                                                  niter=100, show=True)[0]
minv3d_ne = minv3d_ne_dist.asarray().reshape((ny, nx, nz))

###############################################################################

# Regularized inversion with regularized equations
StackOp = pylops_mpi.MPIStackedVStack([BDiag, np.sqrt(epsR) * LapOp])
d0_dist = pylops_mpi.DistributedArray(global_shape=ny * nx * nz, 
                                      base_comm_nccl=nccl_comm, engine="cupy")
d0_dist[:] = 0.
dstack_dist = pylops_mpi.StackedDistributedArray([d_dist, d0_dist])

dnorm_dist = BDiag.H @ d_dist
minv3d_reg_dist = pylops_mpi.optimization.basic.cgls(StackOp, dstack_dist, 
                                                     x0=mback3d_dist, 
                                                     niter=100, show=True)[0]
minv3d_reg = minv3d_reg_dist.asarray().reshape((ny, nx, nz))

###############################################################################
# Finally we visualize the results. Note that the array must be copied back 
# to the CPU by calling the :code:`get()` method on the CuPy arrays.

if rank == 0:
    # Check the distributed implementation gives the same result
    # as the one running only on rank0
    PPop0 = PoststackLinearModelling(wav, nt0=nz, spatdims=(ny, nx))
    d0 = (PPop0 @ m3d.transpose(2, 0, 1)).transpose(1, 2, 0)
    d0_0 = (PPop0 @ m3d.transpose(2, 0, 1)).transpose(1, 2, 0)

    # Check the two distributed implementations give the same modelling results
    print('Distr == Local', np.allclose(d, d0))
    print('Smooth Distr == Local', np.allclose(d_0, d0_0))

    # Visualize
    fig, axs = plt.subplots(nrows=6, ncols=3, figsize=(9, 14), constrained_layout=True)
    axs[0][0].imshow(m3d[5, :, :].T, cmap="gist_rainbow", vmin=m.min(), vmax=m.max())
    axs[0][0].set_title("Model x-z")
    axs[0][0].axis("tight")
    axs[0][1].imshow(m3d[:, 200, :].T, cmap="gist_rainbow", vmin=m.min(), vmax=m.max())
    axs[0][1].set_title("Model y-z")
    axs[0][1].axis("tight")
    axs[0][2].imshow(m3d[:, :, 220].T, cmap="gist_rainbow", vmin=m.min(), vmax=m.max())
    axs[0][2].set_title("Model y-z")
    axs[0][2].axis("tight")

    axs[1][0].imshow(mback3d[5, :, :].T, cmap="gist_rainbow", vmin=m.min(), vmax=m.max())
    axs[1][0].set_title("Smooth Model x-z")
    axs[1][0].axis("tight")
    axs[1][1].imshow(mback3d[:, 200, :].T, cmap="gist_rainbow", vmin=m.min(), vmax=m.max())
    axs[1][1].set_title("Smooth Model y-z")
    axs[1][1].axis("tight")
    axs[1][2].imshow(mback3d[:, :, 220].T, cmap="gist_rainbow", vmin=m.min(), vmax=m.max())
    axs[1][2].set_title("Smooth Model y-z")
    axs[1][2].axis("tight")

    axs[2][0].imshow(d[5, :, :].T.get(), cmap="gray", vmin=-1, vmax=1)
    axs[2][0].set_title("Data x-z")
    axs[2][0].axis("tight")
    axs[2][1].imshow(d[:, 200, :].T.get(), cmap='gray', vmin=-1, vmax=1)
    axs[2][1].set_title('Data y-z')
    axs[2][1].axis('tight')
    axs[2][2].imshow(d[:, :, 220].T.get(), cmap='gray', vmin=-1, vmax=1)
    axs[2][2].set_title('Data x-y')
    axs[2][2].axis('tight')

    axs[3][0].imshow(minv3d_iter[5, :, :].T.get(), cmap="gist_rainbow", vmin=m.min(), vmax=m.max())
    axs[3][0].set_title("Inverted Model iter x-z")
    axs[3][0].axis("tight")
    axs[3][1].imshow(minv3d_iter[:, 200, :].T.get(), cmap='gist_rainbow', vmin=m.min(), vmax=m.max())
    axs[3][1].set_title('Inverted Model iter y-z')
    axs[3][1].axis('tight')
    axs[3][2].imshow(minv3d_iter[:, :, 220].T.get(), cmap='gist_rainbow', vmin=m.min(), vmax=m.max())
    axs[3][2].set_title('Inverted Model iter x-y')
    axs[3][2].axis('tight')

    axs[4][0].imshow(minv3d_ne[5, :, :].T.get(), cmap="gist_rainbow", vmin=m.min(), vmax=m.max())
    axs[4][0].set_title("Normal Equations Inverted Model iter x-z")
    axs[4][0].axis("tight")
    axs[4][1].imshow(minv3d_ne[:, 200, :].T.get(), cmap='gist_rainbow', vmin=m.min(), vmax=m.max())
    axs[4][1].set_title('Normal Equations Inverted Model iter y-z')
    axs[4][1].axis('tight')
    axs[4][2].imshow(minv3d_ne[:, :, 220].T.get(), cmap='gist_rainbow', vmin=m.min(), vmax=m.max())
    axs[4][2].set_title('Normal Equations Inverted Model iter x-y')
    axs[4][2].axis('tight')

    axs[5][0].imshow(minv3d_reg[5, :, :].T.get(), cmap="gist_rainbow", vmin=m.min(), vmax=m.max())
    axs[5][0].set_title("Regularized Inverted Model iter x-z")
    axs[5][0].axis("tight")
    axs[5][1].imshow(minv3d_reg[:, 200, :].T.get(), cmap='gist_rainbow', vmin=m.min(), vmax=m.max())
    axs[5][1].set_title('Regularized Inverted Model iter y-z')
    axs[5][1].axis('tight')
    axs[5][2].imshow(minv3d_reg[:, :, 220].T.get(), cmap='gist_rainbow', vmin=m.min(), vmax=m.max())
    axs[5][2].set_title('Regularized Inverted Model iter x-y')
    axs[5][2].axis('tight')
