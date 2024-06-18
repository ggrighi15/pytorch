# mypy: allow-untyped-defs
""" Triton Implementation of the flex_attention Kernel"""

import logging
from enum import auto, Enum
from typing import Any, List, Tuple

import torch
from .. import config
from ..ir import (
    ComputedBuffer,
    FixedLayout,
    FlexibleLayout,
    InputBuffer,
    IRNode,
    StorageBox,
    Subgraph,
    TensorBox,
)
from ..lowering import empty_strided, full, lowerings, register_lowering
from ..select_algorithm import autotune_select_algorithm, TritonTemplate

log = logging.getLogger(__name__)
aten = torch.ops.aten


class SubgraphType(Enum):
    """The type of subgraph for which we want to generate an output buffer."""

    FWD = auto()  # Forward pass
    JOINT_FWD = auto()  # The recompute step fo the of the bwds kernel
    JOINT_BWD = auto()  # The bwd pass of the joint


def flex_attention_grid(batch_size, num_heads, num_queries, d_model, meta):
    """How is this kernel parallelized?
    We create a grid of (batch_size * num_heads, ceil_div(n_queries, query_block_size), 1)
    Each block is responsible for iterating over blocks of keys and values calculating
    the final attention output.
    """
    import triton

    return (triton.cdiv(num_queries, meta["BLOCK_M"]), batch_size * num_heads, 1)


def create_placeholder(
    name: str, dtype: torch.dtype, device: torch.device
) -> TensorBox:
    """Creates a placeholder input buffers for producing subgraph_output."""
    input_buffer = InputBuffer(name, FixedLayout(device, dtype, [1], [1]))
    return TensorBox.create(input_buffer)


def index_to_other_buffers(cnt: int, graph_type: SubgraphType) -> int:
    """This function needs to be aware of the signatures for flex_attention_forward
    and flex_attention_backward. If new args are added, or the signature changes
    be sure to update the indexing math

    Args:
        cnt (int): The current index of the placeholder node
        is_joint_graph (bool): Whether or not this subgraph represents the joint graph
    """
    # Current fwd_args = [query, key, value, score_mod, *other_buffers]
    # For fwd_graphs we have 5 dummy values this when the first lifted args
    # is seen cnt = 5 and the start of the index_buffers is at args[4]
    # thus we subtract 1 from the current cnt
    if graph_type == SubgraphType.FWD:
        return cnt - 1

    # Current bwd_args = [q, k, v, out, lse, grad_out, fw_graph, joint_graph, *other_buffers]
    # We have 5 dummy values but the start of other_buffers is at index 8
    if graph_type == SubgraphType.JOINT_FWD:
        return cnt + 3

    # Same bwd args but now with 6 dummy values while other_buffers still start at 8
    if graph_type == SubgraphType.JOINT_BWD:
        return cnt + 2


