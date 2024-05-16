import math
from tabulate import tabulate


device_bw_tops = {
    "Gaudi2H_FP32": [2.24e12, 114e12, 11e12],
    "Gaudi2H_FP16": [2.24e12, 420e12, 22e12],
    "Gaudi2H_BF16": [2.24e12, 420e12, 22e12],
    "Gaudi2H_FP8": [2.24e12, 840e12, 22e12],
}

type2bytes = {
    "f32": 4,
    "bf16": 2,
    "fp8": 1,
}


class Config:
    def __init__(self, batch_size, seq_len_q, seq_len_kv, hidden_size, num_heads_q, num_heads_kv,
                 intermediate_size, is_decoding, num_bytes, bw, tops, tops_tpc, with_gate, num_experts, num_layers):
        self.batch_size = batch_size
        self.seq_len_q = seq_len_q
        self.seq_len_kv = seq_len_kv
        self.hidden_size = hidden_size
        self.num_heads_q = num_heads_q
        self.num_heads_kv = num_heads_kv
        self.intermediate_size = intermediate_size
        self.is_decoding = is_decoding
        self.num_bytes = num_bytes
        self.bw = bw
        self.tops = tops
        self.tops_tpc = tops_tpc
        self.with_gate = with_gate
        self.num_experts = num_experts
        self.num_layers = num_layers
        self.kvcache_bucket = False
        self.hardware_ai = tops / bw
        self.hardware_ai_attn = tops / bw


def proj_qkvo_proj(model_config):
    # memory (in & out)
    params_in_input = model_config.batch_size * \
        model_config.seq_len_q * model_config.hidden_size
    params_in_weight = model_config.hidden_size * model_config.hidden_size
    params_out = model_config.batch_size * \
        model_config.seq_len_q * model_config.hidden_size
    params_total = params_in_input + params_in_weight + params_out
    params_total *= 4  # 4 for qkvo
    bytes_total = params_total * model_config.num_bytes
    runtime_memory = bytes_total / model_config.bw

    # compute (2 for mul & add)
    # [B, T_Q, H] @ [H, H]
    num_ops = model_config.batch_size * model_config.seq_len_q * \
        model_config.hidden_size * model_config.hidden_size * 2 * 4  # 4 for qkvo
    tops = model_config.tops * \
        (model_config.batch_size * model_config.seq_len_q / 128)
    runtime_compute = num_ops / tops  # model_config.tops

    # arithmetic intensity (#flops / #bytes)
    math_ai = num_ops / bytes_total

    proj_rst = {
        "name": "qkvo_proj",
        "#ops": num_ops,
        "#mem": bytes_total,
        "math_ai": math_ai,
        "tops_roofline": min(tops, math_ai * model_config.bw),
        "latency": runtime_memory if runtime_memory > runtime_compute else runtime_compute,
        "bound": "memory" if runtime_memory > runtime_compute else "compute"
    }

    return proj_rst


def proj_attn_qk(model_config):
    head_dim = model_config.hidden_size // model_config.num_heads_q

    # memory (in & out)
    params_in_q = model_config.batch_size * model_config.num_heads_q * \
        model_config.seq_len_q * head_dim
    params_in_k = model_config.batch_size * model_config.num_heads_kv * \
        model_config.seq_len_kv * head_dim
    params_out = model_config.batch_size * model_config.num_heads_q * \
        model_config.seq_len_q * model_config.seq_len_kv
    params_total = params_in_q + params_in_k + params_out
    bytes_total = params_total * model_config.num_bytes
    runtime_memory = bytes_total / model_config.bw

    # compute (2 for mul & add)
    # [B, M, T_Q, D] @ [B, M, D, T_KV]
    num_ops = model_config.batch_size * model_config.num_heads_q * \
        model_config.seq_len_q * head_dim * model_config.seq_len_kv * 2
    tops = model_config.tops
    if model_config.is_decoding:
        tops = tops * (model_config.batch_size / 128) # 128 for Gaudi2
    runtime_compute = num_ops / tops

    # arithmetic intensity (#flops / #bytes)
    math_ai = num_ops / bytes_total

    proj_rst = {
        "name": "q@k_T",
        "#ops": num_ops,
        "#mem": bytes_total,
        "math_ai": math_ai,
        "tops_roofline": min(tops, math_ai * model_config.bw),
        "latency": runtime_memory if runtime_memory > runtime_compute else runtime_compute,
        "bound": "memory" if runtime_memory > runtime_compute else "compute"
    }

    return proj_rst


