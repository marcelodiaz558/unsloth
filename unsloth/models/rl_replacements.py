# Copyright 2023-present Daniel Han-Chen & the Unsloth team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

__all__ = [
    "RL_EXTRA_ARGS",
    "RL_FUNCTIONS",
    "RL_PRE_ITEMS",
]

import re
import torch
import inspect
from collections import defaultdict
RL_EXTRA_ARGS = defaultdict(list)
RL_FUNCTIONS  = defaultdict(list)
RL_PRE_ITEMS  = defaultdict(list)

torch_compile_options = {
    "epilogue_fusion"   : True,
    "max_autotune"      : True,
    "shape_padding"     : True,
    "trace.enabled"     : False,
    "triton.cudagraphs" : False,
}

# Check untrained tokens
def sft_trainer_fix_untraiend_tokens(call_args, extra_args):
    if "model" in call_args and "train_dataset" in call_args:
        fix_tokenizer = \
        "IGNORED_TOKENIZER_NAMES = os.environ.get('UNSLOTH_IGNORED_TOKENIZER_NAMES', '').split('\\n')\n"\
        "from unsloth_zoo.tokenizer_utils import fix_untrained_tokens\n"\
        "from unsloth_zoo.training_utils  import fix_zero_training_loss\n"\
        "if 'tokenizer' not in locals(): tokenizer = processing_class\n"\
        "fix_untrained_tokens(model, tokenizer, train_dataset, IGNORED_TOKENIZER_NAMES, eps = 1e-16)\n"\
        "fix_zero_training_loss(model, tokenizer, train_dataset)\n"
        return fix_tokenizer
    return ""
pass
RL_EXTRA_ARGS["sft_trainer"].append(sft_trainer_fix_untraiend_tokens)


# Remove DPO columns which might randomnly be tokenized
def dpo_trainer_fix_columns(call_args, extra_args):
    if "model" in call_args and "train_dataset" in call_args:
        fix_dpo = \
        "if hasattr(train_dataset, 'column_names'):\n"\
        "    column_names = set(train_dataset.column_names)\n"\
        "    check = ['chosen', 'rejected', 'prompt', 'chosen_input_ids', 'chosen_attention_mask',\n"\
        "             'chosen_labels', 'rejected_input_ids', 'rejected_attention_mask', 'rejected_labels',\n"\
        "             'prompt_input_ids', 'prompt_attention_mask']\n"\
        "    if all(x in column_names for x in check):\n"\
        "        train_dataset = train_dataset.remove_columns(['chosen', 'rejected', 'prompt'])\n"\
        "    del check, column_names\n"
        return fix_dpo
    return ""
pass
RL_EXTRA_ARGS["dpo_trainer"].append(dpo_trainer_fix_columns)


# Fix tokenizer double BOS
def sft_trainer_prepare_dataset(function_name, function):
    if  function_name != "_prepare_non_packed_dataloader" and \
        function_name != "_prepare_dataset": return function

    check_text = \
    "if 'tokenizer'          not in locals(): tokenizer = processing_class\n"\
    "if 'formatting_func'    not in locals(): raise RuntimeError('Unsloth: Please file a bug report - `formatting_func` does not exist!')\n"\
    "if 'dataset_text_field' not in locals() and 'args' in locals(): dataset_text_field = args.dataset_text_field\n"\
    "if 'dataset_text_field' not in locals(): raise RuntimeError('Unsloth: Please file a bug report - `dataset_text_field` does not exist!')\n"\
    "test_text = dataset[0][dataset_text_field] if (formatting_func is None and dataset_text_field is not None) else formatting_func(dataset[0])[0]\n"\
    "chat_template = getattr(tokenizer, 'chat_template', None)\n"\
    "chat_template = '' if chat_template is None else chat_template\n"\
    "has_bos_token_already = (test_text.startswith(tokenizer.bos_token) or tokenizer.bos_token in chat_template) "\
    "if getattr(tokenizer, 'bos_token', None) is not None else False\n"\
    "if 'add_special_tokens' not in locals() and has_bos_token_already:\n"\
    "    from functools import partial\n"\
    "    tokenizer = partial(tokenizer, add_special_tokens = False)\n"\
    "    processing_class = tokenizer\n"\
    "else:\n"\
    "    add_special_tokens = False if has_bos_token_already else add_special_tokens\n"

    check_text = check_text.split("\n")
    check_text = "\n".join(" "*8 + x for x in check_text)
    check_text = check_text.rstrip() + "\n"

    # .*? matches first match. .+? matches final match.
    replacer = re.findall(
        r"def {function_name}\(.*?\).*?\:\n",
        function,
        flags = re.MULTILINE | re.DOTALL,
    )
    if len(replacer) != 0:
        replacer = replacer[0]
        function = function.replace(replacer, replacer + check_text)
    pass
    return function