def build_subgraph_buffer(
    args: Tuple[IRNode],
    placeholder_inps: List[TensorBox],
    subgraph: Subgraph,
    graph_type: SubgraphType,
) -> ComputedBuffer:
    """This function's goal is to take in the required args and produce the subgraph buffer
    The subgraph buffer is a ComputedBuffer that will be inlined into the triton template

    Args:
        args: The args that were passed into the flex_attention kernel
        placeholder_inps: The list of scalar inputs, these were created on the fly through `create_placeholder`
        subgraph: The Subgraph ir for which to produce the output node
        graph_type: The type of subgraph for which we want to produce the output node, see enum above for details
    """
    cnt = 0
    env = {}
    for node in subgraph.graph_module.graph.nodes:
        # There are two classes of placeholder inpts that we need
        # to handle differently. For the first n_scalar_inps inputs
        # we expect that these placeholders were generated by the make_fx call
        # in the flex Attention HOP. So we need to create a new placeholder
        # TensorBox for each of these inputs. For the rest of the inputs we
        # expect that these are lifted inputs that fill up the '*other_buffers'
        # tuple and already have corresponding TensorBoxes passed in as args.
        if node.op == "placeholder":
            is_lifted_input = cnt >= len(placeholder_inps)
            lifted_input_index = index_to_other_buffers(cnt, graph_type)
            env[node] = (
                args[lifted_input_index] if is_lifted_input else placeholder_inps[cnt]
            )
            cnt += 1
        elif node.op == "call_function":
            # For call_function we use the default lowerings and pass in the
            # already created TensorBoxes as args
            from torch.utils._pytree import tree_map

            args, kwargs = tree_map(
                lambda x: env[x] if x in env else x, (node.args, node.kwargs)
            )
            env[node] = lowerings[node.target](*args, **kwargs)
        elif node.op == "output":
            # For the output node we need to create a ComputedBuffer
            # which represents the actual score modification
            # The joint_graph's output should be of the form[grad_score, None, None, None, None]
            # This is because only the 'score' requires grad and the other outputs are
            # the non-differentiable index scalars
            if graph_type == SubgraphType.FWD or graph_type == SubgraphType.JOINT_FWD:
                output_node = node.args[0]
            else:
                output_node = node.args[0][0]
            output_buffer = env[output_node]
            assert isinstance(output_buffer, TensorBox), (
                "The output node  for flex attention's subgraph must be a TensorBox, but got: ",
                type(output_buffer),
            )
            assert isinstance(output_buffer.data, StorageBox), (
                "The output node for the flex attention subgraph must be a StorageBox, but got: ",
                type(output_buffer),
            )
            # Create the ComputedBuffer directly that will be inlined into the modification block
            subgraph_buffer = ComputedBuffer(
                name=None,
                layout=FlexibleLayout(
                    device=output_buffer.data.get_device(),
                    dtype=output_buffer.data.get_dtype(),
                    size=output_buffer.data.get_size(),
                ),
                data=output_buffer.data.data,  # type: ignore[arg-type]
            )
            return subgraph_buffer

    raise ValueError("TemplatedAttention was passed a subgraph with no output node!")