def proj_attn_softmax(model_config):
    head_dim = model_config.hidden_size // model_config.num_heads_q

    # memory (in & out)
    params_in = model_config.batch_size * model_config.num_heads_q * \
        model_config.seq_len_q * model_config.seq_len_kv
    params_out = model_config.batch_size * model_config.num_heads_q * \
        model_config.seq_len_q * model_config.seq_len_kv

    params_total = params_in + params_out
    bytes_total = params_total * model_config.num_bytes
    runtime_memory = bytes_total / model_config.bw

    # compute (max, x-max, exp(x-max), sum(exp(x-max)), x/sum(exp(x-max)))
    num_ops = model_config.batch_size * model_config.num_heads_q * \
        model_config.seq_len_q * head_dim * \
        model_config.seq_len_kv * 5  # 5 for traversal times
    runtime_compute = num_ops / model_config.tops_tpc

    # arithmetic intensity (#flops / #bytes)
    math_ai = num_ops / bytes_total

    proj_rst = {
        "name": "softmax",
        "#ops": num_ops,
        "#mem": bytes_total,
        "math_ai": math_ai,
        "tops_roofline": min(model_config.tops_tpc, math_ai * model_config.bw),
        "latency": runtime_memory if runtime_memory > runtime_compute else runtime_compute,
        "bound": "memory" if runtime_memory > runtime_compute else "compute"
    }

    return proj_rst


def proj_attn_scorev(model_config):
    head_dim = model_config.hidden_size // model_config.num_heads_q

    # memory (in & out)
    params_in_score = model_config.batch_size * model_config.num_heads_q * \
        model_config.seq_len_q * model_config.seq_len_kv
    params_in_v = model_config.batch_size * \
        model_config.num_heads_kv * model_config.seq_len_kv * head_dim
    params_out = model_config.batch_size * \
        model_config.num_heads_q * model_config.seq_len_q * head_dim
    params_total = params_in_score + params_in_v + params_out
    bytes_total = params_total * model_config.num_bytes
    runtime_memory = bytes_total / model_config.bw

    # compute (2 for mul & add)
    # [B, M, T_Q, T_KV] @ [B, M, T_KV, D]
    num_ops = model_config.batch_size * model_config.num_heads_q * \
        model_config.seq_len_q * model_config.seq_len_kv * head_dim * 2
    tops = model_config.tops
    if model_config.is_decoding:
        tops = tops * (model_config.batch_size / 128) # 128 for Gaudi2
    runtime_compute = num_ops / tops

    # arithmetic intensity (#flops / #bytes)
    math_ai = num_ops / bytes_total

    proj_rst = {
        "name": "score@v",
        "#ops": num_ops,
        "#mem": bytes_total,
        "math_ai": math_ai,
        "tops_roofline": min(tops, math_ai * model_config.bw),
        "latency": runtime_memory if runtime_memory > runtime_compute else runtime_compute,
        "bound": "memory" if runtime_memory > runtime_compute else "compute"
    }

    return proj_rst


def proj_attn(model_config):
    qk = proj_attn_qk(model_config)
    softmax = proj_attn_softmax(model_config)
    scorev = proj_attn_scorev(model_config)
    runtime_attn = qk["latency"] + softmax["latency"] + scorev["latency"]

    return runtime_attn, (qk, softmax, scorev)


