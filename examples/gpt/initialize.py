import torch
import json
import veGiantModel

from megatron import get_args, mpu
from megatron.fp16 import FP16_Module
from torch.nn.parallel.distributed import DistributedDataParallel as torchDDP
from megatron.model import DistributedDataParallel as LocalDDP
from megatron.model import get_params_for_weight_decay_optimization
from apex.optimizers import FusedAdam as Adam
from megatron.learning_rates import AnnealingLR
from megatron import print_rank_0


def get_learning_rate_scheduler(optimizer, lr_scheduler_builder):
    """Build the learning rate scheduler."""
    args = get_args()


    if lr_scheduler_builder is not None:
        lr_scheduler = lr_scheduler_builder(optimizer)
    else:
        # Add linear learning rate scheduler.
        if args.lr_decay_iters is not None:
            num_iters = args.lr_decay_iters
        else:
            num_iters = args.train_iters
        num_iters = max(1, num_iters)
        init_step = 0
        warmup_iter = args.warmup * num_iters
        lr_scheduler = AnnealingLR(
            optimizer,
            start_lr=args.lr,
            warmup_iter=warmup_iter,
            total_iters=num_iters,
            decay_style=args.lr_decay_style,
            last_iter=init_step,
            min_lr=args.min_lr,
            use_checkpoint_lr_scheduler=args.use_checkpoint_lr_scheduler,
            override_lr_scheduler=args.override_lr_scheduler)

    return lr_scheduler


def get_model(model_provider_func):
    """Build the model."""
    args = get_args()

    # Build model on cpu.
    model = model_provider_func()

    # Print number of parameters.
    if mpu.get_data_parallel_rank() == 0:
        print(' > number of parameters on model parallel rank {}: {}'.format(
            mpu.get_model_parallel_rank(),
            sum([p.nelement() for p in model.parameters()])), flush=True)

    # GPU allocation.
    model.cuda(torch.cuda.current_device())

    return model

def get_optimizer(model):
    """Set up the optimizer."""
    args = get_args()

    # Build parameter groups (weight decay and non-decay).
    while isinstance(model, (torchDDP, LocalDDP, FP16_Module)):
        model = model.module
    param_groups = get_params_for_weight_decay_optimization(model)
    param_groups = list(param_groups)
    def rm_duplicate_params(params):
        new_params = []
        pids = []
        for p in params:
            if id(p) in pids:
                continue
            new_params.append(p)
            pids.append(id(p))
        return new_params
    print_rank_0(type(param_groups), file=open("init-param.txt", "a"))
    for i, pg in enumerate(param_groups):
        print_rank_0(list(pg.keys()), file=open("init-param.txt", "a"))
        for k in list(pg.keys()):
            if k == "params":
                param_groups[i][k] = rm_duplicate_params(pg[k])

    # Add model parallel attribute if it is not set.
    for param_group in param_groups:
        for param in param_group['params']:
            if not hasattr(param, 'model_parallel'):
                param.model_parallel = False

    if args.cpu_optimizer:
        if args.cpu_torch_adam:
            cpu_adam_optimizer = torch.optim.Adam
        else:
            from deepspeed.ops.adam import DeepSpeedCPUAdam
            cpu_adam_optimizer = DeepSpeedCPUAdam
        optimizer = cpu_adam_optimizer(param_groups,
                        lr=args.lr, weight_decay=args.weight_decay)
    else:
        # Use Adam.
        optimizer = Adam(param_groups, lr=args.lr, weight_decay=args.weight_decay)

    if args.deepspeed:
        # fp16 wrapper is not required for DeepSpeed.
        return optimizer

    # Wrap into fp16 optimizer.
    if args.fp16:
        optimizer = FP16_Optimizer(optimizer,
                                   static_loss_scale=args.loss_scale,
                                   dynamic_loss_scale=args.dynamic_loss_scale,
                                   dynamic_loss_args={
                                       'scale_window': args.loss_scale_window,
                                       'min_scale': args.min_scale,
                                       'delayed_shift': args.hysteresis},
                                   fp16_optim=args.fp16_optim)

    return optimizer

def setup_model_and_optimizer(model, optimizer, train_dataset_provider, lr_scheduler_builder):
    """Setup model and optimizer."""
    args = get_args()
    if optimizer is None:
        optimizer = get_optimizer(model)
    lr_scheduler = get_learning_rate_scheduler(optimizer, lr_scheduler_builder)

    print_rank_0("DeepSpeed is enabled.")

    # Print number of parameters.
    if mpu.get_data_parallel_rank() == 0:
        print(' > number of parameters on data parallel rank {}, model parallel rank {}, pipeline parallel rank {}: {}'.format(
            mpu.get_data_parallel_rank(),
            mpu.get_model_parallel_rank(),
            mpu.get_pipe_parallel_rank(),
            sum([p.nelement() for p in model.parameters()])), flush=True)

    if args.deepspeed_pipeline:
        print_rank_0("Pipeline Parallelism is enabled.")
        train_data = train_dataset_provider() if train_dataset_provider is not None else None
        _param_dict = json.loads(args.config_param)
        engine, optimizer, _, lr_scheduler = veGiantModel.initialize(
            model=model,
            optimizer=optimizer,
            args=args,
            lr_scheduler=lr_scheduler,
            mpu=None,
            dist_init_required=False,
            config_params = _param_dict,
            training_data=train_data
        )
        engine.set_batch_fn(model.batch_fn)
    else:
        engine, optimizer, _, lr_scheduler = veGiantModel.initialize(
            model=model,
            optimizer=optimizer,
            args=args,
            lr_scheduler=lr_scheduler,
            mpu=mpu,
            dist_init_required=False
        )

    print_rank_0("Model Preparation Done")
    args.iteration = 0

    return engine, optimizer, lr_scheduler


def initialize_pipeline(model, optimizer, train_dataset_provider, lr_scheduler_builder=None):
    return setup_model_and_optimizer(model, optimizer, train_dataset_provider, lr_scheduler_builder)


def initialize_distributed(num_stages, mp_size, distributed_backend='nccl'):
    veGiantModel.init_distribute(num_stages=num_stages, mp_size=mp_size, distributed_backend=distributed_backend)

def initialize_megatron(extra_args_provider=None, args_defaults={}):
    veGiantModel.initialize_megatron(extra_args_provider=extra_args_provider, args_defaults=args_defaults)