flex_attention_template = TritonTemplate(
    name="flex_attention",
    grid=flex_attention_grid,
    source=r"""
{{def_kernel("Q", "K", "V", "LSE")}}
    # Sub notation for this kernel:
    # Q: Query, K: Key, V: Value
    # M: Number of queries, N: Number of keys/values, D: Model dimension
    # z: Batch size, h: Number of heads, m: Number of queries per head, k: Number of keys per head
    # (Modifiable) Config options:
    # BLOCK_M
    # BLOCK_N
    # SCORE_MOD_IS_LINEAR: Is the score modifier linear? If so, we can lift the
    # change of base out of the loop
    # ROWS_GUARANTEED_SAFE: Is it guaranteed that at least one value in each row
    # is not masked out? If so, we can skip an extra safety check
    # OUTPUT_LOGSUMEXP: We only need to store the logsumexp if we require grad

    # Define Q Strides
    stride_qz = {{stride("Q", 0)}}
    stride_qh = {{stride("Q", 1)}}
    stride_qm = {{stride("Q", 2)}}
    stride_qk = {{stride("Q", 3)}}
    # Define K Strides
    stride_kz = {{stride("K", 0)}}
    stride_kh = {{stride("K", 1)}}
    stride_kn = {{stride("K", 2)}}
    stride_kk = {{stride("K", 3)}}
    # Define V Strides
    stride_vz = {{stride("V", 0)}}
    stride_vh = {{stride("V", 1)}}
    stride_vk = {{stride("V", 2)}}
    stride_vn = {{stride("V", 3)}}

    Z = {{size("Q", 0)}}
    H = {{size("Q", 1)}}
    Q_LEN = {{size("Q", 2)}}
    KV_LEN = {{size("K", 2)}}

    qk_scale = 1.0
    MATMUL_PRECISION = Q.dtype.element_ty

    start_m = tl.program_id(0)
    off_hz = tl.program_id(1)

    q_offset = off_hz * stride_qh
    kv_offset = off_hz * stride_kh
    Q_block_ptr = tl.make_block_ptr(
        base=Q + q_offset,
        shape=(Q_LEN, BLOCK_DMODEL),
        strides=(stride_qm, stride_qk),
        offsets=(start_m * BLOCK_M, 0),
        block_shape=(BLOCK_M, BLOCK_DMODEL),
        order=(1, 0)
    )
    K_block_ptr = tl.make_block_ptr(
        base=K + kv_offset,
        shape=(BLOCK_DMODEL, KV_LEN),
        strides=(stride_kk, stride_kn),
        offsets=(0, 0),
        block_shape=(BLOCK_DMODEL, BLOCK_N),
        order=(0, 1)
    )
    V_block_ptr = tl.make_block_ptr(
        base=V + kv_offset,
        shape=(KV_LEN, BLOCK_DMODEL),
        strides=(stride_vk, stride_vn),
        offsets=(0, 0),
        block_shape=(BLOCK_N, BLOCK_DMODEL),
        order=(1, 0)
    )
    # initialize offsets
    offs_m = start_m * BLOCK_M + tl.arange(0, BLOCK_M)
    offs_n = tl.arange(0, BLOCK_N)
    # initialize pointer to m and l
    m_i = tl.zeros([BLOCK_M], dtype=tl.float32) - float("inf")
    l_i = tl.zeros([BLOCK_M], dtype=tl.float32)
    acc = tl.zeros([BLOCK_M, BLOCK_DMODEL], dtype=tl.float32)

    q = tl.load(Q_block_ptr)
    if SCORE_MOD_IS_LINEAR:
        qk_scale *= 1.44269504
    q = (q * qk_scale).to(MATMUL_PRECISION)
    # loop over k, v and update accumulator
    lo = 0
    hi = KV_LEN
    for start_n in range(lo, hi, BLOCK_N):
        start_n = tl.multiple_of(start_n, BLOCK_N)
        # -- load k, v --
        k = tl.load(K_block_ptr)
        # -- compute qk ---
        qk = tl.dot(q, k)
        # ~~~~~~~~~~~~~~~~~~~ Apply score modification  ~~~~~~~~~~~~~~~~~~~
        m = offs_m[:, None]
        n = start_n + offs_n[None, :]
        {{ modification(
            subgraph_number=0,
            output_name="post_mod_scores",
            score="qk",
            b="off_hz // H",
            h="off_hz % H",
            m="m",
            n="n",
            out="qk"
        ) | indent_except_first(2) }}
        # TODO: In the case that score_mod is linear, this can be LICMed
        if not SCORE_MOD_IS_LINEAR:
            post_mod_scores *= 1.44269504
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

        # -- compute scaling constant ---
        m_ij = tl.maximum(m_i, tl.max(post_mod_scores, 1))

        alpha = tl.math.exp2(m_i - m_ij)
        p = tl.math.exp2(post_mod_scores - m_ij[:, None])
        if not ROWS_GUARANTEED_SAFE:
            masked_out_rows = (m_ij == float("-inf"))
            alpha = tl.where(masked_out_rows, 0, alpha)
            p = tl.where(masked_out_rows[:, None], 0, p)

        # NB: l_i update is pulled up here since it's a bit faster
        # NB: For headdim=256, it's faster to move it back down to after m_i =
        # m_ij
        l_i = l_i * alpha + tl.sum(p, 1)
        # # -- scale and update acc --
        acc = acc * alpha[:, None]
        v = tl.load(V_block_ptr)
        acc = tl.dot(p.to(MATMUL_PRECISION), v, acc)

        # -- update m_i
        m_i = m_ij
        # update pointers
        K_block_ptr = tl.advance(K_block_ptr, (0, BLOCK_N))
        V_block_ptr = tl.advance(V_block_ptr, (BLOCK_N, 0))

    # Store output and logsumexp
    acc = acc / l_i[:, None]
    idx_z = tl.program_id(1) // H
    idx_h = tl.program_id(1) % H
    idx_m = offs_m[:, None]
    idx_d = tl.arange(0, BLOCK_DMODEL)[None, :]

    mask = idx_m < Q_LEN
    # TODO generalize and add proper mask support
    {{store_output(("idx_z", "idx_h", "idx_m", "idx_d"), "acc", "mask")}}

    # TODO dont want to write this if we dont require grad
    if OUTPUT_LOGSUMEXP:
        l_ptrs = LSE + off_hz * Q_LEN + offs_m
        lse = m_i + tl.math.log2(l_i)
        tl.store(l_ptrs, lse)
 """,
)