def proj_mlp_gate_or_w3(model_config):
    # memory (in & out)
    params_in_input = model_config.batch_size * \
        model_config.seq_len_q * model_config.hidden_size
    params_in_weight = model_config.hidden_size * model_config.intermediate_size
    params_out = model_config.batch_size * \
        model_config.seq_len_q * model_config.intermediate_size
    params_total = params_in_input + params_in_weight + params_out
    bytes_total = params_total * model_config.num_bytes
    runtime_memory = bytes_total / model_config.bw

    # compute (2 for mul & add)
    # [B, T_Q, H] @ [H, H_Inter]
    num_ops = model_config.batch_size * model_config.seq_len_q * \
        model_config.hidden_size * model_config.intermediate_size * 2
    tops = model_config.tops * \
        (model_config.batch_size * model_config.seq_len_q / 128)  # 128 for Gaudi2
    runtime_compute = num_ops / tops  # model_config.tops

    # arithmetic intensity (#flops / #bytes)
    math_ai = num_ops / bytes_total

    proj_rst = {
        "name": "mlp_gate(w3)",
        "#ops": num_ops,
        "#mem": bytes_total,
        "math_ai": math_ai,
        "tops_roofline": min(tops, math_ai * model_config.bw),
        "latency": runtime_memory if runtime_memory > runtime_compute else runtime_compute,
        "bound": "memory" if runtime_memory > runtime_compute else "compute"
    }

    return proj_rst


def proj_mlp_up_or_w1(model_config):
    # memory (in & out)
    params_in_input = model_config.batch_size * \
        model_config.seq_len_q * model_config.hidden_size
    params_in_weight = model_config.hidden_size * model_config.intermediate_size
    params_out = model_config.batch_size * \
        model_config.seq_len_q * model_config.intermediate_size
    params_total = params_in_input + params_in_weight + params_out
    bytes_total = params_total * model_config.num_bytes
    runtime_memory = bytes_total / model_config.bw

    # compute (2 for mul & add)
    # [B, T_Q, H] @ [H, H_Inter]
    num_ops = model_config.batch_size * model_config.seq_len_q * \
        model_config.hidden_size * model_config.intermediate_size * 2
    tops = model_config.tops * \
        (model_config.batch_size * model_config.seq_len_q / 128)  # 128 for Gaudi2
    runtime_compute = num_ops / tops  # model_config.tops

    # arithmetic intensity (#flops / #bytes)
    math_ai = num_ops / bytes_total

    proj_rst = {
        "name": "mlp_up(w1)",
        "#ops": num_ops,
        "#mem": bytes_total,
        "math_ai": math_ai,
        "tops_roofline": min(tops, math_ai * model_config.bw),
        "latency": runtime_memory if runtime_memory > runtime_compute else runtime_compute,
        "bound": "memory" if runtime_memory > runtime_compute else "compute"
    }

    return proj_rst


def proj_mlp_down_or_w2(model_config):
    # memory (in & out)
    params_in_input = model_config.batch_size * \
        model_config.seq_len_q * model_config.intermediate_size
    params_in_weight = model_config.intermediate_size * model_config.hidden_size
    params_out = model_config.batch_size * \
        model_config.seq_len_q * model_config.hidden_size
    params_total = params_in_input + params_in_weight + params_out
    bytes_total = params_total * model_config.num_bytes
    runtime_memory = bytes_total / model_config.bw

    # compute (2 for mul & add)
    # [B, T_Q, H_Inter] @ [H_Inter, H]
    num_ops = model_config.batch_size * model_config.seq_len_q * \
        model_config.hidden_size * model_config.intermediate_size * 2
    tops = model_config.tops * \
        (model_config.batch_size * model_config.seq_len_q / 128)  # 128 for Gaudi2
    runtime_compute = num_ops / tops  # model_config.tops

    # arithmetic intensity (#flops / #bytes)
    math_ai = num_ops / bytes_total

    proj_rst = {
        "name": "mlp_down(w2)",
        "#ops": num_ops,
        "#mem": bytes_total,
        "math_ai": math_ai,
        "tops_roofline": min(tops, math_ai * model_config.bw),
        "latency": runtime_memory if runtime_memory > runtime_compute else runtime_compute,
        "bound": "memory" if runtime_memory > runtime_compute else "compute"
    }

    return proj_rst


