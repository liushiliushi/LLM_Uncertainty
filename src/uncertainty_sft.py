# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the Llama 2 Community License Agreement.
import torch
from collections import Counter
import os

# os.environ["CUDA_VISIBLE_DEVICES"] = "0,2,6,5"
# os.environ["TOKENIZERS_PARALLELISM"] = "false"
import dataclasses
import fire
import random
import torch.optim as optim
from peft import get_peft_model, PeftModel, AutoPeftModelForCausalLM
from torch.distributed.fsdp import (
    FullyShardedDataParallel as FSDP,
    ShardingStrategy
)

from torch.distributed.fsdp.fully_sharded_data_parallel import CPUOffload
from torch.optim.lr_scheduler import StepLR
from transformers import (
    AutoTokenizer,
    BitsAndBytesConfig,
    LlamaForCausalLM,
    LlamaConfig,
)
from transformers.models.llama.modeling_llama import LlamaDecoderLayer

from llama_recipes.configs import fsdp_config as FSDP_CONFIG
from llama_recipes.configs import train_config as TRAIN_CONFIG
from llama_recipes.configs import quantization_config as QUANTIZATION_CONFIG
from llama_recipes.data.concatenator import ConcatDataset2
from llama_recipes.policies import AnyPrecisionAdamW, apply_fsdp_checkpointing

from llama_recipes.utils import fsdp_auto_wrap_policy
from llama_recipes.utils.config_utils import (
    update_config,
    generate_peft_config,
    generate_dataset_config,
    get_dataloader_kwargs,
    check_fsdp_config,
)
from llama_recipes.utils.dataset_utils import get_preprocessed_dataset, get_preprocessed_dataset2

from llama_recipes.utils.fsdp_utils import hsdp_device_mesh
from llama_recipes.utils.train_utils_uncertainty import (
    train,
    train_chat,
    train_token,
    freeze_transformer_layers,
    setup,
    setup_environ_flags,
    clear_gpu_cache,
    print_model_size,
    get_policies,
)
from accelerate.utils import is_xpu_available
from warnings import warn


def setup_wandb(train_config, fsdp_config, **kwargs):
    try:
        import wandb
    except ImportError:
        raise ImportError(
            "You are trying to use wandb which is not currently installed. "
            "Please install it using pip install wandb"
        )
    from llama_recipes.configs import wandb_config as WANDB_CONFIG
    wandb_config = WANDB_CONFIG()
    update_config(wandb_config, **kwargs)
    init_dict = dataclasses.asdict(wandb_config)
    run = wandb.init(**init_dict)
    run.config.update(train_config)
    run.config.update(fsdp_config, allow_val_change=True)
    return run