_h100_default_config = {
    (torch.float32, 64): (128, 32, 4, 3),
    (torch.float32, 128): (32, 64, 4, 3),
    (torch.float32, 256): (32, 32, 4, 3),
    (torch.bfloat16, 64): (128, 64, 4, 3),
    (torch.bfloat16, 128): (64, 32, 4, 3),
    (torch.bfloat16, 256): (64, 32, 4, 3),
}

_a100_default_config = {
    (torch.float32, 64): (128, 32, 4, 3),
    (torch.float32, 128): (128, 32, 4, 3),
    (torch.float32, 256): (64, 16, 4, 3),
    (torch.bfloat16, 64): (128, 64, 4, 3),
    (torch.bfloat16, 128): (128, 32, 4, 3),
    (torch.bfloat16, 256): (32, 64, 4, 3),
}


def _get_default_config_fwd(query) -> Tuple[int, int, int, int]:
    dtype = query.get_dtype()
    head_dim = query.get_size()[-1]
    default_config = None

    if head_dim <= 256 and torch.cuda.get_device_capability() >= (9, 0):  # H100
        if dtype == torch.float32:
            default_config = (64, 64, 4, 3)
        else:
            default_config = (128, 64, 4, 3)
        default_config = _h100_default_config.get((dtype, head_dim), default_config)
    elif head_dim <= 256 and torch.cuda.get_device_capability() >= (8, 0):  # A100
        if dtype == torch.float32:
            default_config = (64, 64, 4, 3)
        else:
            default_config = (128, 64, 4, 3)
        default_config = _a100_default_config.get((dtype, head_dim), default_config)
    else:  # modest hardware or extremely large head_dim
        if dtype == torch.float32:
            default_config = (32, 16, 4, 3)
        else:
            default_config = (64, 32, 4, 3)

    return default_config


def _get_default_config_bwd(query) -> Tuple[int, int, int, int]:
    head_dim = query.get_size()[-1]
    dtype = query.get_dtype()

    if head_dim <= 256 and torch.cuda.get_device_capability() >= (9, 0):  # H100
        if dtype == torch.float32:
            return (64, 64, 4, 1)
        return (128, 128, 4, 3)
    elif head_dim <= 256 and torch.cuda.get_device_capability() >= (8, 0):  # A100
        return (64, 64, 4, 1)
    else:  # modest hardware or extremely large head_dim
        return (16, 16, 4, 1)


# TODO: We probably also need a layout constraint?
@register_lowering(torch.ops.higher_order.flex_attention, type_promotion_kind=None)
def flex_attention(*args, **kwargs):
    query, key, value, subgraph, *other_buffers = args
    for buf in [query, key, value]:
        buf.realize()
    placeholder_inps = [
        create_placeholder(name, dtype, query.get_device())
        for name, dtype in [
            ("score", query.get_dtype()),
            ("b", torch.int32),
            ("h", torch.int32),
            ("m", torch.int32),
            ("n", torch.int32),
        ]
    ]
    subgraph_buffer = build_subgraph_buffer(
        args, placeholder_inps, subgraph, graph_type=SubgraphType.FWD
    )
    layout = FixedLayout(
        query.get_device(),
        query.get_dtype(),
        query.get_size(),
        FlexibleLayout.contiguous_strides(query.get_size()),
    )
    # see NOTE:[TritonTemplates with multiple outputs]
    logsumexp_shape = query.get_size()[:-1]  # [B, H, M]
    logsumexp = empty_strided(
        logsumexp_shape,
        None,
        dtype=torch.float32,  # The logsumexp is always stored in fp32 regardless of the input dtype
        device=query.get_device(),
    )
    choices: List[Any] = []
    configs: List[Tuple[int, int, int, int]] = []
    configs.append(_get_default_config_fwd(query))
    if config.max_autotune:
        configs += [
            (128, 64, 4, 3),
            (128, 128, 4, 3),
            (128, 128, 8, 2),
            (64, 128, 4, 3),
            (64, 64, 4, 3),
        ]

    # Note, we don't need to pass in the captured buffers explicitly
    # because they're implicitly added by the score_mod function
    # We do need to explicitly pass it in for autotuning though.
    for BLOCK_M, BLOCK_N, num_warps, num_stages in configs:
        flex_attention_template.maybe_append_choice(
            choices=choices,
            input_nodes=[query, key, value, logsumexp],
            layout=layout,
            subgraphs=[
                subgraph_buffer,
            ],
            mutated_inputs=[
                logsumexp,
            ],
            num_stages=num_stages,
            num_warps=num_warps,
            call_sizes=query.get_size(),
            BLOCK_M=BLOCK_M,
            BLOCK_N=BLOCK_N,
            BLOCK_DMODEL=query.get_size()[-1],
            # For now, we always assume the "sound" option
            SCORE_MOD_IS_LINEAR=True,
            ROWS_GUARANTEED_SAFE=True,
            OUTPUT_LOGSUMEXP=True,
        )
    inputs_for_autotuning = [query, key, value, logsumexp] + list(other_buffers)
    return (
        autotune_select_algorithm(
            "flex_attention", choices, inputs_for_autotuning, layout
        ),
        logsumexp,
    )