def proj_mlp(model_config):
    up = proj_mlp_up_or_w1(model_config)
    if model_config.with_gate:
        gate = proj_mlp_gate_or_w3(model_config)
    down = proj_mlp_down_or_w2(model_config)

    runtime_mlp = up["latency"] + down["latency"]
    if model_config.with_gate:
        runtime_mlp += gate["latency"]

    return runtime_mlp, (up, down, gate if model_config.with_gate else None)


def proj_moe(model_config):
    runtime_mlp, mlp_items = proj_mlp(model_config)
    runtime_moe = runtime_mlp * model_config.num_experts

    return runtime_moe, mlp_items


def proj_single_layer(model_config):
    qkvo_proj = proj_qkvo_proj(model_config)
    runtime_attn, attn_items = proj_attn(model_config)
    runtime_moe, moe_items = proj_moe(model_config)
    runtime_single_layer = qkvo_proj["latency"] + runtime_attn + runtime_moe

    single_layer_items = {
        "qkvo": qkvo_proj,
        "attn": attn_items,
        "moe": moe_items,
    }

    return runtime_single_layer, single_layer_items


def proj_decoder(model_config):
    runtime_single_layer, single_layer_items = proj_single_layer(model_config)
    runtime_decoder = runtime_single_layer * model_config.num_layers

    return runtime_decoder, single_layer_items


item_list = ["HiddenSize", "NumHeadsQ", "NumHeadsKV", "InterSize", "IsDecoding", "NumExperts",
             "NumLayers", "SeqLength", "DataType", "BatchSize", "Latency (s)", "Throughput (tokens/sec)"]
layer_analysis_list = ["SeqLength", "DataType", "BatchSize", "LayerName",
                       "NumOps(e9)", "Memory(GB)", "TopsRF(TFlops)", "AI", "Bound"]


batchsize_list = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]
# batchsize_list = [1, 16, 32, 64, 128]
dtype_list = ["bf16", "fp8"]
device_list = ["Gaudi2H_BF16", "Gaudi2H_FP8"]