def main(**kwargs):
    # Update the configuration for the training and sharding process
    train_config, fsdp_config = TRAIN_CONFIG(), FSDP_CONFIG()
    update_config((train_config, fsdp_config), **kwargs)
    # Set the seeds for reproducibility
    if is_xpu_available():
        torch.xpu.manual_seed(train_config.seed)
    torch.manual_seed(train_config.seed)
    random.seed(train_config.seed)

    if train_config.enable_fsdp:
        setup()
        # torchrun specific
        local_rank = int(os.environ["LOCAL_RANK"])
        rank = int(os.environ["RANK"])
        world_size = int(os.environ["WORLD_SIZE"])

    if torch.distributed.is_initialized():
        if is_xpu_available():
            torch.xpu.set_device(local_rank)
        elif torch.cuda.is_available():
            torch.cuda.set_device(local_rank)
        clear_gpu_cache(local_rank)
        setup_environ_flags(rank)

    wandb_run = None

    if train_config.use_wandb:
        if not train_config.enable_fsdp or rank == 0:
            wandb_run = setup_wandb(train_config, fsdp_config, **kwargs)

    # setting quantization configs
    bnb_config = None
    if train_config.quantization:
        if type(train_config.quantization) == type(True):
            warn(
                "Quantization (--quantization) is a boolean, please specify quantization as '4bit' or '8bit'. Defaulting to '8bit' but this might change in the future.",
                FutureWarning)
            train_config.quantization = "8bit"

        if train_config.quantization == "8bit" and train_config.enable_fsdp:
            raise ValueError("8bit quantization is not supported with FSDP, please use 4bit quantization")

        quant_config = QUANTIZATION_CONFIG()
        update_config(quant_config, **kwargs)
        bnb_config = quant_config.create_bnb_config(train_config.quantization)

    # Load the pre-trained model and setup its configuration
    use_cache = False if train_config.enable_fsdp else None
    model = LlamaForCausalLM.from_pretrained(
        train_config.model_name,
        quantization_config=bnb_config,
        use_cache=use_cache,
        attn_implementation="sdpa" if train_config.use_fast_kernels else None,
        device_map="auto" if train_config.quantization and not train_config.enable_fsdp else None,
        torch_dtype=torch.float16 if train_config.use_fp16 else torch.bfloat16,
    )

    # model = AutoPeftModelForCausalLM.from_pretrained("checkpoints0921")

    # Load the tokenizer and add special tokens
    tokenizer = AutoTokenizer.from_pretrained(
        train_config.model_name if train_config.tokenizer_name is None else train_config.tokenizer_name)
    tokenizer.pad_token_id = tokenizer.eos_token_id

    # If there is a mismatch between tokenizer vocab size and embedding matrix,
    # throw a warning and then expand the embedding matrix
    if len(tokenizer) > model.get_input_embeddings().weight.shape[0]:
        print("WARNING: Resizing the embedding matrix to match the tokenizer vocab size.")
        model.resize_token_embeddings(len(tokenizer))

    print_model_size(model, train_config, rank if train_config.enable_fsdp else 0)

    # Convert the model to bfloat16 if fsdp and pure_bf16 is enabled
    if train_config.enable_fsdp and fsdp_config.pure_bf16 and not train_config.quantization:
        model.to(torch.bfloat16)

    if train_config.use_peft:
        # Load the pre-trained peft model checkpoint and setup its configuration
        if train_config.from_peft_checkpoint:
            model = PeftModel.from_pretrained(model, train_config.from_peft_checkpoint, is_trainable=True)
            peft_config = model.peft_config
        # Generate the peft config and start fine-tuning from original model
        else:
            peft_config = generate_peft_config(train_config, kwargs)
            model = get_peft_model(model, peft_config)
        if wandb_run:
            wandb_run.config.update(peft_config)
        model.print_trainable_parameters()

    hsdp_device_mesh_plan = None
    if fsdp_config.hsdp and fsdp_config.sharding_strategy == ShardingStrategy.HYBRID_SHARD:
        hsdp_device_mesh_plan = hsdp_device_mesh(replica_group_size=fsdp_config.replica_group_size,
                                                 sharding_group_size=fsdp_config.sharding_group_size)
        print("HSDP device mesh is ready")

    # setting up FSDP if enable_fsdp is enabled
    if train_config.enable_fsdp:
        check_fsdp_config(fsdp_config)

        if not train_config.use_peft and train_config.freeze_layers:
            freeze_transformer_layers(model, train_config.num_freeze_layers)

        mixed_precision_policy, wrapping_policy = get_policies(fsdp_config, rank)
        my_auto_wrapping_policy = fsdp_auto_wrap_policy(model, LlamaDecoderLayer)

        device_id = 0
        if is_xpu_available():
            device_id = torch.xpu.current_device()
        elif torch.cuda.is_available():
            device_id = torch.cuda.current_device()
        model = FSDP(
            model,
            auto_wrap_policy=my_auto_wrapping_policy if train_config.use_peft else wrapping_policy,
            cpu_offload=CPUOffload(offload_params=True) if fsdp_config.fsdp_cpu_offload else None,
            mixed_precision=mixed_precision_policy if not fsdp_config.pure_bf16 else None,
            sharding_strategy=fsdp_config.sharding_strategy,
            device_mesh=hsdp_device_mesh_plan,
            device_id=device_id,
            limit_all_gathers=True,
            sync_module_states=train_config.low_cpu_fsdp,
            param_init_fn=(lambda module: module.to_empty(device=torch.device("cuda"), recurse=False))
            if train_config.low_cpu_fsdp and rank != 0 else None,
        )
        if fsdp_config.fsdp_activation_checkpointing:
            model.enable_input_require_grads()
            model.gradient_checkpointing_enable()
            apply_fsdp_checkpointing(model)
    elif not train_config.quantization and not train_config.enable_fsdp:
        if is_xpu_available():
            model.to("xpu:0")
        elif torch.cuda.is_available():
            model.to("cuda")

    # dataset_config = generate_dataset_config(train_config, kwargs)
    # dataset_config.train_split = 'train'
    # dataset_config.test_split = 'validation'
    dataset_config = None
    # Load and preprocess the dataset for training and validation
    dataset_train = get_preprocessed_dataset2(
        tokenizer,
        train_config.dataset,
        'train',
        train_config.generate
    ).shuffle(seed=42)
    # dataset_train = dataset_train[:4]
    if not train_config.enable_fsdp or rank == 0:
        print(f"--> Training Set Length = {len(dataset_train)}")

    dataset_val = get_preprocessed_dataset2(
        tokenizer,
        train_config.dataset,
        'val',
        train_config.generate
    )
    dataset_test = get_preprocessed_dataset2(
        tokenizer,
        train_config.dataset,
        'test',
        train_config.generate
    )

    if not train_config.enable_fsdp or rank == 0:
        print(f"--> Validation Set Length = {len(dataset_val)}")

    if train_config.batching_strategy == "packing":
        dataset_train = ConcatDataset2(dataset_train, chunk_size=train_config.context_length)

    train_dl_kwargs = get_dataloader_kwargs(train_config, dataset_train, tokenizer, "train")

    # Create DataLoaders for the training and validation dataset
    train_dataloader = torch.utils.data.DataLoader(
        dataset_train,
        num_workers= 0,
        # num_workers=train_config.num_workers_dataloader,
        pin_memory=True,
        batch_sampler=train_dl_kwargs['batch_sampler']
        # **train_dl_kwargs,
    )

    test_dataloader = torch.utils.data.DataLoader(
        dataset_test,
        num_workers= 0,
        # num_workers=train_config.num_workers_dataloader,
        # pin_memory=True,
        batch_size=12
    )

    eval_dataloader = None
    if train_config.run_validation:
        if train_config.batching_strategy == "packing":
            dataset_val = ConcatDataset2(dataset_val, chunk_size=train_config.context_length)

        val_dl_kwargs = get_dataloader_kwargs(train_config, dataset_val, tokenizer, "val")

        eval_dataloader = torch.utils.data.DataLoader(
            dataset_val,
            num_workers=train_config.num_workers_dataloader,
            pin_memory=True,
            # **val_dl_kwargs,
            batch_sampler = val_dl_kwargs['batch_sampler'],
        )
        if len(eval_dataloader) == 0:
            raise ValueError(
                "The eval set size is too small for dataloader to load even one batch. Please increase the size of eval set.")
        else:
            print(f"--> Num of Validation Set Batches loaded = {len(eval_dataloader)}")

    # Initialize the optimizer and learning rate scheduler
    if fsdp_config.pure_bf16 and fsdp_config.optimizer == "anyprecision":
        optimizer = AnyPrecisionAdamW(
            model.parameters(),
            lr=train_config.lr,
            momentum_dtype=torch.bfloat16,
            variance_dtype=torch.bfloat16,
            use_kahan_summation=False,
            weight_decay=train_config.weight_decay,
        )
    else:
        optimizer = optim.AdamW(
            model.parameters(),
            lr=train_config.lr,
            weight_decay=train_config.weight_decay,
        )
    scheduler = StepLR(optimizer, step_size=1, gamma=train_config.gamma)
    # Start the training process
    results = train_token(
        model,
        train_dataloader,
        eval_dataloader,
        test_dataloader,
        tokenizer,
        optimizer,
        scheduler,
        train_config.gradient_accumulation_steps,
        train_config,
        fsdp_config if train_config.enable_fsdp else None,
        local_rank if train_config.enable_fsdp else None,
        rank if train_config.enable_fsdp else None,
        wandb_run,
    )
    if not train_config.enable_fsdp or rank == 0:
        [print(f'Key: {k}, Value: {v}') for k, v in results.items()]
        if train_config.use_wandb:
            for k, v in results.items():
                wandb_run.summary[k] = v


if __name__ == "__main__":
    fire.Fire(main)