# ---------------------------- Backward HOP Implementation ----------------------------


def flex_attention_backward_grid(
    batch_size, num_heads, num_queries, d_model, num_key_value, meta
):
    """How is this kernel parallelized?
    Currently this is only parallelizing over batch * num_heads, but we can, and want to
    parallelize over ceil_div(num_key_value, key_value_block_size). To do this will either require
    atomic updates to some grad values or to have a two pass kernel design.
    """
    import triton

    return (
        triton.cdiv(num_queries, meta["BLOCK_M2"])
        + triton.cdiv(num_key_value, meta["BLOCK_N1"]),
        1,
        batch_size * num_heads,
    )


flex_attention_backward_template = TritonTemplate(
    name="flex_attention_backward",
    grid=flex_attention_backward_grid,
    source=r"""
{{def_kernel("Q", "K", "V", "OUT", "LSE", "DELTA", "DO", "DQ", "DV")}}
    # Sub notation for this kernel:
    # Q: Query, K: Key, V: Value
    # OUT: Forward output, LSE: logsumexp (logsumexp is always stored in fp32 regardless of the input dtype)
    # DELTA: Precomputed sum(OUT* DO, axis=1)
    # DO: Derivative of Output, DQ: Derivative of Query, DV: Derivative of Value
    # DK: Derivative of Key, is the written to via the store_output call due to some limitations with
    # inductor codegen
    # M: Number of queries, N: Number of keys/values, D: Model dimension
    # z: Batch size, h: Number of heads, m: Number of queries or keys/values, d: Head dim
    # (Modifiable) Config options:
    # BLOCK_M1: when calculating DK & DV, iterate over BLOCK_M1 across the seqlen dim of Q in each thread block.
    # BLOCK_N1: when calculating DK & DV, the thread block size across the seqlen dim of K/V.
    # BLOCK_M2: when calculating DQ, the thread block size across the seqlen dim of Q.
    # BLOCK_N2: when calculating DQ, iterate over BLOCK_N2 across the seqlen dim of K/V in each thread block.
    # SCORE_MOD_IS_LINEAR: Is the score modifier linear? If so, we can lift the
    # change of base out of the loop

    # Define Q Strides
    stride_qz = {{stride("Q", 0)}}
    stride_qh = {{stride("Q", 1)}}
    stride_qm = {{stride("Q", 2)}}
    stride_qd = {{stride("Q", 3)}}
    # Define K Strides
    stride_kz = {{stride("K", 0)}}
    stride_kh = {{stride("K", 1)}}
    stride_km = {{stride("K", 2)}}
    stride_kd = {{stride("K", 3)}}
    # Define V Strides
    stride_vz = {{stride("V", 0)}}
    stride_vh = {{stride("V", 1)}}
    stride_vm = {{stride("V", 2)}}
    stride_vd = {{stride("V", 3)}}

    Z = {{size("Q", 0)}}
    H = {{size("Q", 1)}}
    Q_LEN = {{size("Q", 2)}}
    KV_LEN = {{size("K", 2)}}

    MATMUL_PRECISION = Q.dtype.element_ty

    pid = tl.program_id(0)
    NUM_KV_BLOCKS = KV_LEN // BLOCK_N1

    off_hz = tl.program_id(2)
    off_z = off_hz // H # batch idx
    off_h = off_hz % H # head idx

    off_chz = (off_hz * Q_LEN).to(tl.int64)
    q_adj = (stride_qh * (off_hz % H) + stride_qz * (off_hz // H)).to(tl.int64)
    k_adj = (stride_kh * (off_hz % H) + stride_kz * (off_hz // H)).to(tl.int64)
    v_adj = (stride_vh * (off_hz % H) + stride_vz * (off_hz // H)).to(tl.int64)

    # offset pointers for batch/head
    Q += q_adj
    K += k_adj
    V += v_adj
    DO += q_adj
    DQ += q_adj
    DV += v_adj
    LSE += off_chz
    DELTA += off_chz

    offs_k = tl.arange(0, BLOCK_DMODEL)

    if pid >= NUM_KV_BLOCKS:
        # THIS BLOCK DOES DQ
        off_pid = pid - NUM_KV_BLOCKS
        start_m2 = off_pid * BLOCK_M2

        offs_m2 = start_m2 + tl.arange(0, BLOCK_M2)

        q = tl.load(Q + offs_m2[:, None] * stride_qm + offs_k[None, :] * stride_qd)
        dq = tl.zeros([BLOCK_M2, BLOCK_DMODEL], dtype=tl.float32)
        do = tl.load(DO + offs_m2[:, None] * stride_qm + offs_k[None, :] * stride_qd)

        lse = tl.load(LSE + offs_m2)
        lse = lse[:, None]

        start_n2 = 0
        offs_m2 = start_m2 + tl.arange(0, BLOCK_M2)
        offs_n2 = start_n2 + tl.arange(0, BLOCK_N2)
        kT_ptrs = K + offs_n2[None, :] * stride_km + offs_k[:, None] * stride_kd
        vT_ptrs = V + offs_n2[None, :] * stride_vm + offs_k[:, None] * stride_vd
        Di = tl.load(DELTA + offs_m2)
        # BLOCK_M2 must be a multiple of BLOCK_N2, otherwise the code wouldn't work.
        tl.static_assert(BLOCK_M2 % BLOCK_N2 == 0)

        curr_n = start_n2
        num_steps = KV_LEN // BLOCK_N2
        for blk_idx in range(num_steps):
            offs_n2= curr_n + tl.arange(0, BLOCK_N2)
            kT = tl.load(kT_ptrs)
            vT = tl.load(vT_ptrs)
            qk = tl.dot(q, kT)
            # ~~~~~~~~~~~~~~~~~~~ Apply score modification  ~~~~~~~~~~~~~~~~~~~
            pre_mod_scores = qk
            m = offs_m2[:, None]
            n = offs_n2[None, :]
            {{ modification(
                subgraph_number=0,
                output_name="post_mod_scores",
                score="qk",
                b="off_z",
                h="off_h",
                m="m",
                n="n",
                out="qk"
            ) | indent_except_first(3) }}
            # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            if not SCORE_MOD_IS_LINEAR:
                post_mod_scores *= 1.44269504
            p = tl.math.exp2(post_mod_scores - lse).to(MATMUL_PRECISION)
            # Compute dP and dS.
            dp = tl.dot(do, vT)
            ds = p * (dp - Di[:, None])
            # ~~~~~~~~~~~~~~~~~~~ Apply joint modification  ~~~~~~~~~~~~~~~~~~~
            {{ modification(
                subgraph_number=1,
                output_name = "grad_scores",
                score="pre_mod_scores",
                b="off_z",
                h="off_h",
                m="m",
                n="n",
                grad_score_mod="ds"
            ) | indent_except_first(3) }}
            ds = grad_scores
            # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            ds = ds.to(MATMUL_PRECISION)
            # Compute dQ.
            dq += tl.dot(ds, tl.trans(kT))
            # Increment pointers.
            curr_n += BLOCK_N2
            kT_ptrs += BLOCK_N2 * stride_km
            vT_ptrs += BLOCK_N2 * stride_km
        # Write back dQ.
        dq_ptrs = DQ + offs_m2[:, None] * stride_qm + offs_k[None, :] * stride_qd
        tl.store(dq_ptrs, dq)
    else:
        # THIS BLOCK DOES DK & DV
        start_n1 = pid * BLOCK_N1
        start_m1 = 0

        offs_n1 = start_n1 + tl.arange(0, BLOCK_N1)

        dv = tl.zeros([BLOCK_N1, BLOCK_DMODEL], dtype=tl.float32)
        dk = tl.zeros([BLOCK_N1, BLOCK_DMODEL], dtype=tl.float32)

        # load K and V: they stay in SRAM throughout the inner loop.
        k = tl.load(K + offs_n1[:, None] * stride_km + offs_k[None, :] * stride_kd)
        v = tl.load(V + offs_n1[:, None] * stride_vm + offs_k[None, :] * stride_vd)

        offs_m1 = start_m1 + tl.arange(0, BLOCK_M1)
        offs_n1 = start_n1 + tl.arange(0, BLOCK_N1)
        qT_ptrs = Q + offs_m1[None, :] * stride_qm + offs_k[:, None] * stride_qd
        do_ptrs = DO + offs_m1[:, None] * stride_qm + offs_k[None, :] * stride_qd
        # BLOCK_N1 must be a multiple of BLOCK_M1, otherwise the code wouldn't work.
        tl.static_assert(BLOCK_N1 % BLOCK_M1 == 0)

        curr_m = start_m1
        num_steps = Q_LEN // BLOCK_M1
        for blk_idx in range(num_steps):
            qT = tl.load(qT_ptrs)
            # Load LSE before computing qk to reduce pipeline stall.
            offs_m1 = curr_m + tl.arange(0, BLOCK_M1)
            lse = tl.load(LSE + offs_m1)
            qkT = tl.dot(k, qT)
            # ~~~~~~~~~~~~~~~~~~~ Apply score modification  ~~~~~~~~~~~~~~~~~~~
            m = offs_m1[None, :]
            n = offs_n1[:, None]
            pre_mod_scores = qkT
            {{ modification(
                subgraph_number=0,
                output_name="post_mod_scores",
                score="qkT",
                b="off_z",
                h="off_h",
                m="m",
                n="n",
                out="qkT"
            ) | indent_except_first(3) }}
            # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            if not SCORE_MOD_IS_LINEAR:
                post_mod_scores *= 1.44269504
            pT = tl.math.exp2(post_mod_scores - lse[None, :])
            do = tl.load(do_ptrs)
            # Compute dV.
            ppT = pT
            dv += tl.dot(ppT.to(MATMUL_PRECISION), do)
            Di = tl.load(DELTA + offs_m1)
            # Compute dP and dS.
            dpT = tl.dot(v, tl.trans(do))
            dsT = pT * (dpT - Di[None, :])
            # ~~~~~~~~~~~~~~~~~~~ Apply joint modification  ~~~~~~~~~~~~~~~~~~~
            m = offs_m1[None, :]
            n = offs_n1[:, None]
            {{ modification(
                subgraph_number=1,
                output_name = "grad_scores",
                score="pre_mod_scores",
                b="off_z",
                h="off_h",
                m="m",
                n="n",
                grad_score_mod="dsT"
            ) | indent_except_first(3) }}
            dsT = grad_scores
            # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            dk += tl.dot(dsT.to(MATMUL_PRECISION), tl.trans(qT))
            # Increment pointers.
            curr_m += BLOCK_M1
            qT_ptrs += BLOCK_M1 * stride_qm
            do_ptrs += BLOCK_M1 * stride_qm

        dv_ptrs = DV + offs_n1[:, None] * stride_vm + offs_k[None, :] * stride_vd
        tl.store(dv_ptrs, dv)

        # Write back dK.
        index_n = offs_n1[:, None]
        index_k = offs_k[None, :]
        # TODO generalize and add proper mask support
        mask = (index_n != -1) & (index_k != -1)
        {{store_output(("off_z", "off_h", "index_n", "index_k"), "dk", "mask", indent_width=8)}}
 """,
)