for dtype in dtype_list:
    for device in device_list:
        num_bytes = type2bytes[dtype]
        bw = device_bw_tops[device][0]
        tops = device_bw_tops[device][1]
        tops_tpc = device_bw_tops[device][2]
        # prefill long sequence
        print("projection prefill...")
        prefill_projection = [item_list]
        prefill_layer_analysis = dict()
        for bs in batchsize_list:
            prefill_layer_analysis[bs] = [layer_analysis_list]
            model_config = Config(batch_size=bs,
                                seq_len_q=32000,
                                seq_len_kv=32000,
                                hidden_size=4096,
                                num_heads_q=32,
                                num_heads_kv=8,
                                intermediate_size=14336,
                                is_decoding=False,
                                num_bytes=num_bytes,
                                bw=bw,
                                tops=tops,
                                tops_tpc=tops_tpc,
                                with_gate=True,
                                num_experts=8,
                                num_layers=32)
            runtime_decoder, single_layer_items = proj_decoder(model_config)
            prefill_projection.append([model_config.hidden_size, model_config.num_heads_q, model_config.num_heads_kv,
                                    model_config.intermediate_size, model_config.is_decoding, model_config.num_experts,
                                    model_config.num_layers, model_config.seq_len_q, dtype, bs, round(
                                        runtime_decoder, 2),
                                    round(1/runtime_decoder * model_config.batch_size, 2)])
            prefill_layer_analysis[bs].append(
                [model_config.seq_len_q, dtype, bs, single_layer_items["qkvo"]["name"], round(single_layer_items["qkvo"]["#ops"]/1e9, 2),
                round(single_layer_items["qkvo"]["#mem"]/1024/1024/1024,
                    2), round(single_layer_items["qkvo"]["tops_roofline"]/1e12, 2),
                round(single_layer_items["qkvo"]["math_ai"], 2), single_layer_items["qkvo"]["bound"]])
            for item in single_layer_items["attn"]:
                prefill_layer_analysis[bs].append(
                    [model_config.seq_len_q, dtype, bs, item["name"], round(item["#ops"]/1e9, 2), round(item["#mem"]/1024/1024/1024, 2),
                    round(item["tops_roofline"]/1e12, 2), round(item["math_ai"], 2), item["bound"]])
            for item in single_layer_items["moe"]:
                prefill_layer_analysis[bs].append(
                    [model_config.seq_len_q, dtype, bs, item["name"], round(item["#ops"]/1e9, 2), round(item["#mem"]/1024/1024/1024, 2),
                    round(item["tops_roofline"]/1e12, 2), round(item["math_ai"], 2), item["bound"]])
            # print(
            #     f"moe projection for prefill, bs: {bs}, 1st token latency: {runtime_decoder:.2f} s, 1st token throughput: {1/runtime_decoder} tokens/sec")
        print(tabulate(prefill_projection))
        for bs in batchsize_list:
            print(tabulate(prefill_layer_analysis[bs]))
        print("done!\n")

        # decode
        print("projection decoding...")
        decoding_projection = [item_list]
        decoding_layer_analysis = dict()
        for bs in batchsize_list:
            decoding_layer_analysis[bs] = [layer_analysis_list]
            model_config = Config(batch_size=bs,
                                seq_len_q=1,
                                seq_len_kv=512,
                                hidden_size=4096,
                                num_heads_q=32,
                                num_heads_kv=8,
                                intermediate_size=14336,
                                is_decoding=True,
                                num_bytes=num_bytes,
                                bw=bw,
                                tops=tops,
                                tops_tpc=tops_tpc,
                                with_gate=True,
                                num_experts=8,
                                num_layers=16)
            runtime_decoder, single_layer_items = proj_decoder(model_config)
            decoding_projection.append([model_config.hidden_size, model_config.num_heads_q, model_config.num_heads_kv,
                                        model_config.intermediate_size, model_config.is_decoding, model_config.num_experts,
                                        model_config.num_layers, model_config.seq_len_q, dtype, bs, round(
                                            runtime_decoder, 2),
                                        round(1/runtime_decoder * model_config.batch_size, 2)])
            decoding_layer_analysis[bs].append(
                [model_config.seq_len_q, dtype, bs, single_layer_items["qkvo"]["name"], round(single_layer_items["qkvo"]["#ops"]/1e9, 2),
                round(single_layer_items["qkvo"]["#mem"]/1024/1024/1024,
                    2), round(single_layer_items["qkvo"]["tops_roofline"]/1e12, 2),
                round(single_layer_items["qkvo"]["math_ai"], 2), single_layer_items["qkvo"]["bound"]]
            )
            for item in single_layer_items["attn"]:
                decoding_layer_analysis[bs].append(
                    [model_config.seq_len_q, dtype, bs, item["name"], round(item["#ops"]/1e9, 2), round(item["#mem"]/1024/1024/1024, 2),
                    round(item["tops_roofline"]/1e12, 2), round(item["math_ai"], 2), item["bound"]])
            for item in single_layer_items["moe"]:
                decoding_layer_analysis[bs].append(
                    [model_config.seq_len_q, dtype, bs, item["name"], round(item["#ops"]/1e9, 2), round(item["#mem"]/1024/1024/1024, 2),
                    round(item["tops_roofline"]/1e12, 2), round(item["math_ai"], 2), item["bound"]])
            # print(
            #     f"moe projection for decoding, bs: {bs}, 1st token latency: {runtime_decoder:.2f} s, 1st token throughput: {1/runtime_decoder} tokens/sec")
        print(tabulate(decoding_projection))
        for bs in batchsize_list:
            print(tabulate(decoding_layer_analysis[bs]))
        print("done!")