pass
RL_FUNCTIONS["sft_trainer"].append(sft_trainer_prepare_dataset)


# Ignore mean_token_accuracy since it needs logits
# We override it directly with our version
def sft_trainer_compute_loss(function_name, function):
    if  function_name != "compute_loss": return function

    def compute_loss(self, model, inputs, return_outputs = False, num_items_in_batch = None):
        outputs = super().compute_loss(
            model,
            inputs,
            return_outputs = return_outputs,
            num_items_in_batch = num_items_in_batch,
        )
        return outputs
    pass

    function = inspect.getsource(compute_loss)
    return function
pass
RL_FUNCTIONS["sft_trainer"].append(sft_trainer_compute_loss)


# Autocast precision for GRPO
def grpo_trainer__prepare_inputs(function_name, function):
    if  function_name != "_prepare_inputs": return function

    if "with torch.inference_mode()" not in function: return function

    # Add mixed precision training
    function = function.replace(
        "with torch.inference_mode():",

        "with torch.inference_mode(), "\
        "torch.amp.autocast(device_type = 'cuda', "\
        "dtype = torch.float16 if os.environ.get('ACCELERATE_MIXED_PRECISION', 'fp16') == 'fp16' else torch.bfloat16) "\
        "if not torch.is_autocast_enabled('cuda') else nullcontext():",
    )

    # Disable attaching a float32 conversion hook which upcasts logits to FP32
    function = function.replace(
        "self.accelerator.unwrap_model(self.model)",
        "self.accelerator.unwrap_model(self.model, keep_fp32_wrapper = False)",
    )
    return function
pass
RL_FUNCTIONS["grpo_trainer"].append(grpo_trainer__prepare_inputs)


# Remove _move_model_to_vllm
def grpo_trainer__move_model_to_vllm(function_name, function):
    if  function_name != "_move_model_to_vllm": return function

    # .*? matches first match. .+? matches final match.
    replacement = "def _move_model_to_vllm(self, *args, **kwargs): return None\n"
    return " "*function.find("def") + replacement
pass
RL_FUNCTIONS["grpo_trainer"].append(grpo_trainer__move_model_to_vllm)


# Edit _get_per_token_logps to handle mixed precision
def grpo_trainer__get_per_token_logps(function_name, function):
    if  function_name != "_get_per_token_logps": return function

    def _get_per_token_logps(self, model, input_ids, attention_mask, logits_to_keep):
        if not hasattr(self, '_autocast_dtype'):
            self._autocast_dtype = torch.float16 if os.environ.get('ACCELERATE_MIXED_PRECISION', 'fp16') == 'fp16' else torch.bfloat16
        with torch.amp.autocast(device_type = 'cuda', dtype = self._autocast_dtype):
            # We add 1 to `logits_to_keep` because the last logits of the sequence is later excluded
            logits = model(input_ids=input_ids, attention_mask=attention_mask, logits_to_keep=logits_to_keep + 1).logits
            logits = logits[:, :-1, :]  # (B, L-1, V), exclude the last logit: it corresponds to the next token pred

            input_ids = input_ids[:, -logits_to_keep:]
            # For transformers<=4.48, logits_to_keep argument isn't supported, so here we drop logits ourselves.
            # See https://github.com/huggingface/trl/issues/2770
            logits = logits[:, -logits_to_keep:]
            return logits
            # return selective_log_softmax(logits, input_ids)  #  compute logprobs for the input tokens
        pass
    pass

    function = inspect.getsource(_get_per_token_logps)
    return function
pass
RL_FUNCTIONS["grpo_trainer"].append(grpo_trainer__get_per_token_logps)