# TODO: We probably also need a layout constraint?
@register_lowering(
    torch.ops.higher_order.flex_attention_backward, type_promotion_kind=None
)
def flex_attention_backward(*args, **kwargs):
    (
        query,
        key,
        value,
        out,
        logsumexp,
        grad_out,
        fw_graph,
        joint_graph,
        *other_buffers,
    ) = args
    for buf in [query, key, value, grad_out]:
        buf.realize()

    device = query.get_device()
    dtype = query.get_dtype()

    fwd_placeholder_inps = [
        create_placeholder(name, dtype, device)
        for name, dtype in [
            ("score", dtype),
            ("b", torch.int32),
            ("h", torch.int32),
            ("m", torch.int32),
            ("n", torch.int32),
        ]
    ]
    fw_subgraph_buffer = build_subgraph_buffer(
        args, fwd_placeholder_inps, fw_graph, graph_type=SubgraphType.JOINT_FWD
    )

    joint_placeholder_inps = fwd_placeholder_inps + [
        create_placeholder("grad_score_mod", dtype, device)
    ]
    joint_subgraph_buffer = build_subgraph_buffer(
        args, joint_placeholder_inps, joint_graph, graph_type=SubgraphType.JOINT_BWD
    )

    layout_k = FixedLayout(
        key.get_device(),
        key.get_dtype(),
        key.get_size(),
        FlexibleLayout.contiguous_strides(key.get_size()),
    )

    # Create delta which will is needed for the bwd's kernel
    mul_delta = lowerings[aten.mul](out, grad_out)
    delta = lowerings[aten.sum](mul_delta, axis=-1)

    # see NOTE:[TritonTemplates with multiple outputs]
    grad_query = full(
        query.get_size(), 0.0, dtype=dtype, device=device
    )  # torch.zeros equivalent
    grad_query.realize()
    grad_value = empty_strided(value.get_size(), None, dtype=dtype, device=device)

    choices: List[Any] = []
    configs: List[Tuple[int, int, int, int]] = []
    configs.append(_get_default_config_bwd(query))
    if config.max_autotune:
        for BLOCK1 in [32, 64]:
            for BLOCK2 in [32, 64]:
                for w in [4, 8]:
                    for s in [1, 3]:
                        configs.append((BLOCK1, BLOCK2, w, s))

    for BLOCK1, BLOCK2, num_warps, num_stages in configs:
        flex_attention_backward_template.maybe_append_choice(
            choices=choices,
            input_nodes=[
                query,
                key,
                value,
                out,
                logsumexp,
                delta,
                grad_out,
                grad_query,
                grad_value,
            ],
            layout=layout_k,  # We use store_output only for grad_key
            subgraphs=[fw_subgraph_buffer, joint_subgraph_buffer],
            mutated_inputs=[grad_query, grad_value],
            call_sizes=query.get_size() + [key.get_size()[2]],
            num_stages=num_stages,
            num_warps=num_warps,
            BLOCK_M1=BLOCK1,
            BLOCK_N1=BLOCK1,
            BLOCK_M2=BLOCK2,
            BLOCK_N2=BLOCK2,
            BLOCK_DMODEL=query.get_size()[-1],
            # For now, we always assume the "sound" option
            SCORE_MOD_IS_LINEAR=False,
        )
    inputs_for_autotuning = [
        query,
        key,
        value,
        out,
        logsumexp,
        delta,
        grad_out,
        grad_query,
        grad_value,
    ] + list(other_buffers)

    grad_key = autotune_select_algorithm(
        "flex_attention_backward", choices, inputs_for_autotuning, layout_k
    )
    return (
        grad_query,
        grad_key,
        grad_value,
    )
