import collections

import numpy

import chainer.utils
from chainermn.communicators import _communication_utility
from chainermn.communicators import _memory_utility
from chainermn import nccl


class _MessageType(object):

    def __init__(self, obj):
        if isinstance(obj, numpy.ndarray):
            self.is_tuple = False
            self.narr = 1
            self.ndims = [obj.ndim]
            self.shapes = [obj.shape]
        elif isinstance(obj, collections.Iterable):
            self.is_tuple = True
            self.narr = len(obj)
            self.ndims = [x.ndim for x in obj]
            self.shapes = [x.shape for x in obj]
        else:
            raise ValueError('Message object should be numpy array or tuple.')


class CommunicatorBase(object):

    def __init__(self, mpi_comm):
        self.mpi_comm = mpi_comm

    @property
    def rank(self):
        return self.mpi_comm.rank

    @property
    def size(self):
        return self.mpi_comm.size

    def split(self, color, key):
        """A wrapper function of MPI_Comm_Split.

        This method splits the inter MPI commnicator and return a wrapped
        ChainerMN communicator.

        Args:
            color (int):
                Index of new group. The process with the same color will be
                assigned to the same group.
            key (int):
                Control of rank assignment. The process will be assigned
                a rank in the new group ordered by the value of key.
                If you do not care of the rank, you can just simply specify
                the original rank.

        Returns:
            CommunicatorBase
        """
        return self.__class__(mpi_comm=self.mpi_comm.Split(color, key))

    def send(self, obj, dest, tag):
        """A primitive for inter-process transmitter.

        This method sends numpy-array to target process.
        The target process is expected to invoke ``recv()``.
        This method relies on mpi4py fast communication optimized for
        numpy arrays, which discards any information attached to
        chainer.Variable objects. Please be sure.

        Args:
            obj: data to be sent (tuple, list or raw numpy/cupy array)
            dest (int): Target process specifier.
            tag (int): Message ID (MPI feature).

        """
        chainer.utils.experimental(
            'chainermn.communicators.CommunicatorBase.send')

        msgtype = _MessageType(obj)
        self.mpi_comm.send(msgtype, dest=dest, tag=tag)

        if not msgtype.is_tuple:
            obj = [obj]

        for array in obj:
            if chainer.cuda.get_array_module(array) is not numpy:
                chainer.cuda.Stream.null.synchronize()

            buf = _memory_utility.array_to_buffer_object(array)
            self.mpi_comm.Send(buf, dest=dest, tag=tag)

    def recv(self, source, tag):
        """A primitive of inter-process receiver.

        This method tries to receive numpy-array from target process.
        The target process is expected to invoke ``send()``.
        This method relies on mpi4py fast communication optimized for
        numpy arrays, which discards any information attached to
        chainer.Variable objects. Please be sure.

        Args:
            source (int): Target process specifier.
            tag (int): Message ID (MPI feature).

        """

        chainer.utils.experimental(
            'chainermn.communicators.CommunicatorBase.recv')

        msgtype = self.mpi_comm.recv(source=source, tag=tag)

        if msgtype.is_tuple:
            msg = []
            for shape in msgtype.shapes:
                buf = numpy.empty(numpy.prod(shape), dtype=numpy.float32)
                self.mpi_comm.Recv(buf, source=source, tag=tag)
                msg.append(buf.reshape(shape))
            return tuple(msg)

        else:
            assert len(msgtype.shapes) == 1
            shape = msgtype.shapes[0]
            buf = numpy.empty(numpy.prod(shape), dtype=numpy.float32)
            self.mpi_comm.Recv(buf, source=source, tag=tag)
            return buf.reshape(shape)

    def broadcast_data(self, model):
        raise NotImplementedError()

    def allreduce_grad(self, model):
        raise NotImplementedError()


class NodeAwareCommunicatorBase(CommunicatorBase):

    def __init__(self, mpi_comm, use_nccl):
        super(NodeAwareCommunicatorBase, self).__init__(mpi_comm)

        if use_nccl and not nccl._available:
            raise RuntimeError(
                'NCCL is not available. '
                'Please confirm that NCCL can be found by dynamic linkers, '
                'and ChainerMN is installed without --no-nccl flag.'
            )

        self.use_nccl = use_nccl

        self._init_ranks()

        # We have to delay the initialization of communicators. This is because
        # NCCL's communicators use the current CUDA devices at the time of
        # initialization. Therefore, we have to initialize NCCL communicators
        # after users set the devices to use.
        self.inter_mpi_comm = None
        self.intra_mpi_comm = None
        if self.use_nccl:
            self.intra_nccl_comm = None

    def _init_ranks(self):
        my_ranks = _communication_utility.init_ranks(self.mpi_comm)
        assert my_ranks[0] == self.mpi_comm.rank
        self.intra_rank = my_ranks[1]
        self.intra_size = my_ranks[2]
        self.inter_rank = my_ranks[3]
        self.inter_size = my_ranks[4]

    def _init_comms(self):
        if self.inter_mpi_comm is not None:
            assert self.intra_mpi_comm is not None
            assert not self.use_nccl or self.intra_nccl_comm is not None
            return

        comms = _communication_utility.init_comms(
            self.mpi_comm, self.intra_rank, self.intra_size, self.inter_rank,
            use_nccl=self.use_nccl)
        self.intra_mpi_comm = comms[0]
        self.inter_mpi_comm = comms[1]
        if self.use_nccl:
            self.intra_nccl_comm = comms[2]