# Custom compiled GRPO loss - creates 3 Triton kernels
@torch.compile(dynamic = True, fullgraph = True, options = torch_compile_options,)
def _grpo_compute_loss(old_logits, new_logits, input_ids, mask, beta):
    old_logits = old_logits.to(torch.float32)
    new_logits = new_logits.to(torch.float32)
    input_ids  = input_ids.unsqueeze(-1)

    # x_i - logsumexp(x_i)
    old_x = torch.gather(old_logits, dim = -1, index = input_ids).squeeze(-1)
    new_x = torch.gather(new_logits, dim = -1, index = input_ids).squeeze(-1)
    old = old_x - torch.logsumexp(old_logits, dim = -1)
    new = new_x - torch.logsumexp(new_logits, dim = -1)

    kl_i = torch.exp(old - new) - (old - new) - 1.0
    loss_i = torch.exp(new - new.detach()) * advantages.unsqueeze(1)
    loss_i = -(loss_i - beta * kl_i)

    mask = mask.to(torch.float32)
    n_mask_per_reward = mask.sum(1)
    loss_per_reward = (loss_i * mask).sum(1) / n_mask_per_reward
    loss = loss_per_reward.mean()
    
    # Get metrics as well which are folded
    with torch.inference_mode():
        completion_length = n_mask_per_reward.mean()
        mean_kl_per_reward = (kl_i * mask).sum(1) / n_mask_per_reward
        mean_kl = mean_kl_per_reward.mean()
    pass
    return loss, completion_length, mean_kl
pass
def grpo_compute_loss(old_logits, new_logits, input_ids, mask, beta):
    loss, completion_length, mean_kl = _grpo_compute_loss(old_logits, new_logits, input_ids, mask, beta)
    return loss, completion_length.item(), mean_kl.item()
pass
RL_PRE_ITEMS["grpo_trainer"].append(inspect.getsource((_grpo_compute_loss)))
RL_PRE_ITEMS["grpo_trainer"].append(inspect.getsource((grpo_compute_loss)))


# Edit _get_per_token_logps to handle mixed precision
def grpo_trainer_compute_loss(function_name, function):
    if  function_name != "compute_loss": return function

    def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
        if return_outputs:
            raise ValueError("The GRPOTrainer does not support returning outputs")
        # Compute the per-token log probabilities for the model

        prompt_ids, prompt_mask = inputs["prompt_ids"], inputs["prompt_mask"]
        completion_ids, completion_mask = inputs["completion_ids"], inputs["completion_mask"]
        input_ids = torch.cat([prompt_ids, completion_ids], dim=1)
        # attention_mask = torch.cat([prompt_mask, completion_mask], dim=1)
        attention_mask = None
        logits_to_keep = completion_ids.size(1)  # we only need to compute the logits for the completion tokens

        per_token_logps = self._get_per_token_logps(model, input_ids, attention_mask, logits_to_keep)

        # Compute the KL divergence between the model and the reference model
        ref_per_token_logps = inputs["ref_per_token_logps"]
        # per_token_kl = torch.exp(ref_per_token_logps - per_token_logps) - (ref_per_token_logps - per_token_logps) - 1

        # x - x.detach() allows for preserving gradients from x
        advantages = inputs["advantages"]
        # per_token_loss = torch.exp(per_token_logps - per_token_logps.detach()) * advantages.unsqueeze(1)
        # per_token_loss = -(per_token_loss - self.beta * per_token_kl)
        # loss = ((per_token_loss * completion_mask).sum(dim=1) / completion_mask.sum(dim=1)).mean()

        loss, completion_length, mean_kl = grpo_compute_loss(
            ref_per_token_logps, per_token_logps, input_ids, completion_mask, self.beta,
        )
        # Log the metrics
        # completion_length = self.accelerator.gather_for_metrics(completion_mask.sum(1)).float().mean().item()
        self._metrics["completion_length"].append(completion_length)

        # mean_kl = ((per_token_kl * completion_mask).sum(dim=1) / completion_mask.sum(dim=1)).mean()
        # self._metrics["kl"].append(self.accelerator.gather_for_metrics(mean_kl).mean().item())
        self._metrics["kl"].append(mean_kl)
        return loss
    pass

    function = inspect.getsource(compute_loss)
    return function
pass
RL_FUNCTIONS["grpo_trainer"].append(grpo_trainer_compute_loss)
